from __future__ import annotations

import re
import secrets
from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from db.models import Base

from web.routers import admin, home


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    monkeypatch.setattr(home, "WEB_PASSWORD", "login-test-password")
    monkeypatch.setattr(admin, "ADMIN_PASSWORD", "login-test-password")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(32))
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["172.19.0.2"])
    app.include_router(home.router)
    app.include_router(admin.router, prefix="/admin")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)

    def get_db() -> Generator[Session, None, None]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[home.get_db] = get_db
    app.dependency_overrides[admin.get_db] = get_db
    with TestClient(app, client=("192.0.2.1", 50000)) as test_client:
        yield test_client
    engine.dispose()


def csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@pytest.mark.parametrize("path", ("/login", "/admin/login"))
@pytest.mark.parametrize("password", (None, "", "日本語の誤ったパスワード", "wrong-password"))
def test_invalid_login_returns_japanese_form_error(
    client: TestClient, path: str, password: str | None,
) -> None:
    token = csrf_token(client.get(path).text)
    data: dict[str, str] = {"csrf_token": token}
    if password is not None:
        data["password"] = password

    response = client.post(path, data=data, follow_redirects=False)

    assert response.status_code == (400 if not password else 401)
    assert response.headers["content-type"].startswith("text/html")
    assert f'action="{path}"' in response.text
    assert ('パスワードを入力してください。' if not password else 'パスワードが正しくありません。') in response.text
    assert 'aria-invalid="true" aria-describedby="login-error"' in response.text
    assert csrf_token(response.text) == token
    assert 'type="password"' in response.text
    if password:
        assert f'value="{password}"' not in response.text
    assert client.get(path, follow_redirects=False).status_code == 200

    corrected = client.post(
        path, data={"password": "login-test-password", "csrf_token": token}, follow_redirects=False,
    )
    assert corrected.status_code == 302
    assert corrected.headers["location"] == ("/" if path == "/login" else "/admin")


@pytest.mark.parametrize("path", ("/login", "/admin/login"))
@pytest.mark.parametrize("password", ("login-test-password", "日本語を含む正しいパスワード"))
def test_login_accepts_configured_ascii_or_unicode_password(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, password: str,
) -> None:
    monkeypatch.setattr(home, "WEB_PASSWORD", password)
    monkeypatch.setattr(admin, "ADMIN_PASSWORD", password)
    token = csrf_token(client.get(path).text)

    response = client.post(
        path, data={"password": password, "csrf_token": token}, follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == ("/" if path == "/login" else "/admin")
    assert client.get(path, follow_redirects=False).status_code == 302


@pytest.mark.parametrize("path", ("/login", "/admin/login"))
def test_password_limit_returns_html_and_cannot_be_bypassed_with_headers(client: TestClient, path: str) -> None:
    token: str = csrf_token(client.get(path).text)
    for index in range(5):
        response = client.post(path, data={"password": "wrong", "csrf_token": token},
                               headers={"X-Forwarded-For": f"198.51.100.{index + 1}"})
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert "秒後に再度お試しください" in response.text
    assert csrf_token(response.text) == token
    assert client.post(path, data={"password": "login-test-password", "csrf_token": token}).status_code == 429
    assert client.post(path, data={"password": "wrong", "csrf_token": "forged"}).status_code == 403


def test_trusted_proxy_separates_real_clients_and_ignores_forged_prefix(client: TestClient) -> None:
    with TestClient(client.app, client=("172.19.0.2", 50000)) as proxied:
        token: str = csrf_token(proxied.get("/login").text)
        for index in range(5):
            response = proxied.post("/login", data={"password": "wrong", "csrf_token": token},
                                   headers={"X-Forwarded-For": f"198.51.100.{index + 1}, 192.0.2.99"})
        assert response.status_code == 429
        response = proxied.post("/login", data={"password": "login-test-password", "csrf_token": token},
                               headers={"X-Forwarded-For": "192.0.2.100"}, follow_redirects=False)
        assert response.status_code == 302


def test_missing_password_still_shows_login_form(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home, "WEB_PASSWORD", None)
    monkeypatch.setenv("ALLOW_ANONYMOUS_READ", "false")
    assert client.get("/login", follow_redirects=False).status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_database_failure_does_not_authenticate_or_disclose_password(
    client: TestClient, caplog: pytest.LogCaptureFixture,
) -> None:
    empty_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    def unavailable_db() -> Generator[Session, None, None]:
        with Session(empty_engine) as db:
            yield db

    client.app.dependency_overrides[home.get_db] = unavailable_db
    token: str = csrf_token(client.get("/login").text)
    response = client.post("/login", data={"password": "login-test-password", "csrf_token": token})
    assert response.status_code == 503
    assert "現在ログインできません" in response.text
    assert "login-test-password" not in response.text + caplog.text
    assert "OperationalError" in caplog.text
    assert client.get("/login", follow_redirects=False).status_code == 200
    empty_engine.dispose()
