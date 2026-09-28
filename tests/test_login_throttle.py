from __future__ import annotations

import os
import secrets
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from db.models import LoginAttempt
from services import login_throttle
from services.login_throttle import LoginResult, check_password, purge_expired_attempts


@pytest.fixture(params=("sqlite", "postgresql"))
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Generator[Engine, None, None]:
    url: str = f"sqlite:///{(tmp_path / 'throttle.db').as_posix()}"
    if request.param == "postgresql":
        url = os.getenv("TEST_POSTGRES_URL", "")
        if not url:
            pytest.skip("TEST_POSTGRES_URL is required for PostgreSQL concurrency tests")
        database: str = make_url(url).database or ""
        if not (database.startswith("test_") or database.endswith("_test")):
            raise RuntimeError("Login tests require a test database")
    db_engine: Engine = create_engine(url)
    LoginAttempt.__table__.create(db_engine, checkfirst=True)
    yield db_engine
    db_engine.dispose()


def attempt(engine: Engine, key: str, password: str = "wrong", host: str = "192.0.2.1") -> LoginResult:
    with Session(engine) as db:
        return check_password(
            db, role="viewer", client_host=host, password=password,
            expected_password="correct-test-password", secret_key=key,
        )


def test_concurrent_attempts_are_counted_once_each(engine: Engine) -> None:
    key: str = secrets.token_hex(32)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results: list[LoginResult] = list(executor.map(lambda _: attempt(engine, key), range(8)))
    assert sum(result.retry_after == 0 for result in results) == 4
    assert sum(result.retry_after > 0 for result in results) == 4
    assert not attempt(engine, key, "correct-test-password").authenticated
    assert attempt(engine, key, "correct-test-password", "192.0.2.2").authenticated


def test_restart_wait_and_success_reset(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    key: str = secrets.token_hex(32)
    clock: list[int] = [2_000_000_000]
    monkeypatch.setattr(login_throttle.time, "time", lambda: clock[0])
    for _ in range(5):
        result = attempt(engine, key)
    assert result.retry_after == 30
    engine.dispose()
    assert attempt(engine, key, "correct-test-password").retry_after == 30
    clock[0] += 30
    assert attempt(engine, key).retry_after == 60
    clock[0] += 60
    assert attempt(engine, key, "correct-test-password").authenticated
    assert attempt(engine, key).retry_after == 0
    clock[0] += login_throttle.WINDOW_SECONDS
    assert attempt(engine, key).retry_after == 0


def test_ipv6_network_and_login_roles(engine: Engine) -> None:
    key: str = secrets.token_hex(32)
    for index in range(1, 6):
        result = attempt(engine, key, host=f"2001:db8:1::{index}")
    assert result.retry_after > 0
    with Session(engine) as db:
        assert check_password(
            db, role="admin", client_host="2001:db8:1::99", password="test-password",
            expected_password="test-password", secret_key=key,
        ).authenticated


def test_stored_attempts_do_not_contain_addresses_or_passwords(engine: Engine) -> None:
    key: str = secrets.token_hex(32)
    attempt(engine, key)
    with Session(engine) as db:
        rows: list[LoginAttempt] = list(db.scalars(select(LoginAttempt)))
        assert rows
        assert all(len(row.key) == 64 and ":" not in row.key and "." not in row.key for row in rows)


def test_expired_attempt_cleanup(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    key: str = secrets.token_hex(32)
    monkeypatch.setattr(login_throttle.time, "time", lambda: 2_100_000_000)
    attempt(engine, key)
    monkeypatch.setattr(login_throttle.time, "time", lambda: 2_100_000_901)
    with Session(engine) as db:
        purge_expired_attempts(db)
        assert db.scalar(select(LoginAttempt).where(LoginAttempt.window_started == 2_100_000_000)) is None
