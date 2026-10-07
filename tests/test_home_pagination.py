from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from db.models import Base, Message, Shop, ShopMention
from web.routers import home as home_router


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "session": {
                "authenticated": True,
                "admin_authenticated": False,
                "csrf_token": "test-token",
            },
        }
    )


def add_public_shops(db: Session, count: int) -> list[Shop]:
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    shops: list[Shop] = []
    for index in range(1, count + 1):
        shop = Shop(
            shop_name="同名店",
            area="銀座",
            category="寿司",
            is_visited=True,
            created_at=created_at,
        )
        mention = ShopMention(
            message=Message(message_id=f"9{index:016d}"),
            shop=shop,
            occurrence_index=0,
            extracted_name=shop.shop_name,
            extracted_area=shop.area,
            extracted_category=shop.category,
            resolution_status="resolved",
            review_status="approved",
            metadata_review_status="approved",
            extraction_source="test",
        )
        db.add(mention)
        shops.append(shop)
    db.commit()
    return shops


def test_home_paginates_configured_items_and_preserves_filters(db: Session) -> None:
    per_page = home_router.PER_PAGE
    shops = add_public_shops(db, per_page * 2 + 1)

    response = home_router.home(
        request(),
        area="銀座",
        status="visited",
        q="同名",
        category="寿司",
        sort="name_asc",
        page=2,
        db=db,
    )

    context = response.context
    assert [shop.id for shop in context["shops"]] == [
        shop.id for shop in shops[per_page:per_page * 2]
    ]
    assert context["total"] == per_page * 2 + 1
    assert context["total_pages"] == 3
    assert context["current_page"] == 2
    assert context["page_start"] == per_page + 1
    assert context["page_end"] == per_page * 2
    assert context["per_page"] == per_page
    assert context["has_previous"] is True
    assert context["has_next"] is True
    assert context["previous_page"] == 1
    assert context["next_page"] == 3
    assert context["has_more"] is True
    assert parse_qs(context["filter_qs"]) == {
        "q": ["同名"],
        "area": ["銀座"],
        "status": ["visited"],
        "category": ["寿司"],
    }
    assert context["selected_sort"] == "name_asc"


def test_home_last_page_reports_exact_visible_range(db: Session) -> None:
    per_page = home_router.PER_PAGE
    shops = add_public_shops(db, per_page * 2 + 1)

    response = home_router.home(
        request(),
        sort="name_asc",
        page=3,
        db=db,
    )

    context = response.context
    assert [shop.id for shop in context["shops"]] == [shops[-1].id]
    assert context["page_start"] == per_page * 2 + 1
    assert context["page_end"] == per_page * 2 + 1
    assert context["has_previous"] is True
    assert context["has_next"] is False
    assert context["previous_page"] == 2
    assert context["next_page"] is None
    assert context["has_more"] is False


def test_home_keeps_one_empty_page_and_redirects_later_pages(db: Session) -> None:
    response = home_router.home(request(), page=1, db=db)

    context = response.context
    assert context["shops"] == []
    assert context["total"] == 0
    assert context["total_pages"] == 1
    assert context["current_page"] == 1
    assert context["page_start"] == 0
    assert context["page_end"] == 0
    assert context["has_previous"] is False
    assert context["has_next"] is False

    redirect = home_router.home(request(), page=2, db=db)

    assert redirect.status_code == 302
    assert redirect.headers["location"] == "/?sort=area_order"


def test_home_redirects_a_page_after_the_last_page_and_preserves_filters(
    db: Session,
) -> None:
    add_public_shops(db, home_router.PER_PAGE * 2 + 1)

    response = home_router.home(
        request(),
        q="  同名  ",
        area="銀座",
        status="visited",
        category="  寿司  ",
        sort="name_asc",
        page=4,
        db=db,
    )

    location = urlparse(response.headers["location"])
    assert response.status_code == 302
    assert location.path == "/"
    assert parse_qs(location.query) == {
        "q": ["同名"],
        "area": ["銀座"],
        "status": ["visited"],
        "category": ["寿司"],
        "sort": ["name_asc"],
        "page": ["3"],
    }


def test_home_http_redirects_a_stale_last_page_after_deletion(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shops = add_public_shops(db, home_router.PER_PAGE + 1)
    db.delete(shops[-1])
    db.commit()
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/_test/login")
    def viewer_login(request: Request) -> dict[str, bool]:
        request.session.update(authenticated=True, discord_user_id="123456789012345678")
        return {"ok": True}

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.include_router(home_router.router)
    app.dependency_overrides[home_router.get_db] = override_db
    monkeypatch.setenv("APP_ENV", "development")

    with TestClient(app) as client:
        client.get("/_test/login")
        response = client.get(
            "/",
            params={
                "q": "  同名  ",
                "area": "銀座",
                "status": "visited",
                "category": "  寿司  ",
                "sort": "name_asc",
                "page": "2",
            },
            follow_redirects=False,
        )

    location = urlparse(response.headers["location"])
    assert response.status_code == 302
    assert location.path == "/"
    assert parse_qs(location.query) == {
        "q": ["同名"],
        "area": ["銀座"],
        "status": ["visited"],
        "category": ["寿司"],
        "sort": ["name_asc"],
    }


def test_incremental_shops_api_keeps_second_page_compatibility(db: Session) -> None:
    add_public_shops(db, home_router.PER_PAGE * 2 + 1)

    response = home_router.api_shops(
        request("/api/shops"),
        sort="name_asc",
        page=2,
        db=db,
    )

    body = json.loads(response.body)
    assert body["has_more"] is True
    assert body["next_page"] == 3
    assert body["html"].count("data-shop-row") == home_router.PER_PAGE


@pytest.mark.parametrize("page", ("0", "-1", "not-a-number"))
def test_home_query_rejects_invalid_page_values(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    page: str,
) -> None:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/_test/login")
    def viewer_login(request: Request) -> dict[str, bool]:
        request.session.update(authenticated=True, discord_user_id="123456789012345678")
        return {"ok": True}

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.include_router(home_router.router)
    app.dependency_overrides[home_router.get_db] = override_db
    monkeypatch.setenv("APP_ENV", "development")

    with TestClient(app) as client:
        client.get("/_test/login")
        response = client.get("/", params={"page": page})

    assert response.status_code == 422


def test_shop_sort_uses_id_as_a_stable_tie_breaker(db: Session) -> None:
    shops = add_public_shops(db, 3)

    ascending = home_router._build_shop_query(
        db,
        area=None,
        status=None,
        sort="name_asc",
    ).all()
    descending = home_router._build_shop_query(
        db,
        area=None,
        status=None,
        sort="name_desc",
    ).all()
    area_descending = home_router._build_shop_query(
        db,
        area=None,
        status=None,
        sort="area_desc",
    ).all()

    assert [shop.id for shop in ascending] == [shop.id for shop in shops]
    assert [shop.id for shop in descending] == [shop.id for shop in reversed(shops)]
    assert [shop.id for shop in area_descending] == [
        shop.id for shop in reversed(shops)
    ]


def test_default_order_groups_tokyo_wards_then_other_areas(db: Session) -> None:
    rows: list[tuple[str | None, str]] = [
        (None, "未設定の店"),
        ("架空エリア", "分類未設定の店"),
        ("札幌", "北海道の店"),
        ("武蔵小杉", "神奈川の店"),
        ("東京都八王子市", "都内市部の店"),
        ("清澄白河", "江東区の店"),
        ("新宿", "新宿区の店"),
        ("新橋", "港区の店"),
        ("銀座", "中央区の銀座店"),
        ("日本橋", "中央区の日本橋店"),
        ("秋葉原", "千代田区の秋葉原店"),
        ("神田", "B神田店"),
        ("神田", "A神田店"),
    ]
    shops = add_public_shops(db, len(rows))
    for shop, (area, name) in zip(shops, rows, strict=True):
        shop.area = area
        shop.shop_name = name
    db.commit()

    response = home_router.home(request(), db=db)

    assert response.context["selected_sort"] == "area_order"
    assert response.context["active_filters"] == []
    assert [shop.id for shop in response.context["shops"]] == [
        shops[index].id for index in [12, 11, 10, 9, 8, 7, 6, 5, 4, 2, 3, 1, 0]
    ]


def test_default_order_is_stable_across_pages_and_incremental_api(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(home_router, "PER_PAGE", 2)
    shops = add_public_shops(db, 7)
    areas: list[str | None] = [None, "札幌", "銀座", "神田", "神田", "新橋", "東京都府中市"]
    for shop, area in zip(shops, areas, strict=True):
        shop.area = area
    db.commit()
    expected_ids = [shops[index].id for index in [3, 4, 2, 5, 6, 1, 0]]
    actual_ids: list[int] = []
    for page in range(1, 5):
        response = home_router.home(request(), page=page, db=db)
        assert response.context["selected_sort"] == "area_order"
        actual_ids.extend(shop.id for shop in response.context["shops"])
    assert actual_ids == expected_ids

    body = json.loads(home_router.api_shops(request("/api/shops"), page=2, db=db).body)
    assert body["html"].index(f'data-shop-id="{shops[2].id}"') < body["html"].index(
        f'data-shop-id="{shops[5].id}"'
    )


def test_default_order_keeps_aliases_together_and_empty_areas_last(db: Session) -> None:
    shops = add_public_shops(db, 5)
    rows: list[tuple[str | None, str]] = [
        ("銀座", "B店"), ("東京都中央区 / 銀座駅", "A店"),
        (None, "B未設定"), ("", "A未設定"), ("東京", "旧東京エリア"),
    ]
    for shop, (area, name) in zip(shops, rows, strict=True):
        shop.area, shop.shop_name = area, name
    db.commit()

    results = home_router._build_shop_query(db, area=None, status=None).all()

    assert [shop.id for shop in results] == [shops[index].id for index in [4, 1, 0, 3, 2]]
