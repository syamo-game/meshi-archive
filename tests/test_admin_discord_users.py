from __future__ import annotations

from collections.abc import Generator
from html.parser import HTMLParser
from typing import Literal

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import Base, DiscordViewer, DiscordViewerGrantEvent
from services.admin_sessions import issue_admin_session
from web.csrf import get_csrf_token
from web.routers import admin, auth_discord, home


ADMIN_ID = "11111111111111111"
CONFIGURED_ID = "22222222222222222"
STORED_ID = "33333333333333333"
NEW_ID = "123456789012345678"
PASSWORD = "synthetic-admin-users-password"


class _FormParser(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: dict[str, dict[str, str | None]] = {}
        self.form_actions: list[str] = []
        self.submit_buttons: list[dict[str, str | None]] = []
        self.rows: list[str] = []
        self._row_parts: list[str] | None = None
        self._form_action: str | None = None
        self.feed(markup)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self._form_action = attributes.get("action")
            if self._form_action is not None:
                self.form_actions.append(self._form_action)
        if tag == "input" and attributes.get("name"):
            name = attributes["name"]
            assert name is not None
            self.inputs[name] = attributes
        if tag == "button" and self._form_action == "/admin/users":
            if attributes.get("type", "submit") == "submit":
                self.submit_buttons.append(attributes)
        if tag == "tr":
            self._row_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form_action = None
        if tag == "tr" and self._row_parts is not None:
            self.rows.append(" ".join(self._row_parts))
            self._row_parts = None

    def handle_data(self, data: str) -> None:
        if self._row_parts is not None and data.strip():
            self._row_parts.append(data.strip())


class _SessionState(BaseModel):
    admin_authenticated: bool
    authenticated: bool
    csrf_token: str


@pytest.fixture
def engine() -> Generator[Engine, None, None]:
    isolated = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(isolated)
    with Session(isolated) as db:
        db.add(DiscordViewer(discord_user_id=STORED_ID))
        db.commit()
    yield isolated
    isolated.dispose()


@pytest.fixture
def client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_READ_ONLY", "false")
    monkeypatch.setenv("ALLOW_ANONYMOUS_READ", "false")
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", CONFIGURED_ID)
    monkeypatch.setattr(auth_discord, "ADMIN_USER_ID", ADMIN_ID)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="synthetic-admin-users-session-key")
    app.include_router(admin.router, prefix="/admin")
    app.include_router(home.router)

    def database() -> Generator[Session, None, None]:
        with Session(engine) as db:
            yield db

    @app.get("/_test/session/{role}")
    def set_session(request: Request, role: Literal["admin", "viewer", "anonymous"]) -> _SessionState:
        request.session.clear()
        if role == "admin":
            with Session(engine) as db:
                issue_admin_session(db, request.session, method="discord", credential=ADMIN_ID, discord_user_id=ADMIN_ID)
        elif role == "viewer":
            request.session["authenticated"] = True
        return _SessionState(
            admin_authenticated=bool(request.session.get("admin_authenticated")),
            authenticated=bool(request.session.get("authenticated")),
            csrf_token=get_csrf_token(request),
        )

    @app.get("/_test/session-state")
    def session_state(request: Request) -> _SessionState:
        return _SessionState(
            admin_authenticated=bool(request.session.get("admin_authenticated")),
            authenticated=bool(request.session.get("authenticated")),
            csrf_token=get_csrf_token(request),
        )

    app.dependency_overrides[admin.get_db] = database
    app.dependency_overrides[home.get_db] = database
    with TestClient(app, client=("192.0.2.1", 50000)) as browser:
        yield browser


def _stored_ids(engine: Engine) -> list[str]:
    with Session(engine) as db:
        return list(db.scalars(select(DiscordViewer.discord_user_id).order_by(DiscordViewer.discord_user_id)))


def _grant_events(engine: Engine) -> list[DiscordViewerGrantEvent]:
    with Session(engine) as db:
        return list(db.scalars(select(DiscordViewerGrantEvent).order_by(DiscordViewerGrantEvent.id)))


def _csrf(markup: str) -> str:
    token = _FormParser(markup).inputs["csrf_token"].get("value")
    assert token
    return token


def _admin_token(client: TestClient) -> str:
    client.get("/_test/session/admin")
    form = client.get("/admin/users")
    assert form.status_code == 200
    return _csrf(form.text)


def _assert_private(response: Response) -> None:
    cache_control = response.headers.get("cache-control", "")
    assert "private" in cache_control
    assert "no-store" in cache_control


@pytest.mark.parametrize("role", ("anonymous", "viewer"))
def test_user_list_requires_admin_without_leaking_any_ids(client: TestClient, role: str) -> None:
    client.get(f"/_test/session/{role}")

    response = client.get("/admin/users", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"
    _assert_private(response)
    assert all(user_id not in response.text for user_id in (ADMIN_ID, CONFIGURED_ID, STORED_ID))


def test_admin_list_renders_all_sources_and_safe_text_id_form(client: TestClient) -> None:
    client.get("/_test/session/admin")

    response = client.get("/admin/users")

    assert response.status_code == 200
    _assert_private(response)
    assert response.headers["content-type"].startswith("text/html")
    parsed = _FormParser(response.text)
    assert "/admin/users" in parsed.form_actions
    assert all(any(user_id in row for row in parsed.rows) for user_id in (ADMIN_ID, CONFIGURED_ID, STORED_ID))
    field = parsed.inputs["discord_user_id"]
    assert field["type"] == "text"
    assert field["inputmode"] == "numeric"
    assert _csrf(response.text)
    assert len(parsed.submit_buttons) == 1
    assert "disabled" not in parsed.submit_buttons[0]


@pytest.mark.parametrize("role", ("anonymous", "viewer"))
@pytest.mark.parametrize("read_only", (False, True))
def test_post_auth_precedes_write_mode_csrf_and_validation(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch, role: str, read_only: bool,
) -> None:
    client.get(f"/_test/session/{role}")
    monkeypatch.setenv("APP_READ_ONLY", str(read_only).lower())

    response = client.post("/admin/users", data={}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"
    assert _stored_ids(engine) == [STORED_ID]
    assert all(user_id not in response.text for user_id in (ADMIN_ID, CONFIGURED_ID, STORED_ID))


@pytest.mark.parametrize("submitted_token", (None, "", "forged-token"))
def test_missing_or_forged_csrf_is_403_without_validation_or_write(
    client: TestClient, engine: Engine, submitted_token: str | None,
) -> None:
    _admin_token(client)
    data = {"discord_user_id": NEW_ID}
    if submitted_token is not None:
        data["csrf_token"] = submitted_token

    response = client.post("/admin/users", data=data)

    assert response.status_code == 403
    assert _stored_ids(engine) == [STORED_ID]


@pytest.mark.parametrize("raw_id", (
    "", "1234567890123456", "123456789012345678901", "01234567890123456",
    "１２３４５６７８９０１２３４５６７", "1.2345678901234568e17", "18446744073709551616",
    '12345678901234567\"><script>alert("id")</script>',
))
def test_invalid_id_returns_escaped_japanese_form_and_preserves_table(
    client: TestClient, engine: Engine, raw_id: str,
) -> None:
    token = _admin_token(client)

    response = client.post("/admin/users", data={"discord_user_id": raw_id, "csrf_token": token})

    assert response.status_code == 422
    _assert_private(response)
    assert response.headers["content-type"].startswith("text/html")
    assert "ユーザーID" in response.text
    assert any(message in response.text for message in ("入力してください", "コピーし直してください"))
    parsed = _FormParser(response.text)
    assert parsed.inputs["discord_user_id"].get("value", "") == raw_id
    assert _csrf(response.text) == token
    if "<script>" in raw_id:
        assert raw_id not in response.text
        assert "&lt;script&gt;" in response.text
    assert _stored_ids(engine) == [STORED_ID]


def test_add_and_duplicate_are_idempotent_and_keep_exact_large_id(client: TestClient, engine: Engine) -> None:
    token = _admin_token(client)

    created = client.post("/admin/users", data={"discord_user_id": f" {NEW_ID} ", "csrf_token": token})

    assert created.status_code == 200
    _assert_private(created)
    assert "追加しました" in created.text
    assert _stored_ids(engine) == sorted([STORED_ID, NEW_ID])
    events = _grant_events(engine)
    assert len(events) == 1
    assert (events[0].discord_user_id, events[0].actor_method, events[0].actor_discord_user_id) == (
        NEW_ID, "discord", ADMIN_ID,
    )
    assert len(events[0].actor_session_hash) == 64
    with Session(engine) as db:
        original = db.get(DiscordViewer, NEW_ID)
        assert original is not None
        created_at = original.created_at
    duplicate = client.post("/admin/users", data={"discord_user_id": NEW_ID, "csrf_token": _csrf(created.text)})
    assert duplicate.status_code == 200
    assert "既に" in duplicate.text or "すでに" in duplicate.text
    assert _stored_ids(engine) == sorted([STORED_ID, NEW_ID])
    with Session(engine) as db:
        retained = db.get(DiscordViewer, NEW_ID)
        assert retained is not None
        assert retained.created_at == created_at


@pytest.mark.parametrize("user_id", (ADMIN_ID, CONFIGURED_ID))
def test_configured_allowed_ids_are_not_added_as_database_rows(
    client: TestClient, engine: Engine, user_id: str,
) -> None:
    token = _admin_token(client)

    response = client.post("/admin/users", data={"discord_user_id": user_id, "csrf_token": token})

    assert response.status_code == 200
    assert "既に" in response.text or "すでに" in response.text
    assert _stored_ids(engine) == [STORED_ID]


def test_read_only_allows_list_but_disables_submission_and_blocks_post(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _admin_token(client)
    monkeypatch.setenv("APP_READ_ONLY", "true")

    page = client.get("/admin/users")

    assert page.status_code == 200
    _assert_private(page)
    buttons = _FormParser(page.text).submit_buttons
    assert len(buttons) == 1 and "disabled" in buttons[0]
    assert "読取専用" in page.text or "読み取り専用" in page.text
    for supplied_token in (token, "forged-token"):
        blocked = client.post("/admin/users", data={"discord_user_id": NEW_ID, "csrf_token": supplied_token})
        assert blocked.status_code == 503
        assert "読取専用" in blocked.text or "読み取り専用" in blocked.text
    assert _stored_ids(engine) == [STORED_ID]


def test_discord_admin_home_requires_login_and_protects_ids(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:

    page = client.get("/admin/", follow_redirects=False)

    assert page.status_code == 302
    assert all(user_id not in page.text for user_id in (ADMIN_ID, CONFIGURED_ID, STORED_ID))
    blocked = client.get("/admin/users", follow_redirects=False)
    assert blocked.status_code == 302
    _assert_private(blocked)
    client.get("/_test/session/admin")
    allowed = client.get("/admin/users")
    assert allowed.status_code == 200
    assert all(user_id in allowed.text for user_id in (ADMIN_ID, CONFIGURED_ID, STORED_ID))


def test_verified_admin_can_add_administrator_with_explicit_role(
    client: TestClient, engine: Engine,
) -> None:
    client.get("/_test/session/admin")
    users = client.get("/admin/users")
    assert users.status_code == 200

    added = client.post("/admin/users", data={
        "discord_user_id": NEW_ID, "csrf_token": _csrf(users.text),
        "role": "admin", "is_admin": "true", "admin_authenticated": "true",
    })

    assert added.status_code == 200
    assert _stored_ids(engine) == sorted([STORED_ID, NEW_ID])
    row = next(row for row in _FormParser(added.text).rows if NEW_ID in row)
    assert "管理者" in row
    assert "登録元" not in added.text and "追加した認証" not in added.text
    with Session(engine) as db:
        assert db.get(DiscordViewer, NEW_ID).is_admin is True
        assert db.scalar(select(DiscordViewerGrantEvent)).new_role == "admin"
    state = client.get("/_test/session-state").json()
    assert state["admin_authenticated"] is True


def test_discord_viewer_and_forged_role_cannot_add_or_escalate(
    client: TestClient, engine: Engine,
) -> None:
    client.get("/_test/session/viewer")
    state_before = client.get("/_test/session-state").json()
    assert state_before["authenticated"] is True
    assert state_before["admin_authenticated"] is False

    blocked = client.post("/admin/users", data={
        "discord_user_id": NEW_ID, "csrf_token": state_before["csrf_token"],
        "role": "admin", "is_admin": "true", "admin_authenticated": "true",
    }, follow_redirects=False)

    assert blocked.status_code == 302
    assert blocked.headers["location"] == "/admin/login"
    assert _stored_ids(engine) == [STORED_ID]
    assert client.get("/_test/session-state").json() == state_before
