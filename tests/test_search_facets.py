from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Generator
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import StreamingResponse

from db.models import Base, Message, Shop, ShopMention
from services.import_service import apply_import_batch, parse_csv_bytes, stage_import
from web.routers import home as home_router


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def request() -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/", "headers": [],
        "query_string": b"", "scheme": "http", "server": ("testserver", 80),
        "session": {"authenticated": True, "admin_authenticated": True, "csrf_token": "test"},
    })


def add_shop(
    db: Session, index: int, area: str | None, *,
    category: str = "カフェ", visited: bool = False, approved: bool = True,
) -> Shop:
    shop = Shop(shop_name=f"灯台{index}", area=area, category=category, is_visited=visited)
    db.add(ShopMention(
        shop=shop, message=Message(message_id=f"8{index:016d}"), occurrence_index=0,
        extracted_name=shop.shop_name, resolution_status="resolved",
        review_status="approved", metadata_review_status="approved" if approved else "pending",
        extraction_source="test",
    ))
    db.commit()
    return shop


@pytest.mark.parametrize("query", ["江東区", "東京都", "東京", "東京都江東区", "江東区 カフェ"])
def test_keyword_matches_displayed_parent_regions(db: Session, query: str) -> None:
    target = add_shop(db, 1, "清澄白河")
    add_shop(db, 2, "札幌")
    add_shop(db, 3, "清澄白河", approved=False)
    response = home_router.home(request(), q=query, db=db)
    assert [shop.id for shop in response.context["shops"]] == [target.id]


def test_region_search_is_shared_by_incremental_results(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    add_shop(db, 1, "清澄白河")
    second = add_shop(db, 2, "門前仲町")
    monkeypatch.setattr(home_router, "PER_PAGE", 1)
    response = home_router.api_shops(request(), q="江東区", sort="name_asc", page=2, db=db)
    payload = json.loads(response.body)
    assert f'data-shop-id="{second.id}"' in payload["html"]


@pytest.mark.parametrize(("query", "category", "status"), [
    (None, "カフェ", None), ("灯台1", None, None),
    (None, None, "visited"), ("灯台", "カフェ", "unvisited"),
])
def test_area_counts_equal_results_with_other_filters(
    db: Session, query: str | None, category: str | None, status: str | None,
) -> None:
    add_shop(db, 1, "清澄白河")
    add_shop(db, 2, "清澄白河", visited=True)
    add_shop(db, 3, "札幌", category="寿司")
    add_shop(db, 4, None)
    add_shop(db, 5, "清澄白河", approved=False)
    response = home_router.home(request(), q=query, category=category, status=status, db=db)
    counts: dict[str, int] = {
        area: count for _, areas in response.context["grouped_areas"] for area, count in areas
    }
    counts["__none__"] = response.context["none_count"]
    for area, count in counts.items():
        selected = home_router.home(request(), q=query, category=category, status=status, area=area, db=db)
        assert selected.context["total"] == count
    if category == "カフェ":
        assert "札幌" not in counts


def test_selected_empty_area_is_retained_with_zero_count(db: Session) -> None:
    add_shop(db, 1, "札幌", category="寿司")
    response = home_router.home(request(), area="札幌", category="カフェ", db=db)
    assert response.context["total"] == 0
    assert '<option value="札幌" selected>札幌 (0)</option>' in response.body.decode()


@pytest.mark.parametrize(("area", "other_area", "label"), [
    ("東京都江東区 / 木場", "東京都江東区 / 新木場", "木場"),
    ("東京都江東区 / 新木場", "東京都江東区 / 木場", "新木場"),
    ("東京都府中市", "広島県府中市", "東京 / 府中市"),
    ("広島県府中市", "東京都府中市", "広島 / 府中市"),
])
def test_area_labels_preserve_exact_filter_values_and_prefecture_context(
    db: Session, area: str, other_area: str, label: str,
) -> None:
    target = add_shop(db, 1, area)
    add_shop(db, 2, other_area)

    response = home_router.home(request(), area=area, db=db)
    selected = re.search(
        rf'<option value="{re.escape(area)}"[^>]* selected>\s*([^<]*?)\s*</option>',
        response.body.decode(),
    )
    assert selected is not None and selected.group(1) == f"{label} (1)"
    assert [shop.id for shop in response.context["shops"]] == [target.id]
    assert response.context["total"] == 1
    assert response.context["active_filters"][0]["label"] == f"エリア: {label}"
    for facet in response.context["status_facets"]:
        assert parse_qs(urlsplit(facet["url"]).query)["area"] == [area]
    db.refresh(target)
    assert target.area == area


@pytest.mark.parametrize(("area", "label"), [
    ("東京都江東区 / 木場", "木場"),
    ("東京都江東区 / 新木場", "新木場"),
])
def test_zero_result_station_filter_keeps_its_label_and_stored_value(
    db: Session, area: str, label: str,
) -> None:
    add_shop(db, 1, area, category="寿司")

    response = home_router.home(request(), area=area, category="カフェ", db=db)

    assert response.context["total"] == 0
    assert f'<option value="{area}" selected>{label} (0)</option>' in response.body.decode()
    assert response.context["active_filters"][0]["label"] == f"エリア: {label}"


async def csv_content(response: StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


def test_recent_sort_survives_csv_reimport(db: Session) -> None:
    older = add_shop(db, 1, "清澄白河")
    newer = add_shop(db, 2, "銀座")
    tied = add_shop(db, 3, "札幌")
    older.created_at = datetime(2020, 1, 1, 23, 59, 59, 123456, tzinfo=timezone.utc)
    newer.created_at = tied.created_at = datetime(2020, 1, 2, tzinfo=timezone.utc)
    db.commit()
    expected_ids: list[int] = [tied.id, newer.id, older.id]
    before: dict[int, datetime] = {shop.id: shop.created_at for shop in (older, newer, tied)}
    response = home_router.home(request(), sort="created_at_desc", db=db)
    assert [shop.id for shop in response.context["shops"]] == expected_ids
    exported = home_router.export_csv(request(), sort="created_at_desc", db=db)
    assert isinstance(exported, StreamingResponse)
    batch = stage_import(db, parse_csv_bytes(asyncio.run(csv_content(exported)), "roundtrip.csv"))
    apply_import_batch(db, batch.batch_id)
    after = home_router.home(request(), sort="created_at_desc", db=db)
    assert [shop.id for shop in after.context["shops"]] == expected_ids
    assert {shop.id: shop.created_at for shop in after.context["shops"]} == before
