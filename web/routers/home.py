import csv
import hashlib
import ipaddress
import io
import logging
import os
from collections.abc import Generator, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from typing import Annotated, Optional, TypedDict
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, func, literal, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query as SqlAlchemyQuery
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.elements import ColumnElement
from starlette.responses import Response

from db.database import SessionLocal
from services.admin_sessions import revoke_admin_session
from db.models import (
    AssetKind,
    FetchStatus,
    Message,
    MetadataReviewStatus,
    ReviewStatus,
    Shop,
    ShopMention,
    ShopRedirect,
    prefer_reparsed_duplicate,
)
from web.auth import is_admin
from web.area_groups import (
    area_display_label,
    area_filter_label,
    area_filter_matches,
    area_list_sort_key,
    area_municipality,
    area_ward,
    build_area_filter_options,
    canonicalize_area,
    editable_area_groups,
    group_areas,
)
from web.csrf import get_csrf_token, verify_csrf_token
from web.read_only import require_writable
from web.region_master import Municipality
from services.resolution import normalize_phone
from services.shop_image_cache import processed_image_public_url, validate_image_source_url
from services.shop_image_upload import (
    MAX_UPLOAD_BYTES,
    UploadValidationError,
    save_uploaded_image,
    uploaded_image_public_url,
)
from web.routers.auth_discord import is_configured as discord_login_enabled

_CSV_INJECT_CHARS = frozenset("=+-@\t\r")

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))
templates.env.globals["area_ward"] = area_ward
templates.env.globals["area_display_label"] = area_display_label
templates.env.globals["area_filter_label"] = area_filter_label
logger = logging.getLogger(__name__)

DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

PER_PAGE: int = 24
FIRST_INCREMENTAL_PAGE: int = 2
DEFAULT_SORT: str = "area_order"
VALID_SORTS: frozenset[str] = frozenset({
    "area_order",
    "name_asc",
    "name_desc",
    "area_asc",
    "area_desc",
    "rating_asc",
    "rating_desc",
    "created_at_desc",
})


class ActiveFilter(TypedDict):
    label: str
    remove_url: str


class ShopLinks(TypedDict):
    maps_url: str
    source_url: Optional[str]
    source_is_map: bool
    source_label: Optional[str]
    canonical_url: Optional[str]
    discord_url: Optional[str]
    image_url: Optional[str]


class StatusFacet(TypedDict):
    key: str
    label: str
    count: int
    url: str
    selected: bool


def _normalize_sort(sort: str) -> str:
    return sort if sort in VALID_SORTS else DEFAULT_SORT


def _list_url(
    *,
    q: Optional[str],
    area: Optional[str],
    status: Optional[str],
    category: Optional[str],
    sort: str,
    page: int = 1,
) -> str:
    params: dict[str, str] = {}
    if q:
        params["q"] = q
    if area:
        params["area"] = area
    if status:
        params["status"] = status
    if category:
        params["category"] = category
    params["sort"] = _normalize_sort(sort)
    if page > 1:
        params["page"] = str(page)
    return f"/?{urlencode(params)}"


def _active_filters(
    *,
    q: Optional[str],
    area: Optional[str],
    status: Optional[str],
    category: Optional[str],
    sort: str,
) -> list[ActiveFilter]:
    filters: list[ActiveFilter] = []
    values = {
        "q": q,
        "area": area,
        "status": status,
        "category": category,
    }
    labels = {
        "q": f"キーワード: {q}" if q else "",
        "area": "エリア: 未設定" if area == "__none__" else f"エリア: {area_filter_label(area or '')}",
        "category": f"カテゴリ: {category}" if category else "",
        "status": "訪問済み" if status == "visited" else "未訪問",
    }
    for key in ("q", "area", "category", "status"):
        if not values[key]:
            continue
        remaining = values.copy()
        remaining[key] = None
        filters.append({
            "label": labels[key],
            "remove_url": _list_url(
                q=remaining["q"],
                area=remaining["area"],
                status=remaining["status"],
                category=remaining["category"],
                sort=sort,
            ),
        })
    sort_labels: dict[str, str] = {
        "name_asc": "店名順",
        "name_desc": "店名順（逆順）",
        "area_asc": "エリア名順",
        "area_desc": "エリア名順（逆順）",
        "rating_desc": "評価が高い順",
        "rating_asc": "評価が低い順",
        "created_at_desc": "追加が新しい順",
    }
    if sort in sort_labels:
        filters.append({
            "label": sort_labels[sort],
            "remove_url": _list_url(
                q=q, area=area, status=status, category=category, sort=DEFAULT_SORT,
            ),
        })
    return filters


def _validate_return_to(value: Optional[str]) -> str:
    if not value:
        return "/"
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="return_to must be a local list URL",
        ) from exc
    has_control_character = any(ord(character) < 32 or ord(character) == 127 for character in value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.path != "/"
        or parsed.fragment
        or has_control_character
    ):
        raise HTTPException(
            status_code=400,
            detail="return_to must be a local list URL",
        )
    return value


def _has_public_mention() -> ColumnElement[bool]:
    return Shop.mentions.any(
        and_(
            ShopMention.review_status == ReviewStatus.APPROVED.value,
            ShopMention.metadata_review_status == MetadataReviewStatus.APPROVED.value,
        )
    )


def _public_mentions(shop: Shop) -> list[ShopMention]:
    mentions = sorted(
        (
            mention
            for mention in shop.mentions
            if mention.review_status == ReviewStatus.APPROVED.value
            and mention.metadata_review_status == MetadataReviewStatus.APPROVED.value
        ),
        key=lambda mention: (mention.id is None, mention.id or 0),
    )
    return prefer_reparsed_duplicate(mentions)


def _public_mention(shop: Shop) -> ShopMention | None:
    mentions = _public_mentions(shop)
    return mentions[0] if mentions else None


def _safe_http_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = value.strip()
    if (
        not cleaned
        or any(
            ord(character) < 32
            or ord(character) == 127
            or character.isspace()
            or character == "\\"
            for character in cleaned
        )
    ):
        return None
    try:
        parsed = urlparse(cleaned)
        parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if not _is_valid_http_hostname(parsed.hostname):
        return None
    return cleaned


def _is_valid_http_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip(".")
    if not normalized:
        return False
    try:
        ipaddress.ip_address(normalized)
        return True
    except ValueError:
        pass
    try:
        ascii_hostname = normalized.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_hostname) > 253:
        return False
    for label in ascii_hostname.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(not (character.isalnum() or character == "-") for character in label)
        ):
            return False
    return True


def _http_url_identity(value: str) -> str:
    parsed = urlparse(value)
    hostname = parsed.hostname or ""
    try:
        ipaddress.ip_address(hostname)
        normalized_hostname = hostname.lower()
        if ":" in normalized_hostname:
            normalized_hostname = f"[{normalized_hostname}]"
    except ValueError:
        normalized_hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    port = parsed.port
    include_port = port is not None and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    )
    netloc = normalized_hostname + (f":{port}" if include_port else "")
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    tracking_names = {"fbclid", "gclid", "mc_cid", "mc_eid"}
    query_items = sorted(
        (name, item_value)
        for name, item_value in parse_qsl(parsed.query, keep_blank_values=True)
        if not name.lower().startswith("utm_") and name.lower() not in tracking_names
    )
    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=netloc,
        path=path,
        query=urlencode(query_items),
        fragment="",
    ).geturl()


def _is_google_maps_url(value: Optional[str]) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if hostname == "maps.app.goo.gl":
        return True
    if hostname == "goo.gl" and parsed.path.startswith("/maps"):
        return True
    return (
        hostname in {
            "google.com",
            "www.google.com",
            "maps.google.com",
            "google.co.jp",
            "www.google.co.jp",
            "maps.google.co.jp",
        }
        and parsed.path.startswith("/maps")
    )


def _google_maps_url(shop: Shop) -> str:
    query_parts = [shop.shop_name]
    if shop.branch_name:
        query_parts.append(shop.branch_name)
    if shop.address:
        query_parts.append(shop.address)
    elif shop.area:
        query_parts.append(shop.area)
    query = " ".join(part.strip() for part in query_parts if part.strip())
    return f"https://www.google.com/maps/search/?{urlencode({'api': '1', 'query': query})}"


def _source_link_label(source_url: str, source_is_map: bool) -> str:
    if source_is_map:
        return "登録時の地図"
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").lower()
    if unquote(parsed.path).lower().endswith(".pdf"):
        return "PDF"
    if hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return "X"
    if hostname in {"mobile.x.com", "mobile.twitter.com"}:
        parts: list[str] = parsed.path.strip("/").split("/")
        is_status_post = len(parts) >= 3 and parts[1] == "status" and parts[2].isdigit()
        is_web_post = (
            len(parts) >= 4
            and parts[:3] == ["i", "web", "status"]
            and parts[3].isdigit()
        )
        if is_status_post or is_web_post:
            return "X"
        return hostname
    if hostname == "tabelog.com" or hostname.endswith(".tabelog.com"):
        return "食べログ"
    if hostname == "dancyu.jp" or hostname.endswith(".dancyu.jp"):
        return "dancyu"
    if hostname in {"youtube.com", "youtu.be"} or hostname.endswith(".youtube.com"):
        return "YouTube"
    if hostname == "discord.com" or hostname.endswith(".discord.com"):
        return "Discord"
    return hostname


def _shop_image_url(public_mentions: Sequence[ShopMention]) -> Optional[str]:
    for mention in public_mentions:
        message = mention.message
        if message is None:
            continue
        assets = sorted(
            message.assets,
            key=lambda asset: (asset.id is None, asset.id or 0),
        )
        for asset in assets:
            if (
                asset.kind != AssetKind.IMAGE.value
                or asset.fetch_status == FetchStatus.UNAVAILABLE.value
            ):
                continue
            image_url = _safe_http_url(asset.url)
            if image_url is None or urlparse(image_url).scheme != "https":
                logger.error(
                    "Unsafe public image URL omitted: mention_id=%s asset_id=%s",
                    mention.id,
                    asset.id,
                )
                continue
            try:
                validate_image_source_url(image_url)
            except ValueError:
                logger.error(
                    "Unsupported public image source omitted: mention_id=%s asset_id=%s",
                    mention.id,
                    asset.id,
                )
                continue
            return processed_image_public_url(image_url)
    return None


def _shop_links(shop: Shop, discord_base_url: Optional[str]) -> ShopLinks:
    public_mentions = _public_mentions(shop)
    public_mention = public_mentions[0] if public_mentions else None
    source_url: Optional[str] = None
    for mention in public_mentions:
        if not mention.source_url:
            continue
        source_url = _safe_http_url(mention.source_url)
        if source_url is not None:
            break
        logger.error(
            "Unsafe public source URL omitted: shop_id=%s mention_id=%s",
            shop.id,
            mention.id,
        )
    canonical_url = _safe_http_url(shop.canonical_url)
    if shop.canonical_url and canonical_url is None:
        logger.error("Unsafe canonical URL omitted: shop_id=%s", shop.id)
    if (
        source_url is not None
        and canonical_url is not None
        and _http_url_identity(source_url) == _http_url_identity(canonical_url)
    ):
        canonical_url = None
    discord_url = None
    if discord_base_url and public_mention:
        discord_url = f"{discord_base_url}/{public_mention.message_id}"
    source_is_map = _is_google_maps_url(source_url)
    return {
        "maps_url": _google_maps_url(shop),
        "source_url": source_url,
        "source_is_map": source_is_map,
        "source_label": (
            _source_link_label(source_url, source_is_map)
            if source_url is not None
            else None
        ),
        "canonical_url": canonical_url,
        "discord_url": discord_url,
        "image_url": uploaded_image_public_url(shop.image_key) if shop.image_key else None,
    }


def _shop_links_by_id(
    shops: Sequence[Shop],
    discord_base_url: Optional[str],
) -> dict[int, ShopLinks]:
    result: dict[int, ShopLinks] = {}
    for shop in shops:
        if shop.id is None:
            raise ValueError(f"Cannot build shop links without shop_id: shop_name={shop.shop_name!r}")
        result[shop.id] = _shop_links(shop, discord_base_url)
    return result


def _validate_optional_http_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character.isspace()
        or character == "\\"
        for character in cleaned
    ):
        raise HTTPException(status_code=400, detail="URL contains invalid characters")
    try:
        parsed = urlparse(cleaned)
        parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid URL port")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="URL userinfo is not allowed")
    if not _is_valid_http_hostname(parsed.hostname):
        raise HTTPException(status_code=400, detail="URL host is invalid")
    return cleaned


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated")) or is_admin(request)


def _discord_base_url() -> Optional[str]:
    if DISCORD_GUILD_ID and DISCORD_CHANNEL_ID:
        return f"https://discord.com/channels/{DISCORD_GUILD_ID}/{DISCORD_CHANNEL_ID}"
    return None


def _area_list_order(db: Session) -> ColumnElement[int]:
    area_keys: dict[str, tuple[int, int, str]] = {
        area: area_list_sort_key(area)
        for (area,) in db.query(Shop.area).distinct().all()
        if area is not None
    }
    if not area_keys:
        return literal(0)
    unset_key = area_list_sort_key(None)
    key_ranks: dict[tuple[int, int, str], int] = {
        key: index
        for index, key in enumerate(sorted(set(area_keys.values()) | {unset_key}))
    }
    area_ranks: dict[str, int] = {
        area: key_ranks[key] for area, key in area_keys.items()
    }
    return case(area_ranks, value=Shop.area, else_=key_ranks[unset_key])


def _area_search_labels(area: str) -> tuple[str, ...]:
    municipality: Municipality | None = area_municipality(area)
    labels: list[str] = [area_display_label(area), area_ward(area) or ""]
    if municipality is not None:
        labels.extend((municipality.prefecture, municipality.region, municipality.name, municipality.area))
    return tuple(label for label in labels if label)


def _build_shop_query(
    db: Session,
    area: Optional[str],
    status: Optional[str],
    q: Optional[str] = None,
    category: Optional[str] = None,
    sort: str = DEFAULT_SORT,
    reviewed_only: bool = True,
) -> SqlAlchemyQuery[Shop]:
    query = db.query(Shop)
    if reviewed_only:
        query = query.filter(_has_public_mention())
    if q:
        area_labels: dict[str, tuple[str, ...]] = {
            stored_area: _area_search_labels(stored_area)
            for (stored_area,) in db.query(Shop.area).filter(Shop.area.isnot(None)).distinct().all()
        }
        search_fields = (
            Shop.shop_name,
            Shop.branch_name,
            Shop.area,
            Shop.category,
            Shop.address,
            Shop.memo,
        )
        for term in q.split():
            escaped_term = (
                term.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped_term}%"
            matching_areas: list[str] = [
                stored_area for stored_area, labels in area_labels.items()
                if any(term.casefold() in label.casefold() for label in labels)
            ]
            query = query.filter(
                or_(
                    *(field.ilike(pattern, escape="\\") for field in search_fields),
                    Shop.area.in_(matching_areas),
                )
            )
    if area == "__none__":
        query = query.filter(Shop.area.is_(None))
    elif area:
        selected_areas: list[str] = [
            stored_area
            for (stored_area,) in db.query(Shop.area).filter(Shop.area.isnot(None)).distinct().all()
            if area_filter_matches(stored_area, area)
        ]
        query = query.filter(Shop.area.in_(selected_areas))
    if category:
        query = query.filter(Shop.category == category)
    if status == "unvisited":
        query = query.filter(Shop.is_visited.is_(False))
    elif status == "visited":
        query = query.filter(Shop.is_visited.is_(True))

    if _normalize_sort(sort) == DEFAULT_SORT:
        return query.order_by(_area_list_order(db), Shop.shop_name.asc(), Shop.id.asc())

    order_map = {
        "name_asc":        (Shop.shop_name.asc(), Shop.id.asc()),
        "name_desc":       (Shop.shop_name.desc(), Shop.id.desc()),
        "area_asc":        (Shop.area.asc(), Shop.id.asc()),
        "area_desc":       (Shop.area.desc(), Shop.id.desc()),
        "rating_desc": (Shop.rating.desc().nullslast(), Shop.id.desc()),
        "rating_asc": (Shop.rating.asc().nullslast(), Shop.id.asc()),
        "created_at_desc": (Shop.created_at.desc(), Shop.id.desc()),
    }
    order = order_map.get(
        sort,
        (Shop.shop_name.asc(), Shop.id.asc()),
    )
    return query.order_by(*order)


def _get_categories(db: Session) -> list[str]:
    return sorted(
        c[0]
        for c in (
            db.query(Shop.category)
            .filter(
                Shop.category.isnot(None),
                _has_public_mention(),
            )
            .distinct()
            .all()
        )
        if c[0]
    )


@router.get("/")
def home(
    request: Request,
    area: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    category: Optional[str] = None,
    sort: str = DEFAULT_SORT,
    page: Annotated[int, Query(ge=1)] = 1,
    db: Session = Depends(get_db),
) -> Response:
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)

    normalized_q = q.strip() if q else None
    q = normalized_q or None
    normalized_category = category.strip() if category else None
    category = normalized_category or None
    status = status if status in {"visited", "unvisited"} else None
    requested_sort = sort
    sort = _normalize_sort(sort)
    if requested_sort != sort:
        return RedirectResponse(
            _list_url(
                q=q,
                area=area,
                status=status,
                category=category,
                sort=sort,
                page=page,
            ),
            status_code=302,
        )

    area_counts = (
        _build_shop_query(db, None, status, q, category, "name_asc")
        .order_by(None)
        .with_entities(Shop.area, func.count(Shop.id))
        .group_by(Shop.area)
        .all()
    )
    grouped_areas = group_areas([(a, n) for a, n in area_counts if a])
    area_filter_options = build_area_filter_options([(a, n) for a, n in area_counts if a])
    all_areas = [option.value for option in area_filter_options]
    none_count: int = sum(count for stored_area, count in area_counts if stored_area is None)
    categories = _get_categories(db)

    status_counts: dict[str, int] = {
        "all": _build_shop_query(
            db, area, None, q, category, sort
        ).order_by(None).count(),
        "unvisited": _build_shop_query(
            db, area, "unvisited", q, category, sort
        ).order_by(None).count(),
        "visited": _build_shop_query(
            db, area, "visited", q, category, sort
        ).order_by(None).count(),
    }
    status_facets: list[StatusFacet] = [
        {
            "key": "all",
            "label": "すべて",
            "count": status_counts["all"],
            "url": _list_url(
                q=q,
                area=area,
                status=None,
                category=category,
                sort=sort,
            ),
            "selected": status is None,
        },
        {
            "key": "unvisited",
            "label": "未訪問",
            "count": status_counts["unvisited"],
            "url": _list_url(
                q=q,
                area=area,
                status="unvisited",
                category=category,
                sort=sort,
            ),
            "selected": status == "unvisited",
        },
        {
            "key": "visited",
            "label": "訪問済み",
            "count": status_counts["visited"],
            "url": _list_url(
                q=q,
                area=area,
                status="visited",
                category=category,
                sort=sort,
            ),
            "selected": status == "visited",
        },
    ]

    base_q = _build_shop_query(db, area, status, q, category, sort)
    total = base_q.count()
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        return RedirectResponse(
            _list_url(
                q=q,
                area=area,
                status=status,
                category=category,
                sort=sort,
                page=total_pages,
            ),
            status_code=302,
        )
    offset = (page - 1) * PER_PAGE
    shops = (
        base_q.options(
            selectinload(Shop.mentions)
            .selectinload(ShopMention.message)
            .selectinload(Message.assets)
        )
        .offset(offset)
        .limit(PER_PAGE)
        .all()
    )
    discord_base_url = _discord_base_url()
    shop_links = _shop_links_by_id(shops, discord_base_url)
    page_start = offset + 1 if total else 0
    page_end = offset + len(shops)
    has_previous = page > 1
    has_next = page < total_pages

    filter_params = {
        key: value
        for key, value in {
            "q": q,
            "area": area,
            "status": status,
            "category": category,
        }.items()
        if value
    }
    filter_qs = urlencode(filter_params)
    current_list_url = _list_url(
        q=q,
        area=area,
        status=status,
        category=category,
        sort=sort,
        page=page,
    )

    return templates.TemplateResponse(
        request=request,
        name="explore.html",
        context={
            "shops": shops,
            "shop_links": shop_links,
            "grouped_areas": grouped_areas,
            "area_filter_options": area_filter_options,
            "all_areas": all_areas,
            "none_count": none_count,
            "all_categories": categories,
            "selected_area": area or "",
            "selected_status": status or "",
            "selected_q": q or "",
            "selected_category": category or "",
            "selected_sort": sort,
            "default_sort": DEFAULT_SORT,
            "status_facets": status_facets,
            "total": total,
            "total_pages": total_pages,
            "current_page": page,
            "page_start": page_start,
            "page_end": page_end,
            "per_page": PER_PAGE,
            "has_previous": has_previous,
            "has_next": has_next,
            "previous_page": page - 1 if has_previous else None,
            "next_page": page + 1 if has_next else None,
            "has_more": has_next,
            "filter_qs": filter_qs,
            "current_list_url": current_list_url,
            "active_filters": _active_filters(
                q=q,
                area=area,
                status=status,
                category=category,
                sort=sort,
            ),
            "discord_base_url": discord_base_url,
            "is_admin": is_admin(request),
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/api/shops")
def api_shops(
    request: Request,
    area: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    category: Optional[str] = None,
    sort: str = DEFAULT_SORT,
    page: int = 2,
    db: Session = Depends(get_db),
) -> JSONResponse:
    if not _is_authenticated(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    page = max(FIRST_INCREMENTAL_PAGE, page)
    normalized_q = q.strip() if q else None
    q = normalized_q or None
    normalized_category = category.strip() if category else None
    category = normalized_category or None
    status = status if status in {"visited", "unvisited"} else None
    sort = _normalize_sort(sort)
    base_q = _build_shop_query(db, area, status, q, category, sort)
    total = base_q.count()
    shops = (
        base_q.options(
            selectinload(Shop.mentions)
            .selectinload(ShopMention.message)
            .selectinload(Message.assets)
        )
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
        .all()
    )
    has_more = page * PER_PAGE < total
    discord_base_url = _discord_base_url()
    shop_links = _shop_links_by_id(shops, discord_base_url)

    html = templates.env.get_template("_shop_cards.html").render(
        shops=shops,
        shop_links=shop_links,
        is_admin=is_admin(request),
        discord_base_url=discord_base_url,
        current_list_url=_list_url(
            q=q,
            area=area,
            status=status,
            category=category,
            sort=sort,
            page=page,
        ),
    )
    return JSONResponse({"html": html, "has_more": has_more, "next_page": page + 1})


@dataclass(frozen=True)
class ShopEditValues:
    shop_name: str
    area: str
    category: str
    url: str
    address: str
    phone: str
    memo: str
    rating: str
    is_visited: bool
    visited_at: str
    expected_version: str = ""


_SHOP_EDIT_FIELDS: dict[str, str] = {
    "shop_name": "店名", "area": "エリア", "category": "カテゴリ", "url": "店舗URL",
    "address": "住所", "phone": "電話番号", "memo": "メモ", "rating": "評価",
    "is_visited": "訪問済み", "visited_at": "訪問日",
}


def _shop_edit_values(shop: Shop) -> ShopEditValues:
    return ShopEditValues(
        shop_name=shop.shop_name, area=shop.area or "", category=shop.category or "",
        url=shop.canonical_url or "", address=shop.address or "", phone=shop.phone or "",
        memo=shop.memo or "", rating=str(shop.rating) if shop.rating is not None else "",
        is_visited=shop.is_visited,
        visited_at=shop.visited_at.strftime("%Y-%m-%d") if shop.visited_at else "",
        expected_version=str(shop.version),
    )


def _parse_shop_version(value: str | None) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal() or len(value) > 19:
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


class ShopEditErrors(TypedDict, total=False):
    shop_name: str
    area: str
    url: str
    rating: str
    visited_at: str
    photo: str


def _render_shop_page(
    request: Request,
    shop: Shop,
    db: Session,
    return_to: str,
    *,
    saved: bool = False,
    edit_mode: bool = False,
    edit_values: ShopEditValues | None = None,
    edit_errors: ShopEditErrors | None = None,
    edit_failure: str | None = None,
    status_code: int = 200,
) -> Response:
    public_mention = _public_mention(shop)
    values = edit_values or _shop_edit_values(shop)
    discord_url = None
    base = _discord_base_url()
    primary_mention = public_mention or shop.primary_mention()
    if base and primary_mention:
        discord_url = f"{base}/{primary_mention.message_id}"
    shop_links = _shop_links_by_id([shop], base)
    shop_links[shop.id]["discord_url"] = discord_url

    grouped_areas = editable_area_groups() if is_admin(request) else []
    editable_areas = {
        area for _group_label, areas in grouped_areas for area, _count in areas
    }

    response = templates.TemplateResponse(
        request=request,
        name="shop.html",
        context={
            "shop": shop,
            "shop_links": shop_links,
            "all_categories": _get_categories(db),
            "grouped_areas": grouped_areas,
            "current_area_outside_master": bool(values.area and values.area not in editable_areas),
            "edit_values": values,
            "edit_errors": edit_errors or {},
            "edit_failure": edit_failure,
            "conflict_values": _shop_edit_values(shop) if status_code == 409 else None,
            "edit_field_labels": _SHOP_EDIT_FIELDS,
            "discord_url": discord_url,
            "saved": saved,
            "return_to": return_to,
            "is_admin": is_admin(request),
            "is_editing": is_admin(request) and (edit_mode or edit_values is not None),
            "csrf_token": get_csrf_token(request),
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("/shop/{shop_id}")
def shop_detail(
    shop_id: int,
    request: Request,
    saved: bool = False,
    return_to: Optional[str] = None,
    db: Session = Depends(get_db),
    edit: bool = False,
) -> Response:
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)
    safe_return_to = _validate_return_to(return_to)
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        redirect = (
            db.query(ShopRedirect)
            .filter(ShopRedirect.source_shop_id == shop_id)
            .first()
        )
        if redirect is not None:
            location = f"/shop/{redirect.target_shop_id}"
            if return_to:
                location = f"{location}?{urlencode({'return_to': safe_return_to})}"
            return RedirectResponse(location, status_code=308)
        raise HTTPException(status_code=404, detail="Shop not found")
    if _public_mention(shop) is None and not is_admin(request):
        raise HTTPException(status_code=404, detail="Shop not found")
    return _render_shop_page(request, shop, db, safe_return_to, saved=saved, edit_mode=edit)


@router.get("/shop/{shop_id}/edit-snapshot")
def shop_edit_snapshot(shop_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Administrator authentication required")
    shop = db.query(Shop).filter(Shop.id == shop_id).populate_existing().first()
    if shop is None:
        raise HTTPException(status_code=404, detail="Shop not found")
    values = asdict(_shop_edit_values(shop))
    values.pop("expected_version")
    return JSONResponse(
        {"version": shop.version, "values": values, "image_url": _shop_links(shop, None)["image_url"]},
        headers={"Cache-Control": "private, no-store"},
    )


_SHOP_WRITE_CONFLICT = "店舗情報が更新されています。入力内容を保持したまま、別の画面で最新の内容を確認してください。"


def _reserve_shop_version(db: Session, shop: Shop) -> bool:
    expected_version = shop.version
    updated = (
        db.query(Shop)
        .filter(Shop.id == shop.id, Shop.version == expected_version)
        .update({Shop.version: Shop.version + 1}, synchronize_session=False)
    )
    if updated != 1:
        logger.info(
            "Shop write conflict: shop_id=%s expected_version=%s", shop.id, expected_version,
        )
        return False
    set_committed_value(shop, "version", expected_version + 1)
    return True


@router.post("/shop/{shop_id}/edit")
def shop_edit(
    shop_id: int,
    request: Request,
    shop_name: str = Form(""),
    area: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    url: Optional[str] = Form(None),
    address: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    memo: Optional[str] = Form(None),
    rating: Optional[str] = Form(None),
    is_visited: Optional[str] = Form(None),
    visited_at: Optional[str] = Form(None),
    csrf_token: str = Form(...),
    return_to: Annotated[str, Form()] = "/",
    db: Session = Depends(get_db),
    photo: Annotated[UploadFile | None, File()] = None,
    expected_version: Annotated[str, Form()] = "",
    confirmed_version: Annotated[str, Form()] = "",
    confirm_conflict: Annotated[str, Form()] = "",
    conflict_choices: Annotated[list[str] | None, Form()] = None,
) -> Response:
    if not is_admin(request):
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JSONResponse(
                {"detail": "管理者ログインが必要です。別のタブでログインしてから、もう一度保存してください。"},
                status_code=401, headers={
                    "Cache-Control": "private, no-store", "X-CSRF-Token": get_csrf_token(request),
                },
            )
        return RedirectResponse("/admin/login", status_code=302)
    require_writable()
    try:
        verify_csrf_token(request, csrf_token)
    except HTTPException as exc:
        if exc.status_code != 403 or request.headers.get("x-requested-with") != "XMLHttpRequest":
            raise
        logger.info("Shop edit session validation failed: shop_id=%s status=403", shop_id)
        return JSONResponse(
            {"detail": "画面の認証情報が更新されました。もう一度保存してください。"},
            status_code=403, headers={
                "Cache-Control": "private, no-store", "X-CSRF-Token": get_csrf_token(request),
            },
        )
    safe_return_to = _validate_return_to(return_to)
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        redirect = (
            db.query(ShopRedirect)
            .filter(ShopRedirect.source_shop_id == shop_id)
            .first()
        )
        if redirect is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Shop was merged: canonical_shop_id={redirect.target_shop_id}",
            )
        raise HTTPException(status_code=404, detail="Shop not found")

    values = ShopEditValues(
        shop_name=shop_name,
        area=area or "",
        category=category or "",
        url=url or "",
        address=address or "",
        phone=phone or "",
        memo=memo or "",
        rating=rating or "",
        is_visited=is_visited is not None,
        visited_at=visited_at or "",
        expected_version=expected_version,
    )
    current_values = _shop_edit_values(shop)
    if (
        _parse_shop_version(expected_version) != shop.version
        and confirm_conflict == "on"
        and _parse_shop_version(confirmed_version) == shop.version
    ):
        differences = {
            field for field in _SHOP_EDIT_FIELDS
            if getattr(values, field) != getattr(current_values, field)
        }
        choices = dict(choice.split(":", 1) for choice in (conflict_choices or []) if ":" in choice)
        if all(choices.get(field) in {"input", "latest"} for field in differences):
            values = replace(values, **{
                field: getattr(current_values, field)
                for field in differences if choices[field] == "latest"
            }, expected_version=confirmed_version)
    if _parse_shop_version(values.expected_version) != shop.version:
        return _shop_edit_error(
            request, shop, db, safe_return_to, values, {},
            detail=_SHOP_WRITE_CONFLICT, status_code=409,
        )
    shop_name, area, category, url = values.shop_name, values.area, values.category, values.url
    address, phone, memo = values.address, values.phone, values.memo
    rating, visited_at = values.rating, values.visited_at
    errors: ShopEditErrors = {}
    cleaned_name = shop_name.strip()
    if not cleaned_name:
        errors["shop_name"] = "店名を入力してください。"
    cleaned_area = shop.area if area == shop.area else canonicalize_area(area)
    if area and area.strip() and cleaned_area is None:
        errors["area"] = "候補にある市区町村・エリアを選んでください。"
    cleaned_url: str | None = None
    try:
        cleaned_url = _validate_optional_http_url(url)
    except HTTPException as exc:
        if exc.status_code != 400:
            raise
        errors["url"] = "http:// または https:// で始まる有効なURLを入力してください。"
    cleaned_rating: int | None = None
    if rating and rating.strip():
        if rating not in {"1", "2", "3", "4", "5"}:
            errors["rating"] = "評価は1〜5から選んでください。"
        else:
            cleaned_rating = int(rating)
    cleaned_visited_at: datetime | None = None
    if values.is_visited and visited_at:
        try:
            visit_date = date.fromisoformat(visited_at)
            cleaned_visited_at = datetime(
                visit_date.year, visit_date.month, visit_date.day, tzinfo=timezone.utc
            )
        except ValueError:
            errors["visited_at"] = "訪問日は実在する日付をYYYY-MM-DD形式で入力してください。"
    if errors:
        logger.info("Shop edit validation failed: shop_id=%s fields=%s", shop_id, list(errors))
        return _shop_edit_error(
            request, shop, db, safe_return_to, values, errors,
            detail="入力内容を確認してください。変更内容はまだ保存されていません。", status_code=400,
        )
    new_image_key: str | None = None
    if photo is not None and photo.filename:
        try:
            source_bytes = photo.file.read(MAX_UPLOAD_BYTES + 1)
            new_image_key = save_uploaded_image(source_bytes).image_key
        except UploadValidationError as exc:
            logger.info("Shop photo validation failed: shop_id=%s filename=%r error=%s", shop_id, photo.filename, exc)
            return _shop_edit_error(
                request, shop, db, safe_return_to, values, {"photo": str(exc)},
                detail="写真を確認してください。変更内容はまだ保存されていません。", status_code=400,
            )
        except OSError:
            logger.exception("Shop photo storage failed: shop_id=%s filename=%r", shop_id, photo.filename)
            return _shop_edit_error(
                request, shop, db, safe_return_to, values, {},
                detail="写真を保存できませんでした。時間をおいて、もう一度保存してください。", status_code=503,
            )
        finally:
            photo.file.close()
    try:
        if not _reserve_shop_version(db, shop):
            db.rollback()
            return _shop_edit_error(
                request, shop, db, safe_return_to, values, {},
                detail=_SHOP_WRITE_CONFLICT, status_code=409,
            )
        shop.shop_name = cleaned_name
        shop.area = cleaned_area
        if cleaned_area is None:
            for mention in shop.mentions:
                if (
                    mention.metadata_review_status != MetadataReviewStatus.PENDING.value
                    or mention.metadata_difference_type != "missing_area"
                    or mention.metadata_reviewed_at is not None
                ):
                    mention.metadata_review_status = MetadataReviewStatus.PENDING.value
                    mention.metadata_difference_type = "missing_area"
                    mention.metadata_reviewed_at = None
                    mention.version += 1
        shop.category = category.strip() or None if category else None
        shop.canonical_url = cleaned_url
        shop.address = address.strip() or None if address else None
        shop.phone = normalize_phone(phone)
        shop.memo = memo.strip() or None if memo else None
        if new_image_key is not None:
            shop.image_key = new_image_key
        shop.rating = cleaned_rating
        shop.is_visited = values.is_visited
        shop.visited_at = cleaned_visited_at
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Shop edit storage failed: shop_id=%s image_key=%s", shop_id, new_image_key)
        return _shop_edit_error(
            request, shop, db, safe_return_to, values, {},
            detail="変更内容を保存できませんでした。時間をおいて、もう一度保存してください。", status_code=503,
        )
    detail_query = urlencode({"saved": "true", "return_to": safe_return_to})
    redirect_url = f"/shop/{shop_id}?{detail_query}"
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse({"redirect_url": redirect_url}, headers={"Cache-Control": "private, no-store"})
    return RedirectResponse(redirect_url, status_code=302)


def _shop_edit_error(
    request: Request,
    shop: Shop,
    db: Session,
    return_to: str,
    values: ShopEditValues,
    errors: ShopEditErrors,
    *,
    detail: str,
    status_code: int,
) -> Response:
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(
            {"detail": detail, "errors": errors}, status_code=status_code,
            headers={"Cache-Control": "private, no-store"},
        )
    return _render_shop_page(
        request, shop, db, return_to,
        edit_values=values, edit_errors=errors,
        edit_failure=detail + (" 選択した写真は保存されていません。最新値を確認した後に写真を再選択してください。" if status_code == 409 else ""),
        status_code=status_code,
    )


@router.post("/shop/{shop_id}/delete")
def shop_delete(
    shop_id: int,
    request: Request,
    csrf_token: str = Form(...),
    return_to: Annotated[str, Form()] = "/",
    db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    require_writable()
    verify_csrf_token(request, csrf_token)
    safe_return_to = _validate_return_to(return_to)
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
    if db.query(ShopRedirect).filter(ShopRedirect.target_shop_id == shop_id).first():
        raise HTTPException(
            status_code=409,
            detail=f"Shop is a merge target and cannot be deleted: shop_id={shop_id}",
        )
    db.delete(shop)
    db.commit()
    return RedirectResponse(safe_return_to, status_code=302)


@router.post("/shop/{shop_id}/visited")
def toggle_visited(
    shop_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    require_writable()
    verify_csrf_token(request)
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        return JSONResponse({"error": "not found"}, status_code=404)

    if _parse_shop_version(request.headers.get("x-shop-version")) != shop.version:
        return JSONResponse(
            {"error": "conflict", "detail": _SHOP_WRITE_CONFLICT}, status_code=409,
            headers={"Cache-Control": "private, no-store"},
        )

    try:
        if not _reserve_shop_version(db, shop):
            db.rollback()
            return JSONResponse(
                {"error": "conflict", "detail": _SHOP_WRITE_CONFLICT}, status_code=409,
                headers={"Cache-Control": "private, no-store"},
            )
        shop.is_visited = not shop.is_visited
        if shop.is_visited:
            if not shop.visited_at:
                shop.visited_at = datetime.now(timezone.utc)
        else:
            shop.visited_at = None
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Shop visit storage failed: shop_id=%s", shop_id)
        return JSONResponse({"error": "storage unavailable"}, status_code=503)

    return JSONResponse({
        "is_visited": shop.is_visited,
        "visited_at": shop.visited_at.strftime("%Y-%m-%d") if shop.visited_at else None,
        "version": shop.version,
    })


@router.post("/shop/{shop_id}/rating")
async def set_rating(
    shop_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    require_writable()
    verify_csrf_token(request)

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

    if _parse_shop_version(request.headers.get("x-shop-version")) != shop.version:
        return JSONResponse(
            {"error": "conflict", "detail": _SHOP_WRITE_CONFLICT}, status_code=409,
            headers={"Cache-Control": "private, no-store"},
        )

    try:
        if not _reserve_shop_version(db, shop):
            db.rollback()
            return JSONResponse(
                {"error": "conflict", "detail": _SHOP_WRITE_CONFLICT}, status_code=409,
                headers={"Cache-Control": "private, no-store"},
            )
        shop.rating = rating if rating > 0 else None
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Shop rating storage failed: shop_id=%s", shop_id)
        return JSONResponse({"error": "storage unavailable"}, status_code=503)
    return JSONResponse({"rating": shop.rating, "version": shop.version})


_DISCORD_ERROR_MESSAGES = {
    "discord_access_unavailable": "利用許可を確認できませんでした。時間をおいて再度お試しください。",
    "discord_invalid_user_id": "DiscordのユーザーIDを確認できませんでした。もう一度ログインしてください。",
    "discord_unauthorized":     "このDiscordアカウントは登録されていません。管理者にお問い合わせください。",
    "discord_state_mismatch":   "セッションの有効期限が切れた可能性があります。もう一度お試しください。",
    "discord_token_failed":     "Discord認証に失敗しました（トークン取得エラー）。",
    "discord_user_failed":      "Discord認証に失敗しました（ユーザー情報取得エラー）。",
    "discord_no_token":         "Discord認証に失敗しました（トークン無し）。",
    "discord_no_user_id":       "Discord認証に失敗しました（ユーザーID無し）。",
    "discord_missing_params":   "Discord認証パラメータが不足しています。",
    "discord_not_configured":   "Discord認証は現在無効です。",
    "discord_network":          "Discord 通信エラー。時間をおいて再度お試しください。",
    "discord_access_denied":    "Discord認証がキャンセルされました。",
}


@router.get("/login")
def login_page(request: Request, error: Optional[str] = None) -> Response:
    if request.session.get("authenticated") or is_admin(request):
        return RedirectResponse("/", status_code=302)
    error_msg = None
    if error:
        error_msg = _DISCORD_ERROR_MESSAGES.get(error) or "ログインエラーが発生しました。"
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": error_msg,
            "discord_login_enabled": discord_login_enabled(),
            "csrf_token": get_csrf_token(request),
        },
    )


@router.get("/logout")
def logout_page(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request, name="logout.html",
        context={"csrf_token": get_csrf_token(request)},
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/logout")
def perform_logout(request: Request, csrf_token: str = Form(...)) -> Response:
    verify_csrf_token(request, csrf_token)
    if request.session.get("admin_session_token"):
        try:
            with SessionLocal() as db:
                revoke_admin_session(db, request.session)
        except SQLAlchemyError as exc:
            logger.error("Admin logout revocation failed: error_type=%s", type(exc).__name__)
            return Response("ログアウトできませんでした。再試行してください。", status_code=503)
    for key in ("authenticated", "admin_authenticated", "discord_user_id", "discord_username", "discord_grant_generation"):
        request.session.pop(key, None)
    request.session.pop("admin_session_token", None)
    return RedirectResponse("/login", status_code=302)


@router.get("/export.csv")
def export_csv(
    request: Request,
    area: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    category: Optional[str] = None,
    sort: str = DEFAULT_SORT,
    db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=302)

    shops = _build_shop_query(
        db,
        area,
        status,
        q,
        category,
        sort,
        reviewed_only=False,
    ).options(selectinload(Shop.mentions)).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "_id", "@timestamp", "message_id",
        "shop.name", "shop.branch_name", "shop.area", "shop.category",
        "status.is_visited", "visited_at", "rating", "memo",
        "source_url", "canonical_url", "shop.address", "shop.phone",
        "shop.external_source", "shop.external_id",
        "review_status", "resolution_status", "metadata_review_status",
        "resolution_method", "difference_type", "metadata_difference_type",
        "reviewed_at", "metadata_reviewed_at",
        "needs_review", "extraction_source", "extraction_error", "confidence_reason",
        "shop.image_key",
        "shop.version", "shop.updated_at", "mention_id", "mention.version",
        "shop.mention_count", "shop.identity_pending_count", "shop.identity_deferred_count",
        "shop.metadata_pending_count", "shop.metadata_deferred_count", "shop.needs_review",
        "shop.mention_versions",
    ])

    def _safe(val: Optional[str]) -> str:
        s = (val or "").strip()
        return f"'{s}" if s and s[0] in _CSV_INJECT_CHARS else s

    def _utc_isoformat(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    row_count = 0
    for s in shops:
        mention = s.primary_mention()
        if mention is None:
            continue
        created_at: datetime = s.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        writer.writerow([
            s.id,
            created_at.astimezone(timezone.utc).isoformat(),
            mention.message_id,
            _safe(s.shop_name),
            _safe(s.branch_name),
            _safe(s.area),
            _safe(s.category),
            s.is_visited,
            s.visited_at.strftime("%Y-%m-%d") if s.visited_at else "",
            s.rating or "",
            _safe(s.memo),
            _safe(mention.source_url),
            _safe(s.canonical_url),
            _safe(s.address),
            _safe(s.phone),
            _safe(s.external_source),
            _safe(s.external_id),
            mention.review_status,
            mention.resolution_status,
            mention.metadata_review_status,
            mention.resolution_method or "",
            _safe(mention.difference_type),
            _safe(mention.metadata_difference_type),
            mention.reviewed_at.isoformat() if mention.reviewed_at else "",
            (
                mention.metadata_reviewed_at.isoformat()
                if mention.metadata_reviewed_at
                else ""
            ),
            (
                mention.review_status
                in {ReviewStatus.PENDING.value, ReviewStatus.DEFERRED.value}
                or mention.metadata_review_status
                in {
                    MetadataReviewStatus.PENDING.value,
                    MetadataReviewStatus.DEFERRED.value,
                }
            ),
            _safe(mention.extraction_source),
            _safe(mention.extraction_error),
            _safe(mention.confidence_reason),
            s.image_key or "",
            s.version,
            _utc_isoformat(s.updated_at),
            mention.id,
            mention.version,
            len(s.mentions),
            sum(item.review_status == ReviewStatus.PENDING.value for item in s.mentions),
            sum(item.review_status == ReviewStatus.DEFERRED.value for item in s.mentions),
            sum(
                item.metadata_review_status == MetadataReviewStatus.PENDING.value
                for item in s.mentions
            ),
            sum(
                item.metadata_review_status == MetadataReviewStatus.DEFERRED.value
                for item in s.mentions
            ),
            s.needs_review,
            ";".join(
                f"{item.id}:{item.version}"
                for item in sorted(s.mentions, key=lambda item: item.id)
            ),
        ])
        row_count += 1

    # Do not omit the BOM because Excel can misdetect UTF-8 CSV files.
    content = ("\ufeff" + buf.getvalue()).encode("utf-8")
    content_sha256 = hashlib.sha256(content).hexdigest()
    exported_at = datetime.now(timezone.utc)
    filename = f"meshi_archive_{exported_at:%Y%m%dT%H%M%S%fZ}_{content_sha256[:12]}.csv"
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8-sig",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Export-Generated-At": exported_at.isoformat(),
            "X-Export-Content-SHA256": content_sha256,
            "X-Export-Row-Count": str(row_count),
            "X-Export-Row-Unit": "shop",
            "X-Export-Review-Scope": "representative-mention",
        },
    )
