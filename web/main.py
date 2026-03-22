import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from db.database import init_db
from web.routers import admin, home

logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    # Generate a random key for development; sessions will not survive restarts
    SECRET_KEY = os.urandom(32).hex()
    logger.warning("SECRET_KEY env var is not set. Sessions will not survive process restarts.")

# Enable https_only cookies when running behind HTTPS (set HTTPS_ONLY=true in production)
_HTTPS_ONLY = os.getenv("HTTPS_ONLY", "false").lower() == "true"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add defensive security headers to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        if _HTTPS_ONLY:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


app = FastAPI(title="Meshi Database", docs_url=None, redoc_url=None)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="meshi_session",
    same_site="lax",
    https_only=_HTTPS_ONLY,
    max_age=86400,     # 24 hours
)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()


app.include_router(home.router)
app.include_router(admin.router, prefix="/admin")
