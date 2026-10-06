import logging
import os
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

router = APIRouter()

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
_TOKEN_URL = "https://discord.com/api/oauth2/token"
_USER_URL = "https://discord.com/api/users/@me"
_HTTP_TIMEOUT = 10.0


def is_configured() -> bool:
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI)


def _allowed_ids() -> set[str]:
    raw = os.getenv("DISCORD_ALLOWED_USER_IDS", "")
    ids = {s.strip() for s in raw.split(",") if s.strip()}
    if ADMIN_USER_ID:
        ids.add(ADMIN_USER_ID)
    return ids


@router.get("/auth/discord")
def discord_login(request: Request):
    if not is_configured():
        return RedirectResponse("/login?error=discord_not_configured", status_code=302)
    state = secrets.token_urlsafe(32)
    request.session["discord_oauth_state"] = state
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
        "prompt": "none",
    }
    return RedirectResponse(f"{_AUTHORIZE_URL}?{urlencode(params)}", status_code=302)


@router.get("/auth/discord/callback")
async def discord_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    if error:
        return RedirectResponse(f"/login?error=discord_{error}", status_code=302)
    if not code or not state:
        return RedirectResponse("/login?error=discord_missing_params", status_code=302)

    saved_state = request.session.pop("discord_oauth_state", None)
    if not saved_state or saved_state != state:
        return RedirectResponse("/login?error=discord_state_mismatch", status_code=302)

    if not is_configured():
        return RedirectResponse("/login?error=discord_not_configured", status_code=302)

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            token_resp = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": DISCORD_REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_resp.status_code != 200:
                logger.warning("Discord token exchange failed: %s %s", token_resp.status_code, token_resp.text)
                return RedirectResponse("/login?error=discord_token_failed", status_code=302)
            access_token = token_resp.json().get("access_token")
            if not access_token:
                return RedirectResponse("/login?error=discord_no_token", status_code=302)

            user_resp = await client.get(
                _USER_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_resp.status_code != 200:
                logger.warning("Discord user fetch failed: %s %s", user_resp.status_code, user_resp.text)
                return RedirectResponse("/login?error=discord_user_failed", status_code=302)
            user = user_resp.json()
    except httpx.HTTPError as e:
        logger.error("Discord OAuth network error: %s", e)
        return RedirectResponse("/login?error=discord_network", status_code=302)

    user_id = str(user.get("id") or "")
    username = user.get("username") or "unknown"
    if not user_id:
        return RedirectResponse("/login?error=discord_no_user_id", status_code=302)

    if user_id not in _allowed_ids():
        logger.info("Discord login denied for unregistered user id=%s username=%s", user_id, username)
        return RedirectResponse("/login?error=discord_unauthorized", status_code=302)

    request.session["authenticated"] = True
    request.session["discord_user_id"] = user_id
    request.session["discord_username"] = username
    if ADMIN_USER_ID and user_id == ADMIN_USER_ID:
        request.session["admin_authenticated"] = True
        logger.info("Discord admin login: id=%s username=%s", user_id, username)
    else:
        logger.info("Discord viewer login: id=%s username=%s", user_id, username)

    return RedirectResponse("/", status_code=302)
