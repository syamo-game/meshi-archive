from __future__ import annotations

import ipaddress
import os
import re


def anonymous_read_enabled() -> bool:
    return (
        os.getenv("APP_ENV", "development") == "development"
        and os.getenv("ALLOW_ANONYMOUS_READ", "false").lower() == "true"
    )


def validate_web_security() -> None:
    environment: str = os.getenv("APP_ENV", "development")
    if environment not in {"development", "production"}:
        raise RuntimeError("APP_ENV must be development or production")
    if environment != "production":
        return
    errors: list[str] = []
    for name, minimum in (("SECRET_KEY", 32), ("WEB_PASSWORD", 16), ("ADMIN_PASSWORD", 24)):
        if len(os.getenv(name, "")) < minimum:
            errors.append(f"{name} must contain at least {minimum} characters")
    if os.getenv("HTTPS_ONLY", "false").lower() != "true":
        errors.append("HTTPS_ONLY must be true")
    if os.getenv("ALLOW_ANONYMOUS_READ", "false").lower() != "false":
        errors.append("ALLOW_ANONYMOUS_READ must be false")
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
        raise RuntimeError("Production security configuration is invalid: " + "; ".join(errors))


def validate_bot_security() -> None:
    if not os.getenv("DISCORD_TOKEN"):
        raise RuntimeError("DISCORD_TOKEN is not configured")
    if not re.fullmatch(r"[0-9]{17,20}", os.getenv("ADMIN_USER_ID", "")):
        raise RuntimeError("ADMIN_USER_ID must be a Discord user ID")
