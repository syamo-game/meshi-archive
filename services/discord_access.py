"""Resolve configured Web roles and manage stored viewer access."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import AdminSession, DiscordViewer, DiscordViewerGrantEvent
from services.admin_sessions import AdminActor


_USER_ID = re.compile(r"[1-9][0-9]{16,19}")
_MAX_SNOWFLAKE = "18446744073709551615"


class DiscordAccessChangeError(ValueError):
    """The requested grant change conflicts with the current access state."""


class DiscordUserIdError(ValueError):
    """The supplied ID is not a supported exact Discord snowflake string."""


class DiscordUserRole(StrEnum):
    VIEWER = "viewer"
    ADMIN = "admin"


def normalize_discord_role(value: str) -> DiscordUserRole:
    try:
        return DiscordUserRole(value)
    except ValueError as exc:
        raise DiscordAccessChangeError("権限は閲覧または管理者を選択してください。") from exc


@dataclass(frozen=True)
class AddDiscordViewerResult:
    user_id: str
    created: bool
    is_admin: bool = False


@dataclass(frozen=True)
class ChangeDiscordViewerResult:
    user_id: str
    replacement_user_id: str | None = None
    saved: bool = False


@dataclass(frozen=True)
class DiscordViewerEntry:
    user_id: str
    is_admin: bool
    source: str
    created_at: datetime | None
    granted_by: str | None = None
    generation: str | None = None


def normalize_discord_user_id(value: str) -> str:
    user_id = value.strip()
    if not _USER_ID.fullmatch(user_id):
        raise DiscordUserIdError("DiscordのユーザーIDを17〜20桁の半角数字で入力してください。先頭に0は付けられません。")
    if len(user_id) == len(_MAX_SNOWFLAKE) and user_id > _MAX_SNOWFLAKE:
        raise DiscordUserIdError("DiscordのユーザーIDの範囲を超えています。IDをコピーし直してください。")
    return user_id


def configured_web_admin_ids(admin_user_id: str | None) -> set[str]:
    result: set[str] = set()
    for value in os.getenv("WEB_ADMIN_USER_IDS", "").split(","):
        if not value.strip():
            continue
        try:
            result.add(normalize_discord_user_id(value))
        except DiscordUserIdError as exc:
            raise RuntimeError("WEB_ADMIN_USER_IDS must contain exact Discord user IDs") from exc
    if admin_user_id:
        try:
            result.add(normalize_discord_user_id(admin_user_id))
        except DiscordUserIdError as exc:
            raise RuntimeError("ADMIN_USER_ID must contain an exact Discord user ID") from exc
    return result


def configured_discord_ids(admin_user_id: str | None) -> set[str]:
    result: set[str] = set()
    for value in os.getenv("DISCORD_ALLOWED_USER_IDS", "").split(","):
        if value.strip():
            try:
                result.add(normalize_discord_user_id(value))
            except DiscordUserIdError as exc:
                raise RuntimeError("DISCORD_ALLOWED_USER_IDS must contain exact Discord user IDs") from exc
    return result | configured_web_admin_ids(admin_user_id)


def is_discord_viewer_allowed(db: Session, user_id: str, configured_ids: set[str]) -> bool:
    return user_id in configured_ids or db.get(DiscordViewer, user_id) is not None


def discord_grant_generation(db: Session, user_id: str, configured_ids: set[str]) -> str | None:
    if user_id in configured_ids:
        return "configuration"
    viewer = db.get(DiscordViewer, user_id)
    return viewer.generation if viewer is not None and viewer.generation else None


def change_discord_viewer(
    db: Session, raw_user_id: str, configured_ids: set[str], actor: AdminActor,
    *, expected_generation: str, replacement_user_id: str | None = None,
) -> ChangeDiscordViewerResult:
    user_id: str = normalize_discord_user_id(raw_user_id)
    replacement: str | None = (
        normalize_discord_user_id(replacement_user_id) if replacement_user_id is not None else None
    )
    if user_id in configured_ids:
        raise DiscordAccessChangeError("設定で管理しているユーザーは、この画面では変更できません。")
    if replacement == user_id:
        raise DiscordAccessChangeError("訂正後のIDが同じです。別のDiscordユーザーIDを入力してください。")
    if replacement is not None and is_discord_viewer_allowed(db, replacement, configured_ids):
        raise DiscordAccessChangeError("訂正先のユーザーはすでに許可されています。現在の許可を確認してください。")
    previous_admin: bool | None = db.scalar(
        delete(DiscordViewer).where(
            DiscordViewer.discord_user_id == user_id,
            DiscordViewer.generation == expected_generation,
        ).returning(DiscordViewer.is_admin)
    )
    if previous_admin is None:
        raise DiscordAccessChangeError("利用許可が変更されています。一覧を再読み込みしてください。")
    if replacement is not None:
        db.add(DiscordViewer(discord_user_id=replacement, is_admin=previous_admin))
    db.add(DiscordViewerGrantEvent(
        discord_user_id=user_id, actor_method=actor.method,
        actor_discord_user_id=actor.discord_user_id, actor_session_hash=actor.session_hash,
        action="replace" if replacement is not None else "revoke", replacement_user_id=replacement,
        previous_role=DiscordUserRole.ADMIN if previous_admin else DiscordUserRole.VIEWER,
        new_role=(DiscordUserRole.ADMIN if previous_admin else DiscordUserRole.VIEWER) if replacement else None,
    ))
    db.execute(delete(AdminSession).where(AdminSession.actor_discord_user_id == user_id))
    db.commit()
    return ChangeDiscordViewerResult(user_id, replacement)


def save_discord_user(
    db: Session, raw_user_id: str, configured_ids: set[str], actor: AdminActor,
    *, expected_generation: str, replacement_user_id: str, role: DiscordUserRole,
) -> ChangeDiscordViewerResult:
    user_id: str = normalize_discord_user_id(raw_user_id)
    replacement: str = normalize_discord_user_id(replacement_user_id)
    if user_id in configured_ids:
        raise DiscordAccessChangeError("初期設定で固定されたユーザーは、この画面では変更できません。")
    viewer: DiscordViewer | None = db.get(DiscordViewer, user_id)
    if viewer is None or viewer.generation != expected_generation:
        raise DiscordAccessChangeError("利用許可が変更されています。一覧を再読み込みしてください。")
    if replacement != user_id and is_discord_viewer_allowed(db, replacement, configured_ids):
        raise DiscordAccessChangeError("変更先のユーザーはすでに登録されています。別のIDを入力してください。")
    previous_role: DiscordUserRole = DiscordUserRole.ADMIN if viewer.is_admin else DiscordUserRole.VIEWER
    if replacement == user_id and previous_role == role:
        return ChangeDiscordViewerResult(user_id, saved=True)
    try:
        saved_id: str | None = db.scalar(update(DiscordViewer).where(
            DiscordViewer.discord_user_id == user_id, DiscordViewer.generation == expected_generation,
        ).values(discord_user_id=replacement, is_admin=role == DiscordUserRole.ADMIN,
                 generation=secrets.token_hex(16)).returning(DiscordViewer.discord_user_id),
            execution_options={"synchronize_session": False})
    except IntegrityError as exc:
        db.rollback()
        if replacement != user_id and is_discord_viewer_allowed(db, replacement, configured_ids):
            raise DiscordAccessChangeError("変更先のユーザーはすでに登録されています。別のIDを入力してください。") from exc
        raise
    if saved_id is None:
        raise DiscordAccessChangeError("利用許可が変更されています。一覧を再読み込みしてください。")
    db.execute(delete(AdminSession).where(AdminSession.actor_discord_user_id == user_id))
    db.add(DiscordViewerGrantEvent(
        discord_user_id=user_id, actor_method=actor.method, actor_discord_user_id=actor.discord_user_id,
        actor_session_hash=actor.session_hash, action="update",
        replacement_user_id=replacement if replacement != user_id else None,
        previous_role=previous_role, new_role=role,
    ))
    db.commit()
    db.expire_all()
    return ChangeDiscordViewerResult(user_id, replacement if replacement != user_id else None, saved=True)


def add_discord_viewer(
    db: Session, raw_user_id: str, configured_ids: set[str], actor: AdminActor,
    *, role: DiscordUserRole = DiscordUserRole.VIEWER,
) -> AddDiscordViewerResult:
    user_id = normalize_discord_user_id(raw_user_id)
    if is_discord_viewer_allowed(db, user_id, configured_ids):
        return AddDiscordViewerResult(user_id, created=False)
    db.add(DiscordViewer(discord_user_id=user_id, is_admin=role == DiscordUserRole.ADMIN))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # Do not reject a retry when another request already granted access.
        if db.get(DiscordViewer, user_id) is None:
            raise
        return AddDiscordViewerResult(user_id, created=False)
    db.add(DiscordViewerGrantEvent(
        discord_user_id=user_id, actor_method=actor.method,
        actor_discord_user_id=actor.discord_user_id,
        actor_session_hash=actor.session_hash, new_role=role,
    ))
    db.commit()
    return AddDiscordViewerResult(user_id, created=True, is_admin=role == DiscordUserRole.ADMIN)


def list_discord_viewers(
    db: Session, configured_ids: set[str], admin_user_id: str | None,
) -> list[DiscordViewerEntry]:
    admin_ids = configured_web_admin_ids(admin_user_id)
    entries = {
        row.discord_user_id: DiscordViewerEntry(
            row.discord_user_id, row.is_admin, "database", row.created_at, generation=row.generation,
        )
        for row in db.scalars(select(DiscordViewer))
    }
    grants = list(db.scalars(select(DiscordViewerGrantEvent).order_by(DiscordViewerGrantEvent.id.desc())))
    for grant in grants:
        if grant.action not in {"grant", "replace", "update"}:
            continue
        target_id = grant.replacement_user_id or grant.discord_user_id
        entry = entries.get(target_id) if target_id is not None else None
        if entry is not None and entry.granted_by is None:
            label = (
                f"Discord ID {grant.actor_discord_user_id}"
                if grant.actor_method == "discord" and grant.actor_discord_user_id
                else "共有管理者パスワード（個人不明）"
            )
            entries[entry.user_id] = DiscordViewerEntry(
                entry.user_id, entry.is_admin, entry.source, entry.created_at, label, entry.generation,
            )
    for user_id in configured_ids | admin_ids:
        entries[user_id] = DiscordViewerEntry(user_id, user_id in admin_ids, "configuration", None)
    return sorted(entries.values(), key=lambda entry: (not entry.is_admin, len(entry.user_id), entry.user_id))
