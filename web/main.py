import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from db.database import SessionLocal, init_db
from services.login_throttle import purge_expired_attempts
from services.security_config import validate_web_security
from web.discord_session import DiscordGrantMiddleware
from web.routers import admin, auth_discord, home, review, shop_images
from web.routers import review_suggestions

logger = logging.getLogger(__name__)
validate_web_security()

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    # Do not require a persistent key locally; production must set SECRET_KEY.
    SECRET_KEY = os.urandom(32).hex()
    logger.warning("SECRET_KEY env var is not set. Sessions will not survive process restarts.")

# Do not default to secure cookies because local HTTP cannot send them.
_HTTPS_ONLY = os.getenv("HTTPS_ONLY", "false").lower() == "true"
SESSION_MAX_AGE_SECONDS: int = 24 * 60 * 60


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
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
app.add_middleware(DiscordGrantMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="meshi_session",
    same_site="lax",
    https_only=_HTTPS_ONLY,
    max_age=SESSION_MAX_AGE_SECONDS,
)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()
    with SessionLocal() as db:
        purge_expired_attempts(db)


app.include_router(home.router)
app.include_router(shop_images.router)
app.include_router(auth_discord.router)
app.include_router(admin.router, prefix="/admin")
app.include_router(review.router)
app.include_router(review_suggestions.router)
