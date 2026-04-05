import csv
import hmac
import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import Message, Shop

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
_MAX_ROWS = 5_000
_SAFE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_CSV_INJECT_CHARS = frozenset("=+-@\t\r")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _is_admin(request: Request) -> bool:
    return bool(request.session.get("admin_authenticated"))


def _sanitize_text(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    s = val.strip()
    while s and s[0] in _CSV_INJECT_CHARS:
        s = s[1:].strip()
    return s or None


def _sanitize_url(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    s = val.strip()
    return s if _SAFE_URL_RE.match(s) else None


def _normalize_message_id(val: str) -> str:
    """Convert scientific-notation IDs (e.g. '1.47725e+18') to full integer strings."""
    s = str(val).strip()
    if "e" in s.lower() or ("." in s and s.replace(".", "", 1).isdigit()):
        try:
            return str(int(float(s)))
        except ValueError:
            pass
    return s


def _parse_timestamp(val: str) -> Optional[datetime]:
    """Parse @timestamp string to timezone-aware datetime. Returns None on failure."""
    s = val.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Admin home
# ---------------------------------------------------------------------------

@router.get("/")
def admin_home(request: Request):
    if not ADMIN_PASSWORD:
        return templates.TemplateResponse(
            "admin.html", {"request": request, "disabled": True}
        )
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    return templates.TemplateResponse("admin.html", {"request": request})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.get("/login")
def admin_login_page(request: Request):
    if not ADMIN_PASSWORD:
        return RedirectResponse("/admin", status_code=302)
    if _is_admin(request):
        return RedirectResponse("/admin", status_code=302)
    return templates.TemplateResponse(
        "admin_login.html", {"request": request, "error": None}
    )


@router.post("/login")
def admin_login(request: Request, password: str = Form(...)):
    if ADMIN_PASSWORD and hmac.compare_digest(password, ADMIN_PASSWORD):
        request.session["admin_authenticated"] = True
        return RedirectResponse("/admin", status_code=302)
    return templates.TemplateResponse(
        "admin_login.html",
        {"request": request, "error": "パスワードが正しくありません。"},
        status_code=401,
    )


@router.get("/logout")
def admin_logout(request: Request):
    request.session.pop("admin_authenticated", None)
    return RedirectResponse("/admin/login", status_code=302)


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

@router.post("/import")
async def import_csv(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)

    raw = await file.read()
    errors: list[str] = []
    result: Optional[dict] = None

    if len(raw) > _MAX_FILE_BYTES:
        errors.append(
            f"ファイルサイズが上限を超えています（最大 {_MAX_FILE_BYTES // 1024 // 1024} MB）。"
        )
    elif not (file.filename or "").lower().endswith(".csv") or (
        file.content_type
        and file.content_type not in ("text/csv", "application/csv", "application/octet-stream", "text/plain")
    ):
        errors.append("CSV ファイルのみアップロードできます。")
    else:
        try:
            text = raw.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            fieldnames = set(reader.fieldnames or [])

            required = {"message_id", "shop.name"}
            if not required.issubset(fieldnames):
                errors.append(f"CSV に必須列が不足しています: {required}")
            elif len(rows) > _MAX_ROWS:
                errors.append(f"行数が上限を超えています（最大 {_MAX_ROWS:,} 行）。")
            else:
                updated = inserted = deleted = 0
                try:
                    touched_ids: set[int] = set()

                    for row in rows:
                        mid = _normalize_message_id(row["message_id"])
                        shop_name = _sanitize_text(row.get("shop.name")) or "Unknown"
                        area = _sanitize_text(row.get("shop.area"))
                        category = _sanitize_text(row.get("shop.category"))
                        url = _sanitize_url(row.get("url"))
                        # B-3: parse @timestamp to restore created_at when available
                        created_at = _parse_timestamp(row.get("@timestamp", ""))
                        # B-5: empty is_visited string keeps the existing value (resolved per branch)
                        raw_visited = str(row.get("status.is_visited", "")).strip().lower()
                        is_visited_explicit = raw_visited in ("true", "1", "yes") if raw_visited else None

                        # Ensure parent Message row exists
                        if not db.query(Message).filter_by(message_id=mid).first():
                            db.add(Message(message_id=mid, is_target=True))

                        # Upsert: prefer matching by _id, fall back to (message_id, shop_name)
                        existing: Optional[Shop] = None
                        raw_id = str(row.get("_id", "")).strip()
                        if raw_id.isdigit():
                            existing = db.query(Shop).filter_by(id=int(raw_id)).first()
                        if existing is None:
                            existing = (
                                db.query(Shop)
                                .filter_by(message_id=mid, shop_name=shop_name)
                                .first()
                            )

                        if existing:
                            existing.shop_name = shop_name
                            existing.area = area
                            existing.category = category
                            existing.url = url
                            # B-5: only update is_visited when the CSV value is explicit
                            if is_visited_explicit is not None:
                                existing.is_visited = is_visited_explicit
                            # B-3: only update created_at when the CSV value is valid
                            if created_at is not None:
                                existing.created_at = created_at
                            db.flush()
                            touched_ids.add(existing.id)
                            updated += 1
                        else:
                            new_shop = Shop(
                                message_id=mid,
                                shop_name=shop_name,
                                area=area,
                                category=category,
                                url=url,
                                # B-5: default False for new records when field is absent
                                is_visited=is_visited_explicit if is_visited_explicit is not None else False,
                            )
                            # B-3: set created_at only when explicitly provided; let the model default handle otherwise
                            if created_at is not None:
                                new_shop.created_at = created_at
                            db.add(new_shop)
                            db.flush()
                            touched_ids.add(new_shop.id)
                            inserted += 1

                    # Delete shops that were not present in the CSV
                    for shop in db.query(Shop).all():
                        if shop.id not in touched_ids:
                            db.delete(shop)
                            deleted += 1

                    db.commit()
                    result = {"updated": updated, "inserted": inserted, "deleted": deleted}
                except Exception as e:
                    db.rollback()
                    logger.error("CSV import DB error: %s", e)
                    errors.append("インポート中にエラーが発生しました。管理者に連絡してください。")
        except Exception as e:
            logger.error("CSV parse error: %s", e)
            errors.append("CSV ファイルの読み込みに失敗しました。ファイル形式を確認してください。")

    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "errors": errors, "result": result},
    )
