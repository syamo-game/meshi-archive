from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TypedDict, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from db.models import Base, Message, Shop, ShopMention
from services.import_service import parse_csv_bytes
from web.area_groups import AreaFilterOption
from web.routers import home as home_router


@dataclass
class RenderedOption:
    value: str
    text: str = ""
    selected: bool = False
    disabled: bool = False


class ListHtml(HTMLParser):
    def __init__(self, content: str) -> None:
        super().__init__(convert_charrefs=True)
        self.options: list[RenderedOption] = []
        self.detail_urls: list[str] = []
        self.shop_ids: list[int] = []
        self.area_optgroups: int = 0
        self._in_area: bool = False
        self._option: RenderedOption | None = None
        self.feed(content)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "select":
            self._in_area = attributes.get("name") == "area"
        if tag == "optgroup" and self._in_area:
            self.area_optgroups += 1
        if tag == "option" and self._in_area:
            self._option = RenderedOption(
                value=attributes.get("value") or "",
                selected="selected" in attributes,
                disabled="disabled" in attributes,
            )
            self.options.append(self._option)
        if tag == "a" and "data-shop-detail" in attributes:
            self.detail_urls.append(attributes["href"] or "")
        if tag == "li" and "data-shop-row" in attributes:
            self.shop_ids.append(int(attributes["data-shop-id"] or "0"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "option":
            self._option = None
        if tag == "select":
            self._in_area = False

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option.text += data


class IncrementalResults(TypedDict):
    html: str
    has_more: bool
    next_page: int


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def request(*, admin: bool = False) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("testserver", 80),
        "session": {"authenticated": True, "admin_authenticated": admin, "csrf_token": "test"},
    })


def add_shop(
    db: Session, index: int, area: str | None, *, name: str = "灯台",
    category: str = "カフェ", visited: bool = False, public: bool = True,
    mentions: int = 1,
) -> Shop:
    shop = Shop(shop_name=f"{name}{index:02d}", area=area, category=category, is_visited=visited)
    for occurrence in range(mentions):
        db.add(ShopMention(
            shop=shop, message=Message(message_id=f"8{index:013d}{occurrence:03d}"),
            occurrence_index=0, extracted_name=shop.shop_name, extracted_area=area,
            extracted_category=category, resolution_status="resolved", review_status="approved",
            metadata_review_status="approved" if public else "pending", extraction_source="test",
        ))
    db.flush()
    return shop


def rendered(response: Response) -> ListHtml:
    return ListHtml(bytes(response.body).decode("utf-8"))


async def csv_bytes(response: StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


def test_heading_itself_selects_parent_and_leaves_without_duplicate_options(db: Session) -> None:
    areas = ("東京都江東区", "東京都江東区 / 木場", "東京都江東区 / 新木場", "清澄白河")
    shops = [add_shop(db, index, area, mentions=2) for index, area in enumerate(areas, 1)]
    add_shop(db, 10, "銀座")
    add_shop(db, 11, "東京都江東区 / 木場", public=False)
    db.commit()

    response = home_router.home(request(), area="東京都江東区", db=db)
    options = cast(list[AreaFilterOption], response.context["area_filter_options"])
    parent = [option for option in options if option.value == "東京都江東区"]
    assert len(parent) == 1
    assert (parent[0].label, parent[0].count, parent[0].depth, parent[0].is_group) == (
        "東京 / 江東区", 4, 0, True,
    )
    children = [option for option in options if option.value in areas[1:]]
    assert len(children) == 3 and all(option.depth > 0 and not option.is_group for option in children)
    page = rendered(response)
    assert set(page.shop_ids) == {shop.id for shop in shops}
    assert response.context["total"] == 4
    assert page.area_optgroups == 0
    heading = [option for option in page.options if option.value == "東京都江東区"]
    assert len(heading) == 1 and heading[0].selected and not heading[0].disabled
    assert heading[0].text.strip() == "東京 / 江東区 (4)"
    rendered_children = [option for option in page.options if option.value in areas[1:]]
    assert all(option.text.lstrip(" \t\r\n").startswith("　") for option in rendered_children)
    assert not any("区すべて" in option.text for option in page.options)
    leaf = home_router.home(request(), area="東京都江東区 / 木場", db=db)
    assert rendered(leaf).shop_ids == [shops[1].id]


@pytest.mark.parametrize("include_city_record", [False, True])
def test_designated_city_heading_includes_subordinate_wards(
    db: Session, include_city_record: bool,
) -> None:
    shops = [add_shop(db, 1, "神奈川県横浜市中区"), add_shop(db, 2, "神奈川県横浜市西区")]
    if include_city_record:
        shops.append(add_shop(db, 3, "神奈川県横浜市"))
    add_shop(db, 4, "神奈川県川崎市中原区")
    db.commit()

    response = home_router.home(request(), area="神奈川県横浜市", db=db)
    page = rendered(response)
    assert set(page.shop_ids) == {shop.id for shop in shops}
    heading = [option for option in page.options if option.value == "神奈川県横浜市"]
    assert len(heading) == 1 and heading[0].selected
    assert heading[0].text.strip() == f"神奈川 / 横浜市 ({len(shops)})"
    ward = home_router.home(request(), area="神奈川県横浜市中区", db=db)
    assert rendered(ward).shop_ids == [shops[0].id]


@pytest.mark.parametrize(("query", "category", "status"), [
    ("灯台", None, None), (None, "カフェ", None),
    (None, None, "visited"), ("灯台", "カフェ", "unvisited"),
])
def test_heading_counts_match_results_under_current_other_filters(
    db: Session, query: str | None, category: str | None, status: str | None,
) -> None:
    add_shop(db, 1, "清澄白河", mentions=2)
    add_shop(db, 2, "東京都江東区 / 木場", visited=True)
    add_shop(db, 3, "東京都江東区", name="港", category="寿司", visited=True)
    add_shop(db, 4, "銀座", visited=True)
    add_shop(db, 5, "清澄白河", public=False, visited=True)
    db.commit()

    response = home_router.home(request(), q=query, category=category, status=status, db=db)
    options = cast(list[AreaFilterOption], response.context["area_filter_options"])
    groups = [option for option in options if option.is_group]
    assert any(option.value == "東京都江東区" for option in groups)
    for option in options:
        selected = home_router.home(
            request(), area=option.value, q=query, category=category, status=status, db=db,
        )
        assert selected.context["total"] == option.count, option.value


def test_selected_empty_heading_remains_one_selected_option(db: Session) -> None:
    add_shop(db, 1, "東京都江東区 / 木場", category="寿司")
    db.commit()
    response = home_router.home(request(), area="東京都江東区", category="カフェ", db=db)
    options = [option for option in rendered(response).options if option.value == "東京都江東区"]
    assert response.context["total"] == 0
    assert len(options) == 1 and options[0].selected and not options[0].disabled
    assert options[0].text.strip() == "東京 / 江東区 (0)"


def test_same_named_municipalities_remain_separate(db: Session) -> None:
    tokyo = add_shop(db, 1, "東京都府中市")
    hiroshima = add_shop(db, 2, "広島県府中市")
    db.commit()
    for area, expected, label in (
        ("東京都府中市", tokyo, "東京 / 府中市"),
        ("広島県府中市", hiroshima, "広島 / 府中市"),
    ):
        response = home_router.home(request(), area=area, db=db)
        page = rendered(response)
        assert page.shop_ids == [expected.id]
        options = [option for option in page.options if option.value == area]
        assert len(options) == 1 and options[0].text.strip() == f"{label} (1)"


def test_other_heading_is_filtered_and_does_not_include_unset_area(db: Session) -> None:
    target = add_shop(db, 1, "未登録の路地", visited=True)
    add_shop(db, 2, "海外の市場", name="港", visited=True)
    add_shop(db, 3, "別の未登録地域", category="寿司", visited=True)
    add_shop(db, 4, None, visited=True)
    add_shop(db, 5, "未登録の路地", public=False, visited=True)
    db.commit()
    response = home_router.home(
        request(), area="__group__:その他", q="灯台", category="カフェ", status="visited", db=db,
    )
    page = rendered(response)
    assert page.shop_ids == [target.id]
    heading = [option for option in page.options if option.value == "__group__:その他"]
    assert len(heading) == 1 and heading[0].text.strip() == "その他 (1)"
    assert heading[0].selected and not heading[0].disabled


@pytest.mark.parametrize(("parent", "leaf_areas"), [
    ("東京都江東区", ("清澄白河", "東京都江東区 / 木場", "東京都江東区")),
    ("__group__:その他", ("未登録の路地", "海外の市場", "別の未登録地域")),
])
def test_heading_scope_survives_api_csv_return_links_and_visit_facets(
    db: Session, monkeypatch: pytest.MonkeyPatch, parent: str, leaf_areas: tuple[str, str, str],
) -> None:
    targets = [add_shop(db, index, area, visited=True) for index, area in enumerate(leaf_areas, 1)]
    private = add_shop(db, 4, leaf_areas[0], visited=True, public=False)
    add_shop(db, 5, leaf_areas[1])
    add_shop(db, 6, leaf_areas[2], category="寿司", visited=True)
    add_shop(db, 7, leaf_areas[0], name="港", visited=True)
    add_shop(db, 8, "銀座", visited=True)
    db.commit()
    monkeypatch.setattr(home_router, "PER_PAGE", 2)
    expected_params: dict[str, list[str]] = {
        "q": ["灯台"], "area": [parent], "status": ["visited"],
        "category": ["カフェ"], "sort": ["name_asc"], "page": ["2"],
    }
    response = home_router.home(
        request(), area=parent, q="灯台", category="カフェ", status="visited", sort="name_asc", page=2, db=db,
    )
    assert response.context["total"] == 3
    page = rendered(response)
    assert page.shop_ids == [targets[2].id]
    facets = cast(list[home_router.StatusFacet], response.context["status_facets"])
    assert {facet["key"]: facet["count"] for facet in facets} == {"all": 4, "visited": 3, "unvisited": 1}
    for facet in facets:
        params = parse_qs(urlsplit(facet["url"]).query)
        assert params["area"] == [parent] and params["q"] == ["灯台"] and params["category"] == ["カフェ"]
    incremental = home_router.api_shops(
        request(), area=parent, q="灯台", category="カフェ", status="visited", sort="name_asc", page=2, db=db,
    )
    payload = cast(IncrementalResults, json.loads(incremental.body))
    api_page = ListHtml(payload["html"])
    assert api_page.shop_ids == [targets[2].id] and payload["has_more"] is False
    for href in page.detail_urls + api_page.detail_urls:
        return_to = parse_qs(urlsplit(href).query)["return_to"][0]
        assert parse_qs(urlsplit(return_to).query) == expected_params
    exported = home_router.export_csv(
        request(admin=True), area=parent, q="灯台", category="カフェ", status="visited", sort="name_asc", db=db,
    )
    assert isinstance(exported, StreamingResponse)
    rows = parse_csv_bytes(asyncio.run(csv_bytes(exported)), "heading.csv").rows
    assert {row.shop_id for row in rows} == {shop.id for shop in [*targets, private]}
    assert {row.area for row in rows} == set(leaf_areas)
