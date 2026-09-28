from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from services.login_throttle import LoginRole, check_password


logger = logging.getLogger(__name__)
_LOCAL_KEY: str = secrets.token_hex(32)


@dataclass(frozen=True)
class PasswordLogin:
    authenticated: bool
    error: str | None = None
    status_code: int = 401
    retry_after: int = 0


def authenticate_password(
    request: Request, db: Session, role: LoginRole, password: str, expected: str | None,
) -> PasswordLogin:
    try:
        if request.client is None:
            raise ValueError("Missing client address")
        result = check_password(
            db, role=role, client_host=request.client.host, password=password,
            expected_password=expected, secret_key=os.getenv("SECRET_KEY") or _LOCAL_KEY,
        )
    except (SQLAlchemyError, ValueError, RuntimeError) as exc:
        db.rollback()
        # Database exceptions may contain connection details; log the type only.
        logger.error("Password login unavailable: role=%s error_type=%s", role, type(exc).__name__)
        return PasswordLogin(False, "現在ログインできません。時間をおいて再度お試しください。", 503)
    if result.retry_after:
        return PasswordLogin(
            False, f"ログインに失敗した回数が多いため、{result.retry_after}秒後に再度お試しください。",
            429, result.retry_after,
        )
    return PasswordLogin(
        result.authenticated,
        None if result.authenticated else "パスワードを入力してください。" if not password else "パスワードが正しくありません。",
        400 if not password else 401,
    )
