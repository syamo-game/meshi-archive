from __future__ import annotations

import os

from fastapi import HTTPException


def is_read_only() -> bool:
    return os.getenv("APP_READ_ONLY", "false").lower() == "true"


def require_writable() -> None:
    if is_read_only():
        raise HTTPException(
            status_code=503,
            detail="現在は読取専用モードのため、変更できません。",
        )
