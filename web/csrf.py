import hmac
import secrets
from typing import Optional

from fastapi import HTTPException, Request

_CSRF_SESSION_KEY = "csrf_token"
_CSRF_HEADER = "x-csrf-token"


def get_csrf_token(request: Request) -> str:
    token = request.session.get(_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_CSRF_SESSION_KEY] = token
    return token


def verify_csrf_token(request: Request, token: Optional[str] = None) -> None:
    expected = request.session.get(_CSRF_SESSION_KEY)
    provided = token or request.headers.get(_CSRF_HEADER)
    if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
