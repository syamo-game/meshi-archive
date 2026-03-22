import csv
import hmac
import io
import os
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import Shop

_CSV_INJECT_CHARS = frozenset("=+-@\t\r")

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

WEB_PASSWORD = os.getenv("WEB_PASSWORD")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _is_authenticated(request: Request) -> bool:
    if not WEB_PASSWORD:
        return True
    return bool(request.session.get("authenticated"))


def _build_shop_query(
    db: Session,
    area: Optional[str],
    status: Optional[str],
    q: Optional[str] = None,
):
    query = db.query(Shop)
    if q:
        query = query.filter(Shop.shop_name.ilike(f"%{q.strip()}%"))
    if area:
        query = query.filter(Shop.area == area)
    if status == "unvisited":
        query = query.filter(Shop.is_visited == False)  # noqa: E712
    elif status == "visited":
        query = query.filter(Shop.is_visited == True)  # noqa: E712
    return query.order_by(Shop.created_at.desc())


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

@router.get("/")
def home(
    request: Request,
    area: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)

    areas = sorted(
        a[0]
        for a in db.query(Shop.area).filter(Shop.area.isnot(None)).distinct().all()
        if a[0]
    )
    shops = _build_shop_query(db, area, status, q).all()

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "shops": shops,
            "all_areas": areas,
            "selected_area": area or "",
            "selected_status": status or "",
            "selected_q": q or "",
        },
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.get("/login")
def login_page(request: Request):
    if not WEB_PASSWORD:
        return RedirectResponse("/", status_code=302)
    if _is_authenticated(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    if WEB_PASSWORD and hmac.compare_digest(password, WEB_PASSWORD):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "パスワードが正しくありません。"},
        status_code=401,
    )


@router.get("/logout")
def logout(request: Request):
    request.session.pop("authenticated", None)
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

@router.get("/export.csv")
def export_csv(
    request: Request,
    area: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)

    shops = _build_shop_query(db, area, status, q).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "_id", "@timestamp", "message_id",
        "shop.name", "shop.area", "shop.category", "status.is_visited", "url",
    ])
    def _safe(val: Optional[str]) -> str:
        """Strip CSV injection prefix characters from exported text values."""
        s = (val or "").strip()
        while s and s[0] in _CSV_INJECT_CHARS:
            s = s[1:].strip()
        return s

    for s in shops:
        writer.writerow([
            s.id,
            s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else "",
            s.message_id,
            _safe(s.shop_name),
            _safe(s.area),
            _safe(s.category),
            s.is_visited,
            _safe(s.url),
        ])

    # UTF-8 BOM so Excel opens it correctly
    content = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename=meshi_database.csv"},
    )
