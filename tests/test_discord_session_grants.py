from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import Base, DiscordViewer
from services.admin_sessions import issue_admin_session
from web import discord_session
from web.main import app as production_app
from web.routers import admin as admin_router, auth_discord


VIEWER_ID = "123456789012345678"
CONFIGURED_ID = "234567890123456789"
ADMIN_ID = "345678901234567890"
ADDED_ADMIN_ID = "456789012345678901"


def test_production_middleware_checks_grants_inside_signed_session() -> None:
    middleware = [entry.cls for entry in production_app.user_middleware]
    assert middleware.index(SessionMiddleware) < middleware.index(discord_session.DiscordGrantMiddleware)


def db_generation(engine: Engine, user_id: str) -> str | None:
    with Session(engine) as db:
        viewer = db.get(DiscordViewer, user_id)
        return viewer.generation if viewer else None


@pytest.fixture
def local_auth(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, Engine], None, None]:
    monkeypatch.delenv("WEB_ADMIN_USER_IDS", raising=False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(DiscordViewer(discord_user_id=VIEWER_ID))
        db.commit()
    monkeypatch.setattr(discord_session, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", ADMIN_ID)
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", CONFIGURED_ID)

    app = FastAPI()
    app.add_middleware(discord_session.DiscordGrantMiddleware)
    app.add_middleware(SessionMiddleware, secret_key="synthetic-signed-cookie-key", session_cookie="meshi_session")

    @app.get("/_test/issue/{identity}")
    def issue(request: Request, identity: str) -> dict[str, bool]:
        request.session.clear()
        if identity == "password-admin":
            with Session(engine) as db:
                request.session.update(authenticated=True, admin_authenticated=True)
        elif identity in {ADMIN_ID, ADDED_ADMIN_ID}:
            with Session(engine) as db:
                issue_admin_session(
                    db, request.session, method="discord", credential=identity,
                    discord_user_id=identity, discord_username="synthetic",
                )
        else:
            user_id = VIEWER_ID if identity == "stale-admin-marker" else identity
            request.session.update({
                "authenticated": True,
                "discord_user_id": user_id,
                "discord_username": "synthetic",
                "discord_grant_generation": "configuration" if user_id == CONFIGURED_ID else db_generation(engine, user_id),
                "discord_oauth_state": "pending-other-login",
            })
            if identity == "stale-admin-marker":
                request.session["admin_authenticated"] = True
        return {"issued": True}

    @app.get("/_test/change-identity/{identity}")
    def change_identity(request: Request, identity: str) -> dict[str, bool]:
        request.session["discord_user_id"] = identity
        return {"changed": True}

    @app.get("/_test/private")
    def private(request: Request) -> JSONResponse:
        allowed = bool(request.session.get("authenticated") or request.session.get("admin_authenticated"))
        return JSONResponse({"allowed": allowed}, status_code=200 if allowed else 403)

    @app.get("/_test/admin")
    def admin(request: Request) -> JSONResponse:
        allowed = bool(request.session.get("admin_authenticated"))
        return JSONResponse({"allowed": allowed}, status_code=200 if allowed else 403)

    @app.get("/_test/session")
    def session(request: Request) -> dict[str, object]:
        return dict(request.session)

    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def test_removed_grant_blocks_existing_and_replayed_signed_cookie(
    local_auth: tuple[TestClient, Engine],
) -> None:
    client, engine = local_auth
    client.get(f"/_test/issue/{VIEWER_ID}")
    assert client.get("/_test/private").status_code == 200
    old_cookie = client.cookies.get("meshi_session")
    assert old_cookie

    with Session(engine) as db:
        viewer = db.get(DiscordViewer, VIEWER_ID)
        assert viewer is not None
        db.delete(viewer)
        db.commit()

    assert client.get("/_test/private").status_code == 403
    current = client.get("/_test/session").json()
    assert "authenticated" not in current
    assert "discord_user_id" not in current
    assert current["discord_oauth_state"] == "pending-other-login"

    client.cookies.set("meshi_session", old_cookie)
    assert client.get("/_test/private").status_code == 403


def test_session_check_fails_closed_on_missing_grant_storage(
    local_auth: tuple[TestClient, Engine],
) -> None:
    client, engine = local_auth
    client.get(f"/_test/issue/{VIEWER_ID}")
    DiscordViewer.__table__.drop(engine)
    assert client.get("/_test/private").status_code == 403


def test_configured_discord_still_works_and_legacy_password_is_rejected_without_grant_table(
    local_auth: tuple[TestClient, Engine],
) -> None:
    client, engine = local_auth
    DiscordViewer.__table__.drop(engine)
    client.get(f"/_test/issue/{CONFIGURED_ID}")
    assert client.get("/_test/private").status_code == 200
    assert client.get("/_test/admin").status_code == 403
    client.get(f"/_test/issue/{ADMIN_ID}")
    assert client.get("/_test/admin").status_code == 200
    client.get("/_test/issue/password-admin")
    assert client.get("/_test/admin").status_code == 403


def test_discord_viewer_with_stale_admin_marker_is_rejected(
    local_auth: tuple[TestClient, Engine],
) -> None:
    client, _engine = local_auth
    # Simulate a previously signed session that carried both role flags.
    client.get("/_test/issue/stale-admin-marker")
    assert client.get("/_test/admin").status_code == 403
    assert client.get("/_test/private").status_code == 403


def test_modified_signed_cookie_cannot_claim_admin_access(
    local_auth: tuple[TestClient, Engine],
) -> None:
    client, _engine = local_auth
    client.get("/_test/issue/password-admin")
    original = client.cookies.get("meshi_session")
    assert original
    modified = ("A" if original[0] != "A" else "B") + original[1:]
    client.cookies.set("meshi_session", modified)
    assert client.get("/_test/admin").status_code == 403


def test_discord_admin_id_change_rejects_replayed_admin_cookie(
    local_auth: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _engine = local_auth
    client.get(f"/_test/issue/{ADMIN_ID}")
    old_cookie = client.cookies.get("meshi_session")
    assert old_cookie and client.get("/_test/admin").status_code == 200
    monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", "456789012345678901")
    assert client.get("/_test/admin").status_code == 403
    client.cookies.set("meshi_session", old_cookie)
    assert client.get("/_test/admin").status_code == 403


def test_adding_web_admin_preserves_primary_admin_existing_session(
    local_auth: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _engine = local_auth
    client.get(f"/_test/issue/{ADMIN_ID}")
    original_cookie = client.cookies.get("meshi_session")
    assert original_cookie and client.get("/_test/admin").status_code == 200

    monkeypatch.setenv("WEB_ADMIN_USER_IDS", ADDED_ADMIN_ID)
    client.cookies.set("meshi_session", original_cookie)
    assert client.get("/_test/admin").status_code == 200
    assert client.get("/_test/session").json()["discord_user_id"] == ADMIN_ID


def test_removing_added_admin_invalidates_existing_and_replayed_admin_session(
    local_auth: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, engine = local_auth
    monkeypatch.setenv("WEB_ADMIN_USER_IDS", ADDED_ADMIN_ID)
    with Session(engine) as db:
        db.add(DiscordViewer(discord_user_id=ADDED_ADMIN_ID))
        db.commit()
    client.get(f"/_test/issue/{ADDED_ADMIN_ID}")
    old_cookie = client.cookies.get("meshi_session")
    assert old_cookie and client.get("/_test/admin").status_code == 200

    monkeypatch.delenv("WEB_ADMIN_USER_IDS")
    assert client.get("/_test/admin").status_code == 403
    assert "admin_session_token" not in client.get("/_test/session").json()
    client.cookies.set("meshi_session", old_cookie)
    assert client.get("/_test/admin").status_code == 403


@pytest.mark.parametrize("replacement_id", (ADMIN_ID, VIEWER_ID))
def test_admin_session_cannot_switch_to_another_admin_or_viewer_identity(
    local_auth: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch, replacement_id: str,
) -> None:
    client, _engine = local_auth
    monkeypatch.setenv("WEB_ADMIN_USER_IDS", ADDED_ADMIN_ID)
    client.get(f"/_test/issue/{ADDED_ADMIN_ID}")
    assert client.get("/_test/admin").status_code == 200

    client.get(f"/_test/change-identity/{replacement_id}")
    mismatched_cookie = client.cookies.get("meshi_session")
    assert mismatched_cookie
    assert client.get("/_test/admin").status_code == 403
    client.cookies.set("meshi_session", mismatched_cookie)
    assert client.get("/_test/admin").status_code == 403
