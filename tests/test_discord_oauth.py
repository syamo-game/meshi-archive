from __future__ import annotations

import secrets
from collections.abc import Generator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import Base, DiscordViewer
from services.admin_sessions import get_admin_actor
from web.auth import is_admin
from web.csrf import get_csrf_token
from web.routers import auth_discord


VIEWER_ID = "123456789012345678"
ADMIN_ID = "234567890123456789"
OTHER_ID = "345678901234567890"
ADDED_ADMIN_ID = "456789012345678901"
PRIVATE_MARKER = "synthetic-private-provider-response"


@dataclass
class DiscordProvider:
    user_data: object = field(default_factory=lambda: {"id": VIEWER_ID, "username": "viewer"})
    token_data: object = field(default_factory=lambda: {"access_token": "synthetic-token"})
    token_status: int = 200
    user_status: int = 200
    invalid_json_stage: str | None = None
    network_error_stage: str | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "discord.com"
        if request.url.path == "/api/oauth2/token":
            stage = "token"
            assert request.method == "POST"
            payload = parse_qs(request.content.decode())
            assert payload["grant_type"] == ["authorization_code"]
            assert payload["client_secret"] == ["synthetic-client-secret"]
            assert payload["code"] == ["verified-code"]
            status, data = self.token_status, self.token_data
        else:
            stage = "user"
            assert request.url.path == "/api/users/@me"
            assert request.method == "GET"
            assert request.headers["authorization"] == "Bearer synthetic-token"
            status, data = self.user_status, self.user_data
        if self.network_error_stage == stage:
            raise httpx.ConnectError(PRIVATE_MARKER, request=request)
        if self.invalid_json_stage == stage:
            return httpx.Response(status, text=PRIVATE_MARKER)
        return httpx.Response(status, json=data)


@dataclass
class OAuthApp:
    client: TestClient
    engine: Engine
    sql: list[str]
    provider: DiscordProvider

    def add_viewer(self, user_id: str = VIEWER_ID) -> None:
        with Session(self.engine) as db:
            db.add(DiscordViewer(discord_user_id=user_id))
            db.commit()
        self.sql.clear()

    def begin(self) -> str:
        response = self.client.get("/auth/discord", follow_redirects=False)
        assert response.status_code == 302
        url = urlsplit(response.headers["location"])
        assert url.scheme == "https" and url.netloc == "discord.com"
        parameters = parse_qs(url.query)
        assert parameters["scope"] == ["identify"]
        return parameters["state"][0]

    def callback(self, state: str) -> httpx.Response:
        return self.client.get(
            "/auth/discord/callback", params={"code": "verified-code", "state": state},
            follow_redirects=False,
        )

    def session(self) -> dict[str, object]:
        return self.client.get("/_test/session").json()


@pytest.fixture
def oauth_app(monkeypatch: pytest.MonkeyPatch) -> Generator[OAuthApp, None, None]:
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "")
    monkeypatch.delenv("WEB_ADMIN_USER_IDS", raising=False)
    monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", None)
    monkeypatch.setattr(auth_discord, "DISCORD_CLIENT_ID", "synthetic-client")
    monkeypatch.setattr(auth_discord, "DISCORD_CLIENT_SECRET", "synthetic-client-secret")
    monkeypatch.setattr(auth_discord, "DISCORD_REDIRECT_URI", "http://testserver/auth/discord/callback")
    provider = DiscordProvider()
    original_client = httpx.AsyncClient

    def remote_client(*, timeout: float) -> httpx.AsyncClient:
        return original_client(timeout=timeout, transport=httpx.MockTransport(provider.handle))

    monkeypatch.setattr(auth_discord.httpx, "AsyncClient", remote_client)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record_sql(
        connection: object, cursor: object, statement: str, parameters: object,
        context: object, executemany: bool,
    ) -> None:
        statements.append(statement)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(32))
    app.include_router(auth_discord.router)

    def get_db() -> Generator[Session, None, None]:
        with Session(engine) as db:
            yield db

    app.dependency_overrides[auth_discord.get_db] = get_db

    @app.get("/_test/session")
    def session(request: Request) -> dict[str, object]:
        return dict(request.session)

    @app.post("/_test/admin-session")
    def administrator_session(request: Request) -> dict[str, bool]:
        request.session.update({
            "authenticated": True, "admin_authenticated": True,
            "discord_user_id": ADMIN_ID, "discord_username": "previous-admin",
            "csrf_token": "old-csrf-token", "csv_update_sha256": "old-csv-confirmation",
        })
        return {"ok": True}

    @app.get("/_test/admin-only")
    def admin_only(request: Request) -> JSONResponse:
        return JSONResponse({"allowed": is_admin(request)}, status_code=200 if is_admin(request) else 403)

    @app.get("/_test/csrf")
    def csrf(request: Request) -> dict[str, str]:
        return {"token": get_csrf_token(request)}

    with TestClient(app) as client:
        yield OAuthApp(client, engine, statements, provider)
    engine.dispose()


@pytest.mark.parametrize("membership", ("database", "environment", "both", "administrator"))
def test_oauth_authorizes_union_of_existing_configuration_and_database(
    oauth_app: OAuthApp, monkeypatch: pytest.MonkeyPatch, membership: str,
) -> None:
    if membership in {"database", "both"}:
        oauth_app.add_viewer()
    if membership in {"environment", "both"}:
        monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", f" {VIEWER_ID}, {OTHER_ID} ")
    if membership == "administrator":
        monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", VIEWER_ID)

    response = oauth_app.callback(oauth_app.begin())

    assert response.status_code == 302 and response.headers["location"] == "/"
    session = oauth_app.session()
    assert session["authenticated"] is True
    assert session["discord_user_id"] == VIEWER_ID
    assert session["discord_username"] == "viewer"
    assert bool(session.get("admin_authenticated")) is (membership == "administrator")
    assert "discord_oauth_state" not in session
    assert len(oauth_app.provider.requests) == 2
    if membership == "database":
        assert any("discord_viewers" in statement for statement in oauth_app.sql)
    elif membership != "administrator":
        assert oauth_app.sql == []
    else:
        assert any("INSERT INTO admin_sessions" in statement for statement in oauth_app.sql)
    with Session(oauth_app.engine) as db:
        assert db.query(DiscordViewer).count() == (1 if membership in {"database", "both"} else 0)


@pytest.mark.parametrize("user_id", ("10000000000000000", "18446744073709551615"))
def test_oauth_preserves_exact_valid_discord_id_string(oauth_app: OAuthApp, user_id: str) -> None:
    oauth_app.add_viewer(user_id)
    oauth_app.provider.user_data = {"id": user_id}
    assert oauth_app.callback(oauth_app.begin()).headers["location"] == "/"
    assert oauth_app.session()["discord_user_id"] == user_id
    assert oauth_app.session()["discord_username"] == "unknown"


@pytest.mark.parametrize("user_id", (ADMIN_ID, ADDED_ADMIN_ID))
def test_oauth_admins_receive_sessions_bound_to_their_own_discord_identity(
    oauth_app: OAuthApp, monkeypatch: pytest.MonkeyPatch, user_id: str,
) -> None:
    monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", ADMIN_ID)
    monkeypatch.setenv("WEB_ADMIN_USER_IDS", ADDED_ADMIN_ID)
    oauth_app.provider.user_data = {"id": user_id, "username": "synthetic-admin"}

    response = oauth_app.callback(oauth_app.begin())

    assert response.status_code == 302 and response.headers["location"] == "/"
    session = oauth_app.session()
    assert session["admin_authenticated"] is True
    assert session["discord_user_id"] == user_id
    with Session(oauth_app.engine) as db:
        actor = get_admin_actor(
            db, session, admin_discord_ids={ADMIN_ID, ADDED_ADMIN_ID},
        )
        assert actor is not None and actor.discord_user_id == user_id
        assert db.query(DiscordViewer).count() == 0


@pytest.mark.parametrize("membership", ("database", "environment"))
def test_configuring_multiple_admins_does_not_promote_existing_viewers(
    oauth_app: OAuthApp, monkeypatch: pytest.MonkeyPatch, membership: str,
) -> None:
    monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", ADMIN_ID)
    monkeypatch.setenv("WEB_ADMIN_USER_IDS", ADDED_ADMIN_ID)
    if membership == "database":
        oauth_app.add_viewer()
    else:
        monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", VIEWER_ID)

    assert oauth_app.callback(oauth_app.begin()).headers["location"] == "/"
    assert oauth_app.session()["authenticated"] is True
    assert not oauth_app.session().get("admin_authenticated")
    assert oauth_app.client.get("/_test/admin-only").status_code == 403


@pytest.mark.parametrize("membership", ("database", "environment"))
def test_viewer_oauth_clears_previous_admin_and_session_confirmations(
    oauth_app: OAuthApp, monkeypatch: pytest.MonkeyPatch, membership: str,
) -> None:
    monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", ADMIN_ID)
    if membership == "database":
        oauth_app.add_viewer()
    else:
        monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", VIEWER_ID)
    oauth_app.client.post("/_test/admin-session")
    assert oauth_app.client.get("/_test/admin-only").status_code == 200

    assert oauth_app.callback(oauth_app.begin()).headers["location"] == "/"

    state = oauth_app.session()
    assert state.pop("discord_grant_generation")
    assert state == {
        "authenticated": True, "discord_user_id": VIEWER_ID, "discord_username": "viewer",
    }
    assert oauth_app.client.get("/_test/admin-only").status_code == 403
    assert oauth_app.client.get("/_test/csrf").json()["token"] != "old-csrf-token"


def test_verified_unlisted_user_is_denied_without_creating_allowlist_entry(oauth_app: OAuthApp) -> None:
    response = oauth_app.callback(oauth_app.begin())
    assert response.headers["location"] == "/login?error=discord_unauthorized"
    assert not oauth_app.session().get("authenticated")
    assert any("discord_viewers" in statement for statement in oauth_app.sql)
    with Session(oauth_app.engine) as db:
        assert db.query(DiscordViewer).count() == 0


def test_callback_ignores_supplied_user_id_and_uses_discord_identity(oauth_app: OAuthApp) -> None:
    oauth_app.add_viewer()
    oauth_app.provider.user_data = {"id": OTHER_ID, "username": "unlisted"}
    response = oauth_app.client.get(
        "/auth/discord/callback",
        params={"code": "verified-code", "state": oauth_app.begin(), "user_id": VIEWER_ID, "id": VIEWER_ID},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/login?error=discord_unauthorized"
    assert not oauth_app.session().get("authenticated")


@pytest.mark.parametrize("request_kind", ("id_only", "missing_code", "missing_state", "forged_state", "provider_error"))
def test_callback_requires_oauth_proof_before_database_lookup(oauth_app: OAuthApp, request_kind: str) -> None:
    oauth_app.add_viewer()
    state = oauth_app.begin()
    parameters = {
        "id_only": {"id": VIEWER_ID, "user_id": VIEWER_ID},
        "missing_code": {"state": state, "user_id": VIEWER_ID},
        "missing_state": {"code": "verified-code"},
        "forged_state": {"code": "verified-code", "state": "forged"},
        "provider_error": {"error": "access_denied", "code": "verified-code", "state": state},
    }[request_kind]
    response = oauth_app.client.get("/auth/discord/callback", params=parameters, follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"].startswith("/login?error=")
    assert not oauth_app.session().get("authenticated")
    assert oauth_app.sql == []
    assert oauth_app.provider.requests == []


def test_consumed_oauth_state_cannot_be_replayed(oauth_app: OAuthApp) -> None:
    state = oauth_app.begin()
    assert oauth_app.callback(state).headers["location"] == "/login?error=discord_unauthorized"
    oauth_app.sql.clear()
    oauth_app.provider.requests.clear()
    oauth_app.add_viewer()
    assert oauth_app.callback(state).headers["location"] == "/login?error=discord_state_mismatch"
    assert oauth_app.sql == [] and oauth_app.provider.requests == []
    assert not oauth_app.session().get("authenticated")


def test_missing_oauth_configuration_does_not_query_database(
    oauth_app: OAuthApp, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = oauth_app.begin()
    monkeypatch.setattr(auth_discord, "DISCORD_CLIENT_SECRET", None)
    assert oauth_app.callback(state).headers["location"] == "/login?error=discord_not_configured"
    assert oauth_app.client.get("/auth/discord", follow_redirects=False).headers["location"] == "/login?error=discord_not_configured"
    assert oauth_app.sql == [] and oauth_app.provider.requests == []


@pytest.mark.parametrize("failure", (
    "token_status", "user_status", "token_json", "user_json", "token_shape", "user_shape",
    "missing_token", "non_string_token", "token_network", "user_network",
))
def test_provider_failure_denies_access_without_database_lookup_or_private_logs(
    oauth_app: OAuthApp, caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    provider = oauth_app.provider
    if failure == "token_status":
        provider.token_status, provider.token_data = 400, {"error": PRIVATE_MARKER}
    elif failure == "user_status":
        provider.user_status, provider.user_data = 401, {"error": PRIVATE_MARKER}
    elif failure in {"token_json", "user_json"}:
        provider.invalid_json_stage = failure.split("_")[0]
    elif failure == "token_shape":
        provider.token_data = [PRIVATE_MARKER]
    elif failure == "user_shape":
        provider.user_data = [PRIVATE_MARKER]
    elif failure == "missing_token":
        provider.token_data = {}
    elif failure == "non_string_token":
        provider.token_data = {"access_token": {"private": PRIVATE_MARKER}}
    else:
        provider.network_error_stage = failure.split("_")[0]
    response = oauth_app.callback(oauth_app.begin())
    assert response.status_code == 302 and response.headers["location"].startswith("/login?error=")
    assert not oauth_app.session().get("authenticated")
    assert oauth_app.sql == []
    assert PRIVATE_MARKER not in caplog.text + response.text
    assert "synthetic-token" not in caplog.text + response.text
    assert "synthetic-client-secret" not in caplog.text + response.text


@pytest.mark.parametrize("user_id", (
    None, "", int(VIEWER_ID), float(VIEWER_ID), True, {}, [],
    "1234567890123456", "123456789012345678901", "18446744073709551616",
    "012345678901234567", "+123456789012345678", "1.23456789012345678e17",
    "１２３４５６７８９０１２３４５６７８", "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦٧٨",
    f" {VIEWER_ID}", f"{VIEWER_ID}\n",
))
def test_malformed_provider_id_is_not_coerced_or_looked_up(oauth_app: OAuthApp, user_id: object) -> None:
    oauth_app.provider.user_data = {"id": user_id, "username": "viewer"}
    response = oauth_app.callback(oauth_app.begin())
    assert response.headers["location"] in {
        "/login?error=discord_no_user_id", "/login?error=discord_invalid_user_id",
    }
    assert oauth_app.sql == []
    assert not oauth_app.session().get("authenticated")


def test_database_lookup_failure_does_not_authenticate_or_disclose_sql(
    oauth_app: OAuthApp, caplog: pytest.LogCaptureFixture,
) -> None:
    DiscordViewer.__table__.drop(oauth_app.engine)
    response = oauth_app.callback(oauth_app.begin())
    assert response.headers["location"] == "/login?error=discord_access_unavailable"
    assert not oauth_app.session().get("authenticated")
    assert "OperationalError" in caplog.text
    for private_value in ("SELECT", "discord_viewers", VIEWER_ID, "synthetic-token"):
        assert private_value not in response.text + caplog.text


def test_stored_administrator_receives_real_bound_admin_session(oauth_app: OAuthApp) -> None:
    with Session(oauth_app.engine) as db:
        db.add(DiscordViewer(discord_user_id=VIEWER_ID, is_admin=True))
        db.commit()
    response = oauth_app.callback(oauth_app.begin())
    assert response.status_code == 302 and response.headers["location"] == "/"
    session = oauth_app.session()
    assert session["admin_authenticated"] is True and session["discord_user_id"] == VIEWER_ID
    with Session(oauth_app.engine) as db:
        assert get_admin_actor(db, session, admin_discord_ids=set()) is not None
        db.get(DiscordViewer, VIEWER_ID).is_admin = False
        db.commit()
        assert get_admin_actor(db, session, admin_discord_ids=set()) is None
