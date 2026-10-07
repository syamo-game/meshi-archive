from __future__ import annotations

import csv
import hmac
import io
import logging
import os
from collections.abc import Generator

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from db.database import SessionLocal
from db.models import Shop
from services.admin_sessions import get_admin_actor, revoke_admin_session
from services.discord_access import (
    AddDiscordViewerResult,
    ChangeDiscordViewerResult,
    DiscordAccessChangeError,
    change_discord_viewer,
    DiscordUserIdError,
    DiscordViewerEntry,
    normalize_discord_role,
    save_discord_user,
    add_discord_viewer,
    configured_discord_ids,
    configured_web_admin_ids,
    list_discord_viewers,
    normalize_discord_user_id,
)
from services.import_service import (
    CSV_UPDATE_COLUMNS,
    MAX_FILE_BYTES,
    ImportValidationFailure,
    ImportPreview,
    apply_csv_update,
    escape_csv_update_value,
    parse_csv_update,
    preview_csv_update,
)
from web.auth import is_admin
from web.csv_update_view import CsvChangeView, csv_change_views, csv_error_messages
from web.csrf import get_csrf_token, verify_csrf_token
from web.read_only import is_read_only, require_writable
from web.routers import auth_discord


logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))


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
    preview: ImportPreview | None = None,
    result: ImportPreview | None = None,
    changes: tuple[CsvChangeView, ...] = (),
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="admin_bulk.html",
        context={
            "read_only": is_read_only(),
            "errors": csv_error_messages(errors or []),
            "preview": preview,
            "result": result,
            "changes": changes,
            "csrf_token": get_csrf_token(request),
        },
        status_code=status_code,
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/")
def admin_home(request: Request, db: Session = Depends(get_db)) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    from web.routers.review import _counters
    return templates.TemplateResponse(request=request, name="admin.html", context={
        "counters": _counters(db, "all"), "csrf_token": get_csrf_token(request),
        "read_only": is_read_only(),
    }, headers={"Cache-Control": "private, no-store"})


@router.get("/shops/bulk")
def bulk_edit_page(request: Request) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    return _render_admin(request)


@router.get("/exports")
def exports_page(request: Request) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    return templates.TemplateResponse(request=request, name="admin_exports.html", context={
        "csrf_token": get_csrf_token(request),
    }, headers={"Cache-Control": "private, no-store"})


@router.get("/login")
def admin_login_page(request: Request) -> Response:
    if is_admin(request):
        return RedirectResponse("/admin", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="admin_login.html",
        context={"discord_login_enabled": auth_discord.is_configured(), "csrf_token": get_csrf_token(request)},
    )


@router.post("/logout")
def admin_logout(
    request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db),
) -> Response:
    verify_csrf_token(request, csrf_token)
    try:
        revoke_admin_session(db, request.session)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Admin session revocation failed: error_type=%s", type(exc).__name__)
        return Response("ログアウトできませんでした。再試行してください。", status_code=503)
    request.session.pop("admin_authenticated", None)
    request.session.pop("admin_session_token", None)
    return RedirectResponse("/admin/login", status_code=302)


def _render_discord_users(
    request: Request,
    *,
    entries: list[DiscordViewerEntry] | None = None,
    user_id: str = "",
    error: str | None = None,
    result: AddDiscordViewerResult | None = None,
    notice: str | None = None,
    change_result: ChangeDiscordViewerResult | None = None,
    storage_available: bool = True,
    selected_role: str = "viewer",
    adding: bool = False,
    editing_user_id: str | None = None,
    replacement_user_id: str = "",
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={
            "entries": entries,
            "user_id": user_id,
            "error": error,
            "result": result, "notice": notice,
            "change_result": change_result,
            "selected_role": selected_role, "adding": adding,
            "editing_user_id": editing_user_id, "replacement_user_id": replacement_user_id,
            "read_only": is_read_only(),
            "storage_available": storage_available,
            "discord_configured": auth_discord.is_configured(),
            "csrf_token": get_csrf_token(request),
        },
        status_code=status_code,
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/users")
def discord_users(request: Request, db: Session = Depends(get_db)) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302, headers={"Cache-Control": "private, no-store"})
    try:
        entries = list_discord_viewers(db, configured_discord_ids(auth_discord.ADMIN_USER_ID), auth_discord.ADMIN_USER_ID)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Discord viewer list failed: error_type=%s", type(exc).__name__)
        return _render_discord_users(
            request, error="利用ユーザーを読み込めませんでした。時間をおいて再度お試しください。",
            storage_available=False, status_code=503,
        )
    notice: object = request.session.pop("discord_user_notice", None)
    return _render_discord_users(request, entries=entries, notice=notice if isinstance(notice, str) else None)


@router.post("/users")
def add_discord_user(
    request: Request, discord_user_id: str = Form(""), role: str = Form("viewer"),
    csrf_token: str = Form(""), db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302, headers={"Cache-Control": "private, no-store"})
    require_writable()
    verify_csrf_token(request, csrf_token)
    entries: list[DiscordViewerEntry] | None = None
    try:
        configured_ids: set[str] = configured_discord_ids(auth_discord.ADMIN_USER_ID)
        actor = get_admin_actor(db, request.session, admin_discord_ids=configured_web_admin_ids(auth_discord.ADMIN_USER_ID))
        if actor is None:
            request.session.clear()
            return RedirectResponse("/admin/login", status_code=302, headers={"Cache-Control": "private, no-store"})
        entries = list_discord_viewers(db, configured_ids, auth_discord.ADMIN_USER_ID)
        result = add_discord_viewer(db, discord_user_id, configured_ids, actor, role=normalize_discord_role(role))
        entries = list_discord_viewers(db, configured_ids, auth_discord.ADMIN_USER_ID)
    except (DiscordUserIdError, DiscordAccessChangeError) as exc:
        db.rollback()
        return _render_discord_users(request, entries=entries, user_id=discord_user_id,
                                    selected_role=role, adding=True, error=str(exc), status_code=422)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Discord user addition could not be confirmed: user_id=%s role=%s error_type=%s",
                     discord_user_id, role, type(exc).__name__)
        return _render_discord_users(
            request, entries=entries, user_id=discord_user_id, selected_role=role, adding=True,
            error="追加結果を確認できませんでした。一覧を再読み込みしてください。",
            storage_available=False, status_code=503,
        )
    if result.created:
        role_label: str = "管理者" if result.is_admin else "閲覧ユーザー"
        request.session["discord_user_notice"] = f"ユーザー {result.user_id} を{role_label}として追加しました。"
    else:
        request.session["discord_user_notice"] = f"ユーザー {result.user_id} はすでに登録されています。権限を変える場合は編集してください。"
    return RedirectResponse("/admin/users", status_code=303, headers={"Cache-Control": "private, no-store"})


@router.post("/users/{discord_user_id}/change")
def change_discord_user(
    discord_user_id: str, request: Request,
    expected_generation: str = Form(""), replacement_user_id: str = Form(""),
    role: str = Form("viewer"), action: str = Form(""), csrf_token: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302, headers={"Cache-Control": "private, no-store"})
    require_writable()
    verify_csrf_token(request, csrf_token)
    entries: list[DiscordViewerEntry] | None = None
    try:
        actor = get_admin_actor(db, request.session, admin_discord_ids=configured_web_admin_ids(auth_discord.ADMIN_USER_ID))
        if actor is None:
            request.session.clear()
            return RedirectResponse("/admin/login", status_code=302, headers={"Cache-Control": "private, no-store"})
        configured_ids: set[str] = configured_discord_ids(auth_discord.ADMIN_USER_ID)
        entries = list_discord_viewers(db, configured_ids, auth_discord.ADMIN_USER_ID)
        if action == "save":
            result = save_discord_user(
                db, discord_user_id, configured_ids, actor, expected_generation=expected_generation,
                replacement_user_id=replacement_user_id, role=normalize_discord_role(role),
            )
        elif action in {"revoke", "replace"}:
            result = change_discord_viewer(
                db, discord_user_id, configured_ids, actor, expected_generation=expected_generation,
                replacement_user_id=replacement_user_id if action == "replace" else None,
            )
        else:
            return _render_discord_users(request, entries=entries, error="変更操作が不正です。", status_code=422)
        entries = list_discord_viewers(db, configured_ids, auth_discord.ADMIN_USER_ID)
    except (DiscordUserIdError, DiscordAccessChangeError) as exc:
        db.rollback()
        error: str = str(exc)
        return _render_discord_users(
            request, entries=entries, error=error, editing_user_id=discord_user_id,
            replacement_user_id=replacement_user_id, selected_role=role, status_code=409,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Discord user change failed: user_id=%s action=%s error_type=%s", discord_user_id, action, type(exc).__name__)
        return _render_discord_users(
            request, entries=entries, error="変更結果を確認できませんでした。一覧を再読み込みしてください。",
            storage_available=False, editing_user_id=discord_user_id,
            replacement_user_id=replacement_user_id, selected_role=role, status_code=503,
        )
    if result.saved:
        request.session["discord_user_notice"] = f"ユーザー {result.replacement_user_id or result.user_id} の変更を保存しました。"
    elif result.replacement_user_id:
        request.session["discord_user_notice"] = f"ユーザー {result.user_id} を {result.replacement_user_id} に変更しました。"
    else:
        request.session["discord_user_notice"] = f"ユーザー {result.user_id} の利用許可を削除しました。"
    return RedirectResponse("/admin/users", status_code=303, headers={"Cache-Control": "private, no-store"})


@router.get("/import/update/template.csv")
def download_csv_update_template(request: Request, db: Session = Depends(get_db)) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    output = io.StringIO()
    writer = csv.writer(output)
    columns = ["_id", "shop.version", "message_id", *CSV_UPDATE_COLUMNS]
    writer.writerow(columns)
    shops = db.query(Shop).options(selectinload(Shop.mentions)).order_by(Shop.id).all()
    for shop in shops:
        mention = shop.primary_mention()
        if mention is None:
            continue
        values: list[str | int] = [shop.id, shop.version, mention.message_id]
        for field in CSV_UPDATE_COLUMNS.values():
            value = getattr(shop, field)
            if isinstance(value, bool):
                values.append("true" if value else "false")
            elif value is None:
                values.append("")
            elif isinstance(value, str):
                values.append(escape_csv_update_value(value))
            else:
                values.append(str(value))
        writer.writerow(values)
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="meshi_update_template.csv"',
            "Cache-Control": "private, no-store, max-age=0",
        },
    )


@router.post("/import/validate")
def validate_import(
    request: Request,
    csrf_token: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    require_writable()
    verify_csrf_token(request, csrf_token)
    request.session.pop("csv_update_sha256", None)
    raw = file.file.read(MAX_FILE_BYTES + 1)
    filename = file.filename or "upload.csv"
    try:
        parsed = parse_csv_update(raw, filename)
        preview = preview_csv_update(db, parsed)
        changes = csv_change_views(db, preview)
        request.session["csv_update_sha256"] = parsed.sha256
        return _render_admin(request, preview=preview, changes=changes)
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
            errors=["CSVの確認中にエラーが発生しました。時間をおいてもう一度お試しください。"],
            status_code=500,
        )


@router.post("/import/update/apply")
def apply_update(
    request: Request,
    csrf_token: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    require_writable()
    verify_csrf_token(request, csrf_token)
    try:
        parsed = parse_csv_update(file.file.read(MAX_FILE_BYTES + 1), file.filename or "upload.csv")
        expected_hash = request.session.get("csv_update_sha256")
        if not isinstance(expected_hash, str) or not hmac.compare_digest(expected_hash, parsed.sha256):
            raise ImportValidationFailure(["検証したCSVと一致しません。同じCSVを選択するか、再検証してください。"])
        result = apply_csv_update(db, parsed)
        request.session.pop("csv_update_sha256", None)
    except ImportValidationFailure as exc:
        db.rollback()
        return _render_admin(request, errors=exc.errors, status_code=409)
    except Exception as exc:
        db.rollback()
        logger.exception("CSV update failed: filename=%s error=%s", file.filename, exc)
        return _render_admin(
            request,
            errors=["CSV差分更新の結果を確認できませんでした。最新CSVを出力して保存結果を確認してください。"],
            status_code=500,
        )
    return _render_admin(request, result=result)


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
    return _render_admin(
        request,
        errors=["Web画面からの全置換は停止しました。最新CSVを取得し、差分更新として検証してください。"],
        status_code=410,
    )
