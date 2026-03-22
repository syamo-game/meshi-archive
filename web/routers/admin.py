import csv
import io
import os
import re
from typing import Optional

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
    if password == ADMIN_PASSWORD:
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
    elif not (file.filename or "").lower().endswith(".csv"):
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
                csv_ids = {_normalize_message_id(r["message_id"]) for r in rows}
                updated = inserted = deleted = 0
                try:
                    for row in rows:
                        mid = _normalize_message_id(row["message_id"])
                        shop_name = _sanitize_text(row.get("shop.name")) or "Unknown"
                        area = _sanitize_text(row.get("shop.area"))
                        category = _sanitize_text(row.get("shop.category"))
                        url = _sanitize_url(row.get("url"))
                        is_visited = (
                            str(row.get("status.is_visited", "")).strip().lower()
                            in ("true", "1", "yes")
                        )

                        if not db.query(Message).filter_by(message_id=mid).first():
                            db.add(Message(message_id=mid, is_target=True))

                        existing = db.query(Shop).filter_by(message_id=mid).first()
                        if existing:
                            existing.shop_name = shop_name
                            existing.area = area
                            existing.category = category
                            existing.url = url
                            existing.is_visited = is_visited
                            updated += 1
                        else:
                            db.add(
                                Shop(
                                    message_id=mid,
                                    shop_name=shop_name,
                                    area=area,
                                    category=category,
                                    url=url,
                                    is_visited=is_visited,
                                )
                            )
                            inserted += 1

                    for shop in db.query(Shop).all():
                        if shop.message_id not in csv_ids:
                            db.delete(shop)
                            deleted += 1

                    db.commit()
                    result = {"updated": updated, "inserted": inserted, "deleted": deleted}
                except Exception as e:
                    db.rollback()
                    errors.append(f"インポートに失敗しました: {e}")
        except Exception as e:
            errors.append(f"CSV の読み込みに失敗しました: {e}")

    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "errors": errors, "result": result},
    )
