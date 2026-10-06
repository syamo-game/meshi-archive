from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db.models import LoginAttempt


WINDOW_SECONDS: int = 15 * 60
LoginRole = Literal["viewer", "admin"]


@dataclass(frozen=True)
class LoginResult:
    authenticated: bool
    retry_after: int = 0


def check_password(
    db: Session,
    *,
    role: LoginRole,
    client_host: str,
    password: str,
    expected_password: str | None,
    secret_key: str,
) -> LoginResult:
    address = ipaddress.ip_address(client_host)
    # Group IPv6 privacy addresses from the same network.
    source: str = str(ipaddress.ip_network(f"{address}/64", strict=False)) if address.version == 6 else str(address)
    key: str = hmac.new(secret_key.encode(), f"{role}:{source}".encode(), hashlib.sha256).hexdigest()
    now: int = int(time.time())
    dialect: str = db.get_bind().dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError(f"Unsupported login throttle database: dialect={dialect}")
    # Bound retention during long-running deployments, before taking a row lock.
    if secrets.randbelow(100) == 0:
        purge_expired_attempts(db)
    insert = sqlite_insert if dialect == "sqlite" else postgres_insert
    # The conflict update locks existing rows too, serializing concurrent attempts.
    statement = insert(LoginAttempt).values(key=key, failures=0, window_started=now, blocked_until=0)
    db.execute(statement.on_conflict_do_update(index_elements=[LoginAttempt.key], set_={"key": key}))
    attempt: LoginAttempt = db.execute(select(LoginAttempt).where(LoginAttempt.key == key)).scalar_one()
    if now - attempt.window_started >= WINDOW_SECONDS:
        attempt.failures = 0
        attempt.window_started = now
        attempt.blocked_until = 0
    if attempt.blocked_until > now:
        result = LoginResult(False, attempt.blocked_until - now)
    elif expected_password and hmac.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8")):
        db.delete(attempt)
        result = LoginResult(True)
    else:
        attempt.failures += 1
        delay: int = min(300, 30 * 2 ** min(attempt.failures - 5, 4)) if attempt.failures >= 5 else 0
        attempt.blocked_until = now + delay
        result = LoginResult(False, delay)
    db.commit()
    return result


def purge_expired_attempts(db: Session) -> None:
    db.execute(delete(LoginAttempt).where(LoginAttempt.window_started < int(time.time()) - WINDOW_SECONDS))
    db.commit()
