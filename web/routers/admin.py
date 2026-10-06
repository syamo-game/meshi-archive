from __future__ import annotations

import logging
import os
from collections.abc import Generator

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from db.database import SessionLocal
from services.import_service import (
    ImportValidationFailure,
    apply_import_batch,
    parse_csv_bytes,
    stage_import,
)
from web.auth import is_admin
from web.csrf import get_csrf_token, verify_csrf_token
from web.password_login import authenticate_password
from web.read_only import is_read_only, require_writable


logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _render_admin(
    request: Request,
    *,
    errors: list[str] | None = None,
    preview: object | None = None,
    result: object | None = None,
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "disabled": not bool(ADMIN_PASSWORD),
            "read_only": is_read_only(),
            "errors": errors or [],
            "preview": preview,
            "result": result,
            "csrf_token": get_csrf_token(request),
        },
        status_code=status_code,
    )


@router.get("/")
def admin_home(request: Request) -> Response:
    if not ADMIN_PASSWORD:
        return _render_admin(request)
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    return _render_admin(request)


@router.get("/login")
def admin_login_page(request: Request) -> Response:
    if not ADMIN_PASSWORD:
        return RedirectResponse("/admin", status_code=302)
    if is_admin(request):
        return RedirectResponse("/admin", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="admin_login.html",
        context={"error": None, "csrf_token": get_csrf_token(request)},
    )


@router.post("/login")
def admin_login(
    request: Request,
    password: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf_token(request, csrf_token)
    result = authenticate_password(request, db, "admin", password, ADMIN_PASSWORD)
    if result.authenticated:
        request.session["admin_authenticated"] = True
        return RedirectResponse("/admin", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="admin_login.html",
        context={
            "error": result.error,
            "csrf_token": get_csrf_token(request),
        },
        status_code=result.status_code,
        headers={"Retry-After": str(result.retry_after)} if result.retry_after else None,
    )


@router.get("/logout")
def admin_logout(request: Request) -> Response:
    request.session.pop("admin_authenticated", None)
    return RedirectResponse("/admin/login", status_code=302)


@router.post("/import/validate")
async def validate_import(
    request: Request,
    csrf_token: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    require_writable()
    verify_csrf_token(request, csrf_token)
    raw = await file.read()
    filename = file.filename or "upload.csv"
    try:
        parsed = parse_csv_bytes(raw, filename)
        preview = stage_import(db, parsed)
        return _render_admin(request, preview=preview)
    except ImportValidationFailure as exc:
        return _render_admin(request, errors=exc.errors, status_code=400)
    except Exception as exc:
        db.rollback()
        logger.exception(
            "CSV validation failed: filename=%s size=%s error=%s",
            filename,
            len(raw),
            exc,
        )
        return _render_admin(
            request,
            errors=[f"CSV検証中にエラーが発生しました。filename={filename}"],
            status_code=500,
        )


@router.post("/import/{batch_id}/apply")
def apply_import(
    batch_id: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    require_writable()
    verify_csrf_token(request, csrf_token)
    try:
        result = apply_import_batch(db, batch_id)
        return _render_admin(request, result=result)
    except Exception as exc:
        db.rollback()
        logger.exception("CSV apply failed: batch_id=%s error=%s", batch_id, exc)
        return _render_admin(
            request,
            errors=[f"CSV適用に失敗しました。batch_id={batch_id}"],
            status_code=500,
        )
