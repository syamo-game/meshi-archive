from __future__ import annotations

from collections.abc import Generator
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from web.routers import admin, auth_discord, home


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="synthetic-login-test-secret")
    app.include_router(home.router)
    app.include_router(admin.router, prefix="/admin")
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path", ("/login", "/admin/login"))
@pytest.mark.parametrize("configured", (True, False))
def test_login_shows_only_discord_entry_or_configuration_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, configured: bool,
) -> None:
    monkeypatch.setattr(auth_discord, "DISCORD_CLIENT_ID", "synthetic" if configured else "")
    monkeypatch.setattr(auth_discord, "DISCORD_CLIENT_SECRET", "synthetic" if configured else "")
    monkeypatch.setattr(auth_discord, "DISCORD_REDIRECT_URI", "http://localhost/auth/discord/callback")
    page = client.get(path)
    assert page.status_code == 200
    assert 'type="password"' not in page.text
    assert ('href="/auth/discord"' in page.text) is configured
    if not configured:
        assert "設定が必要" in page.text


@pytest.mark.parametrize("path", ("/login", "/admin/login"))
@pytest.mark.parametrize("password", ("", "wrong", "日本語のパスワード", "formerly-correct-password"))
def test_password_posts_are_disabled_and_cannot_authenticate(
    client: TestClient, path: str, password: str,
) -> None:
    response = client.post(path, data={"password": password, "csrf_token": "any-token"}, follow_redirects=False)
    assert response.status_code == 405
    assert 'meshi_session' not in response.cookies
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_proxy_headers_and_legacy_password_settings_do_not_restore_password_login(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_PASSWORD", "legacy-password")
    monkeypatch.setenv("ADMIN_PASSWORD", "legacy-password")
    monkeypatch.setenv("ALLOW_ANONYMOUS_READ", "true")
    for path in ("/login", "/admin/login"):
        response = client.post(path, data={"password": "legacy-password"}, headers={"X-Forwarded-For": "127.0.0.1"})
        assert response.status_code == 405
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_discord_error_is_escaped_and_does_not_echo_query_input(client: TestClient) -> None:
    page = client.get("/login", params={"error": '<script>alert("secret")</script>'})
    assert page.status_code == 200
    assert "ログインエラー" in page.text
    assert '<script>alert("secret")</script>' not in page.text


def test_missing_password_does_not_disable_discord_admin_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    assert client.get("/admin/login").status_code == 200
    assert client.get("/admin/", follow_redirects=False).status_code == 302
