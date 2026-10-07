from __future__ import annotations

import secrets

import pytest
from starlette.requests import Request

from services.security_config import validate_bot_security, validate_web_security
from web.routers import home


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_ADMIN_USER_IDS", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", secrets.token_urlsafe(32))
    monkeypatch.setenv("WEB_PASSWORD", secrets.token_urlsafe(32))
    monkeypatch.setenv("ADMIN_PASSWORD", secrets.token_urlsafe(32))
    monkeypatch.setenv("ADMIN_USER_ID", "123456789012345678")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "synthetic-client-secret")
    monkeypatch.setenv("DISCORD_REDIRECT_URI", "https://example.test/auth/discord/callback")
    monkeypatch.setenv("HTTPS_ONLY", "true")
    monkeypatch.setenv("ALLOW_ANONYMOUS_READ", "false")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1,172.19.0.2")


def test_valid_production_configuration(production: None) -> None:
    validate_web_security()


@pytest.mark.parametrize("name", ("SECRET_KEY", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI", "FORWARDED_ALLOW_IPS"))
def test_production_rejects_missing_settings(production: None, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(RuntimeError, match=name):
        validate_web_security()


@pytest.mark.parametrize(("name", "value"), (
    ("SECRET_KEY", "short-secret"), ("DISCORD_REDIRECT_URI", "http://example.test/callback"),
    ("HTTPS_ONLY", "false"),
    ("ALLOW_ANONYMOUS_READ", "true"), ("FORWARDED_ALLOW_IPS", "*"),
    ("FORWARDED_ALLOW_IPS", "0.0.0.0/0"), ("FORWARDED_ALLOW_IPS", "::/0"),
    ("APP_ENV", "prodution"),
))
def test_production_rejects_unsafe_settings(
    production: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name) as error:
        validate_web_security()
    assert "short-secret" not in str(error.value)
    assert "short-password" not in str(error.value)


def test_missing_viewer_password_never_opens_production(monkeypatch: pytest.MonkeyPatch) -> None:
    request = Request({"type": "http", "session": {}})
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_READ", "true")
    assert not home._is_authenticated(request)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("ALLOW_ANONYMOUS_READ")
    assert not home._is_authenticated(request)
    monkeypatch.setenv("ALLOW_ANONYMOUS_READ", "true")
    assert not home._is_authenticated(request)
    monkeypatch.setenv("ALLOW_ANONYMOUS_READ", "false")
    request.session["admin_authenticated"] = True
    assert home._is_authenticated(request)


@pytest.mark.parametrize("admin_id", ("", "anyone", "123", "123456789012345678"))
def test_bot_requires_an_explicit_discord_user(monkeypatch: pytest.MonkeyPatch, admin_id: str) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", secrets.token_urlsafe(32))
    monkeypatch.setenv("ADMIN_USER_ID", admin_id)
    if admin_id == "123456789012345678":
        validate_bot_security()
    else:
        with pytest.raises(RuntimeError, match="ADMIN_USER_ID"):
            validate_bot_security()


@pytest.mark.parametrize("environment", ("development", "production"))
def test_invalid_web_admin_configuration_prevents_startup_in_every_environment(
    production: None, monkeypatch: pytest.MonkeyPatch, environment: str,
) -> None:
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("WEB_ADMIN_USER_IDS", "456789012345678901,not-an-id")
    with pytest.raises(RuntimeError, match="WEB_ADMIN_USER_IDS"):
        validate_web_security()


def test_valid_multiple_web_admins_are_accepted_without_changing_bot_admin(
    production: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USER_ID", "123456789012345678")
    monkeypatch.setenv("WEB_ADMIN_USER_IDS", "456789012345678901,567890123456789012")
    monkeypatch.setenv("DISCORD_TOKEN", secrets.token_urlsafe(32))
    validate_web_security()
    validate_bot_security()
    monkeypatch.setenv("ADMIN_USER_ID", "")
    with pytest.raises(RuntimeError, match="ADMIN_USER_ID"):
        validate_bot_security()
