import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from db.database import init_db
from web.routers import admin, home

logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    # Generate a random key for development; sessions won't survive restarts
    SECRET_KEY = os.urandom(32).hex()
    logger.warning("SECRET_KEY env var is not set. Sessions will not survive process restarts.")

app = FastAPI(title="Meshi Database", docs_url=None, redoc_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="meshi_session",
    same_site="lax",
    https_only=False,  # Set True when served over HTTPS
    max_age=86400,     # 24 hours
)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()


app.include_router(home.router)
app.include_router(admin.router, prefix="/admin")
