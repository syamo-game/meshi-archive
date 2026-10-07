from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlparse

from services.discord_access import configured_discord_ids, configured_web_admin_ids


def validate_web_security() -> None:
    environment: str = os.getenv("APP_ENV", "development")
    if environment not in {"development", "production"}:
        raise RuntimeError("APP_ENV must be development or production")
    errors: list[str] = []
    if not configured_web_admin_ids(os.getenv("ADMIN_USER_ID")):
        errors.append("ADMIN_USER_ID or WEB_ADMIN_USER_IDS must configure at least one administrator")
    configured_discord_ids(os.getenv("ADMIN_USER_ID"))
    for name in ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"):
        if not os.getenv(name, "").strip():
            errors.append(f"{name} is required for Discord login")
    redirect_uri = urlparse(os.getenv("DISCORD_REDIRECT_URI", ""))
    if (
        redirect_uri.scheme not in ({"https"} if environment == "production" else {"http", "https"})
        or not redirect_uri.hostname or redirect_uri.username or redirect_uri.password
        or redirect_uri.query or redirect_uri.fragment
    ):
        errors.append("DISCORD_REDIRECT_URI must be an absolute callback URL with no credentials, query, or fragment")
    if os.getenv("ALLOW_ANONYMOUS_READ", "false").lower() != "false":
        errors.append("ALLOW_ANONYMOUS_READ must be false; Discord login is required")
    if environment == "production":
        if len(os.getenv("SECRET_KEY", "")) < 32:
            errors.append("SECRET_KEY must contain at least 32 characters")
        if os.getenv("HTTPS_ONLY", "false").lower() != "true":
            errors.append("HTTPS_ONLY must be true")
        trusted: str = os.getenv("FORWARDED_ALLOW_IPS", "")
        try:
            if not trusted:
                raise ValueError("missing trusted proxy")
            for entry in trusted.split(","):
                network = ipaddress.ip_network(entry.strip(), strict=False)
                if network.prefixlen == 0:
                    raise ValueError("unrestricted proxy")
        except ValueError:
            errors.append("FORWARDED_ALLOW_IPS must list explicit trusted proxy IPs or networks")
    if errors:
        raise RuntimeError("Web security configuration is invalid: " + "; ".join(errors))


def validate_bot_security() -> None:
    if not os.getenv("DISCORD_TOKEN"):
        raise RuntimeError("DISCORD_TOKEN is not configured")
    if not re.fullmatch(r"[0-9]{17,20}", os.getenv("ADMIN_USER_ID", "")):
        raise RuntimeError("ADMIN_USER_ID must be a Discord user ID")
