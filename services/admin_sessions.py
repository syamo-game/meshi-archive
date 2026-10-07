from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from db.models import AdminSession, DiscordViewer, utc_now


ADMIN_SESSION_MAX_AGE = timedelta(hours=24)
_BINDING_KEY = (os.getenv("SECRET_KEY") or secrets.token_hex(32)).encode("utf-8")
_SESSION_KEY = "admin_session_token"


@dataclass(frozen=True)
class AdminActor:
    method: str
    discord_user_id: str | None
    session_hash: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _credential_binding(method: str, credential: str) -> str:
    return hmac.new(
        _BINDING_KEY, f"{method}:{credential}".encode("utf-8"), hashlib.sha256,
    ).hexdigest()


def issue_admin_session(
    db: Session, session: dict[str, object], *, method: str,
    credential: str, discord_user_id: str | None = None,
    discord_username: str | None = None,
) -> AdminActor:
    if method != "discord" or not credential or credential != discord_user_id:
        raise ValueError("Admin session requires a configured authentication method")
    if (method == "discord") != (discord_user_id is not None):
        raise ValueError("Discord admin session requires an exact Discord ID")
    db.execute(delete(AdminSession).where(AdminSession.expires_at <= utc_now()))
    old_token = session.get(_SESSION_KEY)
    if isinstance(old_token, str):
        old_session = db.get(AdminSession, _token_hash(old_token))
        if old_session is not None:
            db.delete(old_session)
    token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    db.add(AdminSession(
        token_hash=token_hash, auth_method=method,
        actor_discord_user_id=discord_user_id,
        credential_binding=_credential_binding(method, credential),
        expires_at=utc_now() + ADMIN_SESSION_MAX_AGE,
    ))
    db.commit()
    session.clear()
    session["admin_authenticated"] = True
    session[_SESSION_KEY] = token
    if discord_user_id is not None:
        session["authenticated"] = True
        session["discord_user_id"] = discord_user_id
        session["discord_username"] = discord_username or "unknown"
    return AdminActor(method, discord_user_id, token_hash)


def is_discord_admin(db: Session, user_id: str, configured_admin_ids: set[str]) -> bool:
    if user_id in configured_admin_ids:
        return True
    if user_id in {value.strip() for value in os.getenv("DISCORD_ALLOWED_USER_IDS", "").split(",")}:
        return False
    viewer: DiscordViewer | None = db.get(DiscordViewer, user_id)
    return viewer is not None and viewer.is_admin


def get_admin_actor(
    db: Session, session: dict[str, object], *,
    admin_discord_ids: set[str],
) -> AdminActor | None:
    if session.get("admin_authenticated") is not True:
        return None
    token = session.get(_SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        return None
    token_hash = _token_hash(token)
    stored = db.get(AdminSession, token_hash)
    if stored is None:
        return None
    expires_at = stored.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= utc_now():
        return None
    actor_id = stored.actor_discord_user_id
    if (
        stored.auth_method != "discord"
        or actor_id is None
        or not is_discord_admin(db, actor_id, admin_discord_ids)
        or session.get("discord_user_id") != actor_id
    ):
        return None
    credential = actor_id
    if not hmac.compare_digest(
        stored.credential_binding, _credential_binding(stored.auth_method, credential)
    ):
        return None
    return AdminActor(stored.auth_method, stored.actor_discord_user_id, token_hash)


def revoke_admin_session(db: Session, session: dict[str, object]) -> None:
    token = session.get(_SESSION_KEY)
    if not isinstance(token, str):
        return
    stored = db.get(AdminSession, _token_hash(token))
    if stored is not None:
        db.delete(stored)
        db.commit()
