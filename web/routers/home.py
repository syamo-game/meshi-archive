import csv
import hmac
import io
import os
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import Shop

_CSV_INJECT_CHARS = frozenset("=+-@\t\r")

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

WEB_PASSWORD = os.getenv("WEB_PASSWORD")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

PER_PAGE = 50


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


def _discord_base_url() -> Optional[str]:
    """Return base URL for Discord message jump links, or None if env vars are absent."""
    if DISCORD_GUILD_ID and DISCORD_CHANNEL_ID:
        return f"https://discord.com/channels/{DISCORD_GUILD_ID}/{DISCORD_CHANNEL_ID}"
    return None


def _build_shop_query(
    db: Session,
    area: Optional[str],
    status: Optional[str],
    q: Optional[str] = None,
    category: Optional[str] = None,
    sort: str = "created_at_desc",
):
    query = db.query(Shop)
    if q:
        query = query.filter(Shop.shop_name.ilike(f"%{q.strip()}%"))
    if area == "__none__":
        query = query.filter(Shop.area.is_(None))
    elif area:
        query = query.filter(Shop.area == area)
    if category:
        query = query.filter(Shop.category == category)
    if status == "unvisited":
        query = query.filter(Shop.is_visited == False)  # noqa: E712
    elif status == "visited":
        query = query.filter(Shop.is_visited == True)  # noqa: E712

    order_map = {
        "name_asc":        Shop.shop_name.asc(),
        "name_desc":       Shop.shop_name.desc(),
        "area_asc":        Shop.area.asc(),
        "rating_desc":     Shop.rating.desc().nullslast(),
        "rating_asc":      Shop.rating.asc().nullslast(),
        "created_at_asc":  Shop.created_at.asc(),
        "created_at_desc": Shop.created_at.desc(),
    }
    return query.order_by(order_map.get(sort, Shop.created_at.desc()))


def _get_categories(db: Session) -> list[str]:
    return sorted(
        c[0]
        for c in db.query(Shop.category).filter(Shop.category.isnot(None)).distinct().all()
        if c[0]
    )


# ---------------------------------------------------------------------------
# Home — list with filters, sort, pagination
# ---------------------------------------------------------------------------

@router.get("/")
def home(
    request: Request,
    area: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    category: Optional[str] = None,
    sort: str = "created_at_desc",
    page: int = 1,
    db: Session = Depends(get_db),
):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)

    page = max(1, page)
    areas = sorted(
        a[0]
        for a in db.query(Shop.area).filter(Shop.area.isnot(None)).distinct().all()
        if a[0]
    )
    categories = _get_categories(db)

    base_q = _build_shop_query(db, area, status, q, category, sort)
    total = base_q.count()
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)
    shops = base_q.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()

    # Pre-build filter query string so templates can construct sort/page links cleanly
    filter_qs = urlencode({
        "q": q or "",
        "area": area or "",
        "status": status or "",
        "category": category or "",
    })

    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "shops": shops,
            "all_areas": areas,
            "all_categories": categories,
            "selected_area": area or "",
            "selected_status": status or "",
            "selected_q": q or "",
            "selected_category": category or "",
            "selected_sort": sort,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "filter_qs": filter_qs,
            "discord_base_url": _discord_base_url(),
        },
    )


# ---------------------------------------------------------------------------
# Shop detail / edit
# ---------------------------------------------------------------------------

@router.get("/shop/{shop_id}")
def shop_detail(
    shop_id: int,
    request: Request,
    saved: bool = False,
    db: Session = Depends(get_db),
):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    discord_url = None
    base = _discord_base_url()
    if base:
        discord_url = f"{base}/{shop.message_id}"

    return templates.TemplateResponse(
        request=request,
        name="shop.html",
        context={
            "shop": shop,
            "all_categories": _get_categories(db),
            "discord_url": discord_url,
            "saved": saved,
        },
    )


@router.post("/shop/{shop_id}/edit")
def shop_edit(
    shop_id: int,
    request: Request,
    shop_name: str = Form(...),
    area: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    url: Optional[str] = Form(None),
    memo: Optional[str] = Form(None),
    rating: Optional[str] = Form(None),
    is_visited: Optional[str] = Form(None),
    visited_at: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    shop.shop_name = shop_name.strip()
    shop.area = area.strip() or None if area else None
    shop.category = category.strip() or None if category else None
    shop.url = url.strip() or None if url else None
    shop.memo = memo.strip() or None if memo else None

    # Parse rating (1-5 or empty → null)
    if rating and rating.isdigit() and 1 <= int(rating) <= 5:
        shop.rating = int(rating)
    else:
        shop.rating = None

    # Parse is_visited checkbox (present = True, absent = False)
    shop.is_visited = is_visited is not None

    # Parse visited_at date field
    if visited_at:
        try:
            d = date.fromisoformat(visited_at)
            shop.visited_at = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            pass
    elif not shop.is_visited:
        shop.visited_at = None

    db.commit()
    return RedirectResponse(f"/shop/{shop_id}?saved=true", status_code=302)


@router.post("/shop/{shop_id}/delete")
def shop_delete(
    shop_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
    db.delete(shop)
    db.commit()
    return RedirectResponse("/", status_code=302)


# ---------------------------------------------------------------------------
# Inline update endpoints — called via fetch() from the list view
# ---------------------------------------------------------------------------

@router.post("/shop/{shop_id}/visited")
def toggle_visited(
    shop_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Toggle is_visited. Sets visited_at to now on first visit; clears on unvisit."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        return JSONResponse({"error": "not found"}, status_code=404)

    shop.is_visited = not shop.is_visited
    if shop.is_visited:
        if not shop.visited_at:
            shop.visited_at = datetime.now(timezone.utc)
    else:
        shop.visited_at = None
    db.commit()

    return JSONResponse({
        "is_visited": shop.is_visited,
        "visited_at": shop.visited_at.strftime("%Y-%m-%d") if shop.visited_at else None,
    })


@router.post("/shop/{shop_id}/rating")
async def set_rating(
    shop_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Set rating (1-5) or clear it (0). Called via fetch with JSON body."""
    if not _is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    try:
        rating = int(body.get("rating", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid rating"}, status_code=400)
    if not (0 <= rating <= 5):
        return JSONResponse({"error": "rating must be 0-5"}, status_code=400)

    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        return JSONResponse({"error": "not found"}, status_code=404)

    shop.rating = rating if rating > 0 else None
    db.commit()
    return JSONResponse({"rating": shop.rating})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.get("/login")
def login_page(request: Request):
    if not WEB_PASSWORD:
        return RedirectResponse("/", status_code=302)
    if _is_authenticated(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    if WEB_PASSWORD and hmac.compare_digest(password, WEB_PASSWORD):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "パスワードが正しくありません。"},
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
    category: Optional[str] = None,
    sort: str = "created_at_desc",
    db: Session = Depends(get_db),
):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)

    shops = _build_shop_query(db, area, status, q, category, sort).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "_id", "@timestamp", "message_id",
        "shop.name", "shop.area", "shop.category",
        "status.is_visited", "visited_at", "rating", "memo", "url",
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
            s.visited_at.strftime("%Y-%m-%d") if s.visited_at else "",
            s.rating or "",
            _safe(s.memo),
            _safe(s.url),
        ])

    # UTF-8 BOM so Excel opens it correctly
    content = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename=meshi_archive.csv"},
    )
