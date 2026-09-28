from __future__ import annotations

from fastapi import Request


def is_admin(request: Request) -> bool:
    return bool(request.session.get("admin_authenticated"))
