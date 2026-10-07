import logging
import os
import secrets
from collections.abc import Generator
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from db.database import SessionLocal
from services.admin_sessions import is_discord_admin, issue_admin_session, revoke_admin_session
from services.discord_access import (
    DiscordUserIdError,
    configured_discord_ids,
    configured_web_admin_ids,
    discord_grant_generation,
    normalize_discord_user_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
_TOKEN_URL = "https://discord.com/api/oauth2/token"
_USER_URL = "https://discord.com/api/users/@me"
_HTTP_TIMEOUT = 10.0


def is_configured() -> bool:
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI)


def _allowed_ids() -> set[str]:
    return configured_discord_ids(ADMIN_USER_ID)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as db:
        yield db


@router.get("/auth/discord")
def discord_login(request: Request) -> RedirectResponse:
    if not is_configured():
        return RedirectResponse("/login?error=discord_not_configured", status_code=302)
    state = secrets.token_urlsafe(32)
    request.session["discord_oauth_state"] = state
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
        "prompt": "none",
    }
    return RedirectResponse(f"{_AUTHORIZE_URL}?{urlencode(params)}", status_code=302)


@router.get("/auth/discord/callback")
async def discord_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/login?error=discord_{error}", status_code=302)
    if not code or not state:
        return RedirectResponse("/login?error=discord_missing_params", status_code=302)

    saved_state = request.session.pop("discord_oauth_state", None)
    if not saved_state or saved_state != state:
        return RedirectResponse("/login?error=discord_state_mismatch", status_code=302)

    if not is_configured():
        return RedirectResponse("/login?error=discord_not_configured", status_code=302)

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            token_resp = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": DISCORD_REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_resp.status_code != 200:
                logger.warning("Discord token exchange failed: status=%s", token_resp.status_code)
                return RedirectResponse("/login?error=discord_token_failed", status_code=302)
            try:
                token_data: object = token_resp.json()
            except ValueError:
                logger.warning("Discord token exchange returned invalid JSON")
                return RedirectResponse("/login?error=discord_token_failed", status_code=302)
            if not isinstance(token_data, dict):
                return RedirectResponse("/login?error=discord_token_failed", status_code=302)
            access_token = token_data.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                return RedirectResponse("/login?error=discord_no_token", status_code=302)

            user_resp = await client.get(
                _USER_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_resp.status_code != 200:
                logger.warning("Discord user fetch failed: status=%s", user_resp.status_code)
                return RedirectResponse("/login?error=discord_user_failed", status_code=302)
            try:
                user: object = user_resp.json()
            except ValueError:
                logger.warning("Discord user fetch returned invalid JSON")
                return RedirectResponse("/login?error=discord_user_failed", status_code=302)
            if not isinstance(user, dict):
                return RedirectResponse("/login?error=discord_user_failed", status_code=302)
    except httpx.HTTPError as e:
        logger.error("Discord OAuth network error: type=%s", type(e).__name__)
        return RedirectResponse("/login?error=discord_network", status_code=302)

    raw_user_id = user.get("id")
    if raw_user_id is None or raw_user_id == "":
        return RedirectResponse("/login?error=discord_no_user_id", status_code=302)
    if not isinstance(raw_user_id, str):
        return RedirectResponse("/login?error=discord_invalid_user_id", status_code=302)
    try:
        user_id = normalize_discord_user_id(raw_user_id)
    except DiscordUserIdError:
        return RedirectResponse("/login?error=discord_invalid_user_id", status_code=302)
    if user_id != raw_user_id:
        return RedirectResponse("/login?error=discord_invalid_user_id", status_code=302)
    raw_username = user.get("username")
    username = raw_username if isinstance(raw_username, str) and raw_username else "unknown"

    try:
        generation: str | None = await run_in_threadpool(discord_grant_generation, db, user_id, _allowed_ids())
        admin_allowed: bool = user_id in configured_web_admin_ids(ADMIN_USER_ID)
        if generation is not None and generation != "configuration":
            admin_allowed = await run_in_threadpool(is_discord_admin, db, user_id, configured_web_admin_ids(ADMIN_USER_ID))
    except SQLAlchemyError as exc:
        logger.error("Discord access lookup failed: type=%s", type(exc).__name__)
        return RedirectResponse("/login?error=discord_access_unavailable", status_code=302)
    if generation is None:
        logger.info("Discord login denied for unregistered user id=%s username=%s", user_id, username)
        return RedirectResponse("/login?error=discord_unauthorized", status_code=302)

    try:
        if admin_allowed:
            await run_in_threadpool(
                issue_admin_session,
                db, request.session, method="discord", credential=user_id,
                discord_user_id=user_id, discord_username=username,
            )
        else:
            await run_in_threadpool(revoke_admin_session, db, request.session)
            request.session.clear()
            request.session["authenticated"] = True
            request.session["discord_user_id"] = user_id
            request.session["discord_username"] = username
            request.session["discord_grant_generation"] = generation
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Discord session creation failed: error_type=%s", type(exc).__name__)
        return RedirectResponse("/login?error=discord_access_unavailable", status_code=302)
    if admin_allowed:
        logger.info("Discord admin login: id=%s username=%s", user_id, username)
    else:
        logger.info("Discord viewer login: id=%s username=%s", user_id, username)

    return RedirectResponse("/", status_code=302)
