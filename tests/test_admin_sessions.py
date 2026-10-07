from __future__ import annotations

import re
from collections.abc import Generator
from datetime import timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import AdminSession, Base, utc_now
from services.admin_sessions import issue_admin_session
from web import discord_session
from web.routers import admin, auth_discord, home


PASSWORD = "synthetic-session-password"
ADMIN_ID = "123456789012345678"
VIEWER_ID = "234567890123456789"


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, Engine], None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(discord_session, "SessionLocal", factory)
    monkeypatch.setattr(home, "SessionLocal", factory)
    monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", ADMIN_ID)
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", VIEWER_ID)
    monkeypatch.setenv("APP_READ_ONLY", "false")
    app = FastAPI()
    app.add_middleware(discord_session.DiscordGrantMiddleware)
    app.add_middleware(
        SessionMiddleware, secret_key="synthetic-admin-session-signing-key",
        session_cookie="meshi_session",
    )
    app.include_router(admin.router, prefix="/admin")
    app.include_router(home.router)

    @app.get("/_test/admin-login")
    def admin_session(request: Request) -> dict[str, bool]:
        with factory() as db:
            issue_admin_session(db, request.session, method="discord", credential=ADMIN_ID, discord_user_id=ADMIN_ID)
        return {"issued": True}

    @app.get("/_test/viewer")
    def viewer_session(request: Request) -> dict[str, bool]:
        request.session.clear()
        request.session.update(authenticated=True, discord_user_id=VIEWER_ID, discord_grant_generation="configuration")
        return {"issued": True}

    @app.get("/_test/session")
    def session_state(request: Request) -> dict[str, object]:
        return dict(request.session)

    def database() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[admin.get_db] = database
    app.dependency_overrides[home.get_db] = database
    with TestClient(app, client=("192.0.2.1", 50000)) as client:
        yield client, engine
    engine.dispose()


def _login(client: TestClient) -> str:
    assert client.get("/_test/admin-login").status_code == 200
    cookie = client.cookies.get("meshi_session")
    assert cookie is not None
    return cookie


def test_logout_revokes_server_session_and_replayed_signed_cookie(
    app_client: tuple[TestClient, Engine],
) -> None:
    client, engine = app_client
    signed_cookie = _login(client)
    assert client.get("/admin/users").status_code == 200
    with Session(engine) as db:
        stored = db.scalar(select(AdminSession))
        assert stored is not None
        assert stored.token_hash not in signed_cookie

    confirmation = client.get("/logout")
    assert confirmation.status_code == 200
    assert client.get("/admin/users").status_code == 200
    assert client.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
    assert client.get("/admin/users").status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', confirmation.text)
    assert token is not None
    assert client.post("/logout", data={"csrf_token": token.group(1)}, follow_redirects=False).status_code == 302
    with Session(engine) as db:
        assert db.scalar(select(AdminSession)) is None
    client.cookies.set("meshi_session", signed_cookie)
    denied = client.get("/admin/users", follow_redirects=False)
    assert denied.status_code == 302
    assert denied.headers["location"] == "/admin/login"


def test_absolute_expiry_and_admin_removal_reject_fresh_signed_cookie(
    app_client: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, engine = app_client
    signed_cookie = _login(client)
    with Session(engine) as db:
        stored = db.scalar(select(AdminSession))
        assert stored is not None
        stored.expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
    assert client.get("/admin/users", follow_redirects=False).status_code == 302

    with Session(engine) as db:
        stored = db.scalar(select(AdminSession))
        assert stored is not None
        stored.expires_at = utc_now() + timedelta(hours=1)
        db.commit()
    client.cookies.set("meshi_session", signed_cookie)
    monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", "345678901234567890")
    assert client.get("/admin/users", follow_redirects=False).status_code == 302


def test_admin_storage_failure_fails_closed(
    app_client: tuple[TestClient, Engine],
) -> None:
    client, engine = app_client
    _login(client)
    AdminSession.__table__.drop(engine)
    assert client.get("/admin/users", follow_redirects=False).status_code == 302


def test_discord_admin_login_replaces_discord_viewer_identity(
    app_client: tuple[TestClient, Engine],
) -> None:
    client, _engine = app_client
    client.get("/_test/viewer")
    assert client.get("/").status_code == 200
    _login(client)
    state = client.get("/_test/session").json()
    assert state["admin_authenticated"] is True
    assert state["discord_user_id"] == ADMIN_ID
    assert client.get("/admin/users").status_code == 200
