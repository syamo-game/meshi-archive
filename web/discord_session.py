"""Recheck Discord grants before trusting a signed browser session."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from db.database import SessionLocal
from services.admin_sessions import get_admin_actor
from services.discord_access import configured_discord_ids, configured_web_admin_ids, discord_grant_generation
from web.routers import auth_discord


logger = logging.getLogger(__name__)
_AUTH_SESSION_KEYS = (
    "authenticated", "admin_authenticated", "discord_user_id", "discord_username",
    "admin_session_token", "discord_grant_generation", "csrf_token", "csv_update_sha256",
)


def _check_session(session: dict[str, object]) -> tuple[bool, bool]:
    user_id = session.get("discord_user_id")
    if not isinstance(user_id, str) or not user_id:
        return False, False
    with SessionLocal() as db:
        if session.get("admin_authenticated"):
            valid: bool = get_admin_actor(
                db, session, admin_discord_ids=configured_web_admin_ids(auth_discord.ADMIN_USER_ID),
            ) is not None
            return valid, valid
        generation: str | None = discord_grant_generation(
            db, user_id, configured_discord_ids(auth_discord.ADMIN_USER_ID),
        )
        return False, generation is not None and session.get("discord_grant_generation") == generation


class DiscordGrantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        session = request.session
        has_admin = bool(session.get("admin_authenticated"))
        if has_admin or session.get("authenticated") or session.get("discord_user_id") is not None:
            try:
                admin_valid, viewer_allowed = await run_in_threadpool(_check_session, session)
            except SQLAlchemyError as exc:
                logger.error("Authentication session check failed: error_type=%s", type(exc).__name__)
                admin_valid, viewer_allowed = False, False
            if (has_admin and not admin_valid) or not viewer_allowed:
                for key in _AUTH_SESSION_KEYS:
                    session.pop(key, None)
        return await call_next(request)
