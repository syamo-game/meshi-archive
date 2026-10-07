from __future__ import annotations

import base64
import io
import json
from collections.abc import Generator
from datetime import datetime, timezone
from hashlib import sha384
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from db.models import Base, Message, Shop, ShopMention, SourceAsset
from services.shop_image_upload import save_uploaded_image
from web.routers.home import _validate_return_to, api_shops, home, shop_detail, shop_edit, templates


class HtmlElements(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.elements.append((tag, dict(attrs)))

    def find(
        self,
        tag: str,
        *,
        attribute: str | None = None,
        value: str | None = None,
    ) -> list[dict[str, str | None]]:
        matches: list[dict[str, str | None]] = []
        for element_tag, attrs in self.elements:
            if element_tag != tag:
                continue
            if attribute is not None and attribute not in attrs:
                continue
            if value is not None:
                if attribute == "class":
                    if not set(value.split()).issubset((attrs.get("class") or "").split()):
                        continue
                elif attrs.get(attribute) != value:
                    continue
            matches.append(attrs)
        return matches


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_request(path: str, *, admin: bool = False) -> Request:
    parsed = urlsplit(path)
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": parsed.path,
            "query_string": parsed.query.encode("utf-8"),
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "session": {
                "authenticated": True,
                "admin_authenticated": admin,
                "csrf_token": "test-token",
            },
        }
    )


def add_public_shop(
    db: Session,
    *,
    index: int,
    name: str,
    area: str | None = "銀座",
    category: str = "寿司",
    canonical_url: str | None = None,
    source_url: str | None = None,
    image_url: str | None = None,
    visited: bool = False,
) -> Shop:
    shop = Shop(
        shop_name=name,
        area=area,
        category=category,
        canonical_url=canonical_url,
        is_visited=visited,
    )
    message = Message(message_id=f"8{index:016d}")
    mention = ShopMention(
        message=message,
        shop=shop,
        occurrence_index=0,
        extracted_name=name,
        extracted_area=area,
        extracted_category=category,
        source_url=source_url,
        resolution_status="resolved",
        review_status="approved",
        metadata_review_status="approved",
        extraction_source="test",
    )
    if image_url:
        message.assets.append(
            SourceAsset(
                kind="image",
                url=image_url,
                fetch_status="available",
            )
        )
    db.add(mention)
    db.flush()
    return shop


def render_html(response: Response) -> tuple[str, HtmlElements]:
    html = response.body.decode("utf-8")
    parser = HtmlElements()
    parser.feed(html)
    return html, parser


def class_contains(attrs: dict[str, str | None], class_name: str) -> bool:
    return class_name in (attrs.get("class") or "").split()


def query_values(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


@pytest.mark.parametrize(
    "return_to",
    (
        "/",
        "/?q=%E5%AF%BF%E5%8F%B8+%26+%E9%85%92&area=%E9%8A%80%E5%BA%A7&page=2",
    ),
)
def test_validate_return_to_accepts_local_list_urls(return_to: str) -> None:
    assert _validate_return_to(return_to) == return_to


@pytest.mark.parametrize(
    "return_to",
    (
        "//evil.example/path",
        "http://evil.example/path",
        "https://evil.example/path",
        "javascript:alert(1)",
        "/shop/1",
        "/#section",
        "/?q=sushi\nredirect=evil",
        "/?q=sushi\x00redirect=evil",
        "/?q=sushi\x7fredirect=evil",
    ),
)
def test_validate_return_to_rejects_non_list_or_unsafe_urls(return_to: str) -> None:
    with pytest.raises(HTTPException) as raised:
        _validate_return_to(return_to)

    assert raised.value.status_code == 400


def test_home_keeps_internal_details_and_exposes_public_external_links(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("web.routers.home.DISCORD_GUILD_ID", None)
    monkeypatch.setattr("web.routers.home.DISCORD_CHANNEL_ID", None)
    canonical_url = "https://official.example/shop"
    source_only_url = "https://social.example/post"
    canonical_shop = add_public_shop(
        db,
        index=1,
        name="公式URLあり",
        canonical_url=canonical_url,
        source_url="https://social.example/official-post",
    )
    canonical_shop.address = "東京都中央区銀座1-1-1"
    source_only_shop = add_public_shop(
        db,
        index=2,
        name="投稿元URLのみ",
        source_url=source_only_url,
    )
    db.commit()

    response = home(make_request("/"), sort="name_asc", db=db)
    _, parser = render_html(response)

    detail_links = parser.find("a", attribute="data-shop-detail")
    detail_by_shop_id = {attrs["data-shop-id"]: attrs for attrs in detail_links}
    assert set(detail_by_shop_id) == {
        str(canonical_shop.id),
        str(source_only_shop.id),
    }
    for shop in (canonical_shop, source_only_shop):
        detail = detail_by_shop_id[str(shop.id)]
        href = detail["href"]
        assert href is not None
        assert urlsplit(href).path == f"/shop/{shop.id}"

    action_links = [
        attrs
        for attrs in parser.find("a")
        if class_contains(attrs, "shop-action-link")
    ]
    action_hrefs = {attrs["href"] for attrs in action_links}
    assert canonical_url not in action_hrefs
    assert "https://social.example/official-post" in action_hrefs
    assert source_only_url in action_hrefs

    map_links = [
        attrs
        for attrs in action_links
        if (attrs.get("href") or "").startswith(
            "https://www.google.com/maps/search/?api=1&query="
        )
    ]
    assert len(map_links) == 2
    for attrs in action_links:
        assert attrs.get("target") == "_blank"
        assert attrs.get("rel") == "noopener noreferrer"
        assert attrs.get("aria-label")
        assert class_contains(attrs, "link-secondary")
        assert not class_contains(attrs, "btn")

    detail_response = shop_detail(
        canonical_shop.id,
        make_request(f"/shop/{canonical_shop.id}"),
        db=db,
    )
    detail_html, _ = render_html(detail_response)
    assert canonical_url in detail_html
    assert "social.example" in detail_html
    assert "関連ページを見る" in detail_html
    assert detail_html.index("place-detail__actions") < detail_html.index(
        "place-detail__facts"
    )


@pytest.mark.parametrize(
    "source_url",
    (
        "https://x.com/example/status/25",
        "https://x.com/i/web/status/25",
        "https://mobile.twitter.com/example/status/25",
    ),
)
def test_public_links_use_short_labels_without_losing_their_destinations(
    db: Session,
    source_url: str,
) -> None:
    shop = add_public_shop(db, index=25, name="短いボタン名の店", source_url=source_url)
    db.commit()

    list_html, list_parser = render_html(home(make_request("/"), db=db))
    assert "</svg> Map" in list_html
    assert "</svg> X" in list_html
    assert "Xの投稿" not in list_html
    assert "↗" not in list_html
    assert "place-card__detail-arrow" not in list_html
    source_link = list_parser.find("a", attribute="href", value=source_url)[0]
    assert source_link["aria-label"] == (
        "短いボタン名の店のX（新しいタブで開きます）"
    )
    assert source_link["rel"] == "noopener noreferrer"
    assert source_link["target"] == "_blank"

    detail_html, detail_parser = render_html(
        shop_detail(shop.id, make_request(f"/shop/{shop.id}"), db=db)
    )
    assert "</svg> Map" in detail_html
    assert "</svg> X" in detail_html
    assert "Xの投稿" not in detail_html
    assert "↗" not in detail_html
    for parser in (list_parser, detail_parser):
        icons = parser.find("svg", attribute="class", value="place-link-icon")
        assert len(icons) == 2
        assert all(icon.get("aria-hidden") == "true" for icon in icons)
        assert all(icon.get("focusable") == "false" for icon in icons)
    detail_source = detail_parser.find("a", attribute="href", value=source_url)[0]
    assert detail_source["aria-label"] == source_link["aria-label"]
    detail_map = [
        attrs
        for attrs in detail_parser.find("a", attribute="class")
        if class_contains(attrs, "place-detail-action--map")
    ]
    assert len(detail_map) == 1
    assert class_contains(detail_map[0], "link-secondary")
    assert not class_contains(detail_map[0], "btn")
    assert not class_contains(detail_source, "btn")


def test_store_site_and_discord_links_only_appear_in_detail(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("web.routers.home.DISCORD_GUILD_ID", "guild")
    monkeypatch.setattr("web.routers.home.DISCORD_CHANNEL_ID", "channel")
    canonical_url = "https://official.example/detail-only"
    shop = add_public_shop(
        db,
        index=17,
        name="詳細限定リンク店",
        canonical_url=canonical_url,
        source_url="https://social.example/detail-only",
    )
    db.commit()

    list_response = home(make_request("/"), db=db)
    list_html, _ = render_html(list_response)
    discord_url = (
        "https://discord.com/channels/guild/channel/"
        f"{shop.message_id}"
    )
    assert canonical_url not in list_html
    assert discord_url not in list_html

    detail_response = shop_detail(
        shop.id,
        make_request(f"/shop/{shop.id}"),
        db=db,
    )
    detail_html, _ = render_html(detail_response)
    assert canonical_url in detail_html
    assert discord_url in detail_html


def test_home_keeps_all_search_conditions_visible_with_one_submit(
    db: Session,
) -> None:
    add_public_shop(db, index=12, name="並び順確認店")
    db.commit()

    response = home(make_request("/"), sort="rating_desc", db=db)
    html, parser = render_html(response)

    assert "<details" not in html.split("<main", 1)[1]
    assert parser.find("input", attribute="id", value="q-input")
    assert {attrs["name"] for attrs in parser.find("select")} == {
        "area", "category", "status", "sort",
    }
    sort_selects = parser.find("select", attribute="name", value="sort")
    assert len(sort_selects) == 1
    assert sort_selects[0]["id"] == "sort-select"
    assert parser.find("input", attribute="name", value="sort") == []
    submit_buttons = parser.find("button", attribute="type", value="submit")
    assert len(submit_buttons) == 1
    assert class_contains(submit_buttons[0], "explorer-search__submit")
    assert html.index('id="sort-select"') < html.index('class="explorer-search__submit')
    sort_options = parser.find("option", attribute="value")
    supported_sorts = {
        "area_order",
        "name_asc",
        "name_desc",
        "area_asc",
        "area_desc",
        "rating_desc",
        "rating_asc",
    }
    rendered_sorts = {
        attrs["value"]
        for attrs in sort_options
        if attrs.get("value") in supported_sorts
    }
    assert rendered_sorts == supported_sorts
    selected_sorts = [
        attrs["value"]
        for attrs in sort_options
        if attrs.get("value") in supported_sorts and "selected" in attrs
    ]
    assert selected_sorts == ["rating_desc"]
    assert len(parser.find("a", attribute="class", value="explorer-active-filters__clear")) == 1
    assert "評価が高い順の条件を解除" in html


def test_search_form_preserves_selected_visit_status(db: Session) -> None:
    add_public_shop(db, index=120, name="訪問条件保持店", visited=True)
    db.commit()

    response = home(make_request("/?status=visited"), status="visited", db=db)
    html, parser = render_html(response)

    status_selects = parser.find("select", attribute="name", value="status")
    assert len(status_selects) == 1
    assert status_selects[0]["id"] == "status-select"
    selected_options = [
        attrs
        for attrs in parser.find("option", attribute="data-status-facet")
        if "selected" in attrs
    ]
    assert [attrs["data-status-facet"] for attrs in selected_options] == ["visited"]
    assert "<details" not in html.split("<main", 1)[1]


def test_visit_status_can_be_cleared_from_the_active_conditions(db: Session) -> None:
    add_public_shop(db, index=121, name="訪問タブ確認店")
    db.commit()

    response = home(
        make_request("/?status=unvisited"),
        status="unvisited",
        db=db,
    )
    _, parser = render_html(response)

    assert parser.find("nav", attribute="class", value="status-switcher") == []
    chips = parser.find("a", attribute="class", value="filter-chip")
    assert len(chips) == 1
    assert chips[0]["aria-label"] == "未訪問の条件を解除"
    assert "status" not in query_values(chips[0]["href"] or "")


def test_condition_chips_remove_only_the_chosen_condition(db: Session) -> None:
    add_public_shop(db, index=122, name="銀座の店", visited=True)
    db.commit()
    response = home(
        make_request("/"), q="銀座", area="銀座", category="寿司",
        status="visited", sort="rating_desc", db=db,
    )
    _, parser = render_html(response)
    chips = {
        attrs["aria-label"]: query_values(attrs["href"] or "")
        for attrs in parser.find("a", attribute="class", value="filter-chip")
    }
    assert chips["訪問済みの条件を解除"] == {
        "q": ["銀座"], "area": ["銀座"], "category": ["寿司"],
        "sort": ["rating_desc"],
    }
    assert chips["評価が高い順の条件を解除"] == {
        "q": ["銀座"], "area": ["銀座"], "category": ["寿司"],
        "status": ["visited"], "sort": ["area_order"],
    }


def test_home_renders_semantic_place_list_and_live_regions(db: Session) -> None:
    add_public_shop(db, index=13, name="読み上げ確認店")
    db.commit()

    response = home(make_request("/"), sort="name_asc", db=db)
    _, parser = render_html(response)

    result_headings = parser.find("h2", attribute="id", value="result-heading")
    assert result_headings[0]["tabindex"] == "-1"
    place_lists = parser.find("ol", attribute="id", value="shop-list")
    assert len(place_lists) == 1
    place_regions = [
        attrs
        for attrs in parser.find("div", attribute="role", value="region")
        if class_contains(attrs, "place-results")
    ]
    assert len(place_regions) == 1
    assert "tabindex" not in place_regions[0]
    cards = parser.find("article", attribute="aria-labelledby")
    assert len(cards) == 1
    assert parser.find("th", attribute="aria-sort") == []
    dialog_bodies = parser.find("div", attribute="id", value="detail-dialog-body")
    assert len(dialog_bodies) == 1
    assert "aria-live" not in dialog_bodies[0]
    dialog_statuses = parser.find("p", attribute="id", value="detail-dialog-status")
    assert dialog_statuses == [
        {
            "id": "detail-dialog-status",
            "class": "visually-hidden",
            "role": "status",
            "aria-live": "polite",
        }
    ]


def test_list_warns_when_map_search_has_no_location(db: Session) -> None:
    shop = add_public_shop(
        db,
        index=14,
        name="同名注意店",
        area=None,
        source_url="https://social.example/no-location",
    )
    db.commit()

    response = home(make_request("/"), db=db)
    html, parser = render_html(response)

    assert "エリア未設定" in html
    map_links = [
        attrs
        for attrs in parser.find("a", attribute="aria-label")
        if (attrs.get("href") or "").startswith(
            "https://www.google.com/maps/search/?api=1&query="
        )
    ]
    assert map_links[0]["aria-label"] == (
        f"{shop.shop_name}をGoogle Mapsで検索。場所情報は未設定です"
        "（新しいタブで開きます）"
    )
    assert "Google Maps" in html

    detail_response = shop_detail(
        shop.id,
        make_request(f"/shop/{shop.id}"),
        db=db,
    )
    detail_html, _ = render_html(detail_response)
    assert "Google Mapsで検索" in detail_html
    assert "場所情報がないため" not in detail_html
    assert "place-detail__map-note" not in detail_html


def test_missing_source_link_is_explained_in_list_and_detail(db: Session) -> None:
    shop = add_public_shop(db, index=140, name="保存元なし店", source_url=None)
    db.commit()

    list_response = home(make_request("/"), db=db)
    list_html, _ = render_html(list_response)
    assert "保存元なし" in list_html

    detail_response = shop_detail(shop.id, make_request(f"/shop/{shop.id}"), db=db)
    detail_html, _ = render_html(detail_response)
    assert "保存元へのリンクは登録されていません。" in detail_html


def test_editing_shop_as_unvisited_clears_submitted_visit_date(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    shop = add_public_shop(db, index=141, name="訪問日整合店", visited=True)
    shop.visited_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    db.commit()

    shop_edit(
        expected_version=str(shop.version),
        shop_id=shop.id,
        request=make_request(f"/shop/{shop.id}/edit", admin=True),
        shop_name=shop.shop_name,
        area=shop.area,
        category=shop.category,
        url=None,
        address=None,
        phone=None,
        memo=None,
        rating=None,
        is_visited=None,
        visited_at="2026-09-01",
        csrf_token="test-token",
        return_to="/",
        db=db,
    )

    assert shop.is_visited is False
    assert shop.visited_at is None


def test_editing_visited_shop_can_clear_visit_date(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    shop = add_public_shop(db, index=142, name="訪問日空欄店", visited=True)
    shop.visited_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    db.commit()

    shop_edit(
        expected_version=str(shop.version),
        shop_id=shop.id,
        request=make_request(f"/shop/{shop.id}/edit", admin=True),
        shop_name=shop.shop_name,
        area=shop.area,
        category=shop.category,
        url=None,
        address=None,
        phone=None,
        memo=None,
        rating=None,
        is_visited="on",
        visited_at="",
        csrf_token="test-token",
        return_to="/",
        db=db,
    )

    assert shop.is_visited is True
    assert shop.visited_at is None


def test_google_maps_source_is_labeled_as_a_registered_map(db: Session) -> None:
    shop = add_public_shop(
        db,
        index=18,
        name="地図登録店",
        source_url="https://maps.app.goo.gl/example-map",
    )
    db.commit()

    list_response = home(make_request("/"), db=db)
    list_html, _ = render_html(list_response)
    assert "登録時の地図" in list_html

    detail_response = shop_detail(
        shop.id,
        make_request(f"/shop/{shop.id}"),
        db=db,
    )
    detail_html, _ = render_html(detail_response)
    assert "登録時の地図を開く" in detail_html


def test_missing_selected_filters_remain_visible_as_zero_count_options(
    db: Session,
) -> None:
    response = home(
        make_request("/"),
        area="削除済みエリア",
        category="削除済みカテゴリ",
        db=db,
    )
    _, parser = render_html(response)
    options = [
        attrs
        for attrs in parser.find("option", attribute="selected")
        if "data-status-facet" not in attrs
    ]
    assert {attrs.get("value") for attrs in options} == {
        "削除済みエリア",
        "削除済みカテゴリ",
        "area_order",
    }

    no_area_response = home(make_request("/"), area="__none__", db=db)
    _, no_area_parser = render_html(no_area_response)
    no_area_options = no_area_parser.find("option", attribute="selected")
    assert any(attrs.get("value") == "__none__" for attrs in no_area_options)


def test_visited_labels_follow_status_even_with_missing_or_stale_date(db: Session) -> None:
    visited_without_date = add_public_shop(
        db,
        index=15,
        name="訪問日なし",
        visited=True,
    )
    unvisited_with_stale_date = add_public_shop(
        db,
        index=16,
        name="未訪問",
        visited=False,
    )
    stale_date = datetime(2025, 1, 2, tzinfo=timezone.utc)
    visited_with_date = add_public_shop(
        db,
        index=17,
        name="訪問日あり",
        visited=True,
    )
    visited_with_date.visited_at = stale_date
    unvisited_with_stale_date.visited_at = stale_date
    db.commit()

    response = home(make_request("/"), sort="name_asc", db=db)
    _, parser = render_html(response)
    visited_labels = [
        attrs
        for attrs in parser.find("span", attribute="title")
        if class_contains(attrs, "place-card__visited-mark")
    ]
    assert {attrs["title"] for attrs in visited_labels} == {
        "訪問済み（訪問日: 2025-01-02）",
        "訪問済み（訪問日未設定）",
    }
    assert len(visited_labels) == 2
    assert all(attrs["aria-label"] == "訪問済み" for attrs in visited_labels)

    detail_response = shop_detail(
        unvisited_with_stale_date.id,
        make_request(f"/shop/{unvisited_with_stale_date.id}"),
        db=db,
    )
    detail_html, _ = render_html(detail_response)
    assert stale_date.strftime("%Y-%m-%d") not in detail_html


def test_css_keeps_compact_cards_and_mobile_touch_targets() -> None:
    css = Path("web/static/css/explorer.css").read_text(encoding="utf-8")
    base = Path("web/templates/base.html").read_text(encoding="utf-8")
    explore = Path("web/templates/explore.html").read_text(encoding="utf-8")
    admin = Path("web/templates/admin.html").read_text(encoding="utf-8")

    assert ".place-grid" in css
    assert ".place-card" in css
    assert "explorer-field--category" in explore
    assert "form-select" in explore
    assert "btn btn-primary" in explore
    assert 'class="detail-dialog place-detail-dialog modal"' in explore
    assert 'class="detail-dialog__panel modal-content"' in explore
    assert ".place-detail-dialog[open] { display: block; }" in css
    assert "width: min(36rem, calc(100% - 2rem))" in css
    assert "width: calc(100% - 1rem)" in css
    assert ".detail-dialog__close { box-sizing: border-box; width: 44px; height: 44px" in css
    assert ".detail-dialog__body { flex: 0 1 auto; }" in css
    assert "html.is-dialog-open" in css
    assert "--app-control-size: 2.5rem" in css
    assert "--app-control-size: 2.75rem" in css
    assert "/static/css/explorer.css?v=16" in base
    assert ".place-card__visited-mark" in css
    assert ".star-rating--empty .star" in css
    assert "/static/js/main.js?v=28" in base
    assert "/static/vendor/bootstrap-5.3.8/bootstrap.min.css" in base
    assert ".explorer-filters__summary" not in css
    assert "outline: 2px solid var(--bs-primary)" in css
    assert "max-width: 1440px" not in css
    assert "var(--place-card-min-width)" in css
    assert "repeat(auto-fill" in css
    assert "--place-card-min-width: 16rem" in css
    assert "gap: var(--place-card-gap)" in css
    assert ".place-action, .place-detail-action" in css
    assert ".site-account-menu__toggle" in css
    assert "width: 44px; height: 44px" in css
    assert "'Noto Sans JP'" in css
    assert "family=Noto+Sans+JP" in base
    assert ".pagination__page.is-current { background: var(--bs-secondary-bg)" in css
    assert ".status-switcher" not in css
    assert "行きたい店を、すぐ見つける。" not in explore
    assert 'href="/export.csv"' not in explore
    assert 'href="/export.csv"' in Path("web/templates/admin_exports.html").read_text(encoding="utf-8")
    assert 'aria-label="店舗一覧のCSVをダウンロード"' in Path("web/templates/admin_exports.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ("/login", "/admin/login", "/admin", "/admin/review"))
def test_bootstrap_is_local_and_shared_with_admin_pages(db: Session, path: str) -> None:
    add_public_shop(db, index=29, name="フレームワーク確認店")
    db.commit()
    _, parser = render_html(home(make_request("/"), db=db))
    stylesheets = {attrs["href"] for attrs in parser.find("link", attribute="rel", value="stylesheet")}
    assert "/static/vendor/bootstrap-5.3.8/bootstrap.min.css" in stylesheets
    assert "/static/css/explorer.css?v=16" in stylesheets
    assert not any((href or "").startswith("/static/css/admin.css?") for href in stylesheets)
    assert not any((href or "").startswith("/static/css/style.css") for href in stylesheets)

    request = make_request(path, admin=True)
    _, admin_parser = render_html(templates.TemplateResponse(request, "base.html", {"csrf_token": "test-token"}))
    admin_stylesheets = {attrs["href"] for attrs in admin_parser.find("link", attribute="rel", value="stylesheet")}
    assert "/static/vendor/bootstrap-5.3.8/bootstrap.min.css" in admin_stylesheets
    assert "/static/css/explorer.css?v=16" in admin_stylesheets
    assert any((href or "").startswith("/static/css/admin.css?") for href in admin_stylesheets)
    assert not any((href or "").startswith("/static/css/style.css") for href in admin_stylesheets)


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("detail", [False, True])
def test_public_pages_share_role_based_header_menu(
    db: Session, admin: bool, detail: bool,
) -> None:
    shop = add_public_shop(db, index=30, name="閲覧画面の確認店")
    db.commit()
    if detail:
        response = shop_detail(shop.id, make_request(f"/shop/{shop.id}", admin=admin), db=db)
    else:
        response = home(make_request("/", admin=admin), db=db)
    html, _ = render_html(response)
    header = html.split('<header class="site-header', 1)[1].split("</header>", 1)[0]
    assert 'class="site-footer' not in html
    assert ('href="/admin"' in header) is admin
    assert 'href="/logout"' in header
    assert 'href="/admin/review"' not in header

    bootstrap = Path("web/static/vendor/bootstrap-5.3.8/bootstrap.min.css").read_bytes()
    assert base64.b64encode(sha384(bootstrap.removesuffix(b"\n")).digest()).decode("ascii") == (
        "sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB"
    )
    assert "MIT License" in Path("web/static/vendor/bootstrap-5.3.8/LICENSE").read_text(encoding="utf-8")


@pytest.mark.parametrize("page", (1, 2))
def test_bootstrap_pagination_styles_both_controls_and_the_disabled_state(
    db: Session,
    page: int,
) -> None:
    for index in range(1, 26):
        add_public_shop(db, index=index, name=f"ページ確認店 {index:02d}")
    db.commit()
    _, parser = render_html(home(make_request("/"), page=page, db=db))
    controls = parser.find("a", attribute="class", value="pagination__control")
    assert len(controls) == 2
    assert all(class_contains(attrs, "page-link") for attrs in controls)
    disabled = [attrs for attrs in controls if attrs.get("aria-disabled") == "true"]
    assert len(disabled) == 1
    assert class_contains(disabled[0], "disabled")
    assert disabled[0].get("href") is None
    assert disabled[0]["tabindex"] == "-1"


def test_list_card_uses_a_processed_sixteen_by_nine_image_when_available(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    image_url = "https://media.discordapp.net/external/food.jpg"
    shop = add_public_shop(
        db,
        index=18,
        name="画像表示店",
        source_url="https://x.com/example/status/18",
        image_url=image_url,
    )
    source = io.BytesIO()
    Image.new("RGB", (100, 100), "orange").save(source, format="PNG")
    saved_image = save_uploaded_image(source.getvalue())
    shop.image_key = saved_image.image_key
    db.commit()

    response = home(make_request("/"), db=db)
    html, parser = render_html(response)

    assert "place-card--with-image" in html
    media = [
        attrs
        for attrs in parser.find("div", attribute="data-shop-media")
        if class_contains(attrs, "place-card__media")
    ]
    assert len(media) == 1
    assert media[0].get("aria-hidden") is None
    assert len(parser.find("a", attribute="data-shop-image-link")) == 1
    assert html.index('data-shop-media') < html.index('place-card__content')
    assert html.index('place-card__content') < html.index('place-card__title')
    assert html.index('place-card__meta') < html.index('place-card__footer')
    cards = parser.find("article", attribute="aria-labelledby")
    assert len(cards) == 1
    assert class_contains(cards[0], "h-100")
    assert not class_contains(cards[0], "p-3")
    assert parser.find("div", attribute="class", value="place-card__content card-body")
    images = parser.find("img", attribute="data-shop-image")
    assert len(images) == 1
    assert images[0]["src"] == saved_image.public_url
    assert images[0]["width"] == "320"
    assert images[0]["height"] == "180"
    assert images[0]["loading"] == "lazy"
    assert images[0]["decoding"] == "async"
    assert images[0]["referrerpolicy"] == "no-referrer"
    assert images[0]["alt"] == ""
    assert class_contains(images[0], "card-img-top")
    assert "aspect-ratio: 16 / 9" in Path("web/static/css/explorer.css").read_text(
        encoding="utf-8"
    )


def test_cards_without_images_keep_the_same_cover_and_card_sizing(db: Session) -> None:
    add_public_shop(db, index=19, name="画像なし店")
    db.commit()

    html, parser = render_html(home(make_request("/"), db=db))

    assert parser.find("a", attribute="data-shop-media") == []
    assert parser.find("a", attribute="data-shop-image-link") == []
    covers = parser.find("div", attribute="data-shop-media")
    assert len(covers) == 1
    assert class_contains(covers[0], "place-card__media--empty")
    assert covers[0]["aria-hidden"] == "true"
    assert parser.find("img", attribute="data-shop-image") == []
    assert "place-card--with-image" not in html
    cards = parser.find("article", attribute="aria-labelledby")
    assert len(cards) == 1
    assert class_contains(cards[0], "h-100")
    css = Path("web/static/css/explorer.css").read_text(encoding="utf-8")
    gallery = css.split(".place-grid {", 1)[1].split("}", 1)[0]
    assert "align-items: stretch" in gallery
    assert "background: var(--bs-secondary-bg)" in css
    assert "5.5rem" not in css


def test_list_row_uses_plain_category_and_five_star_rating(db: Session) -> None:
    shop = add_public_shop(
        db,
        index=7,
        name="表示確認店",
        category="中華料理",
        visited=True,
    )
    shop.rating = 3
    db.commit()

    response = home(make_request("/"), sort="name_asc", db=db)
    html, parser = render_html(response)

    assert 'class="category-tag"' not in html
    readonly_ratings = [
        attrs
        for attrs in parser.find("span", attribute="aria-label")
        if class_contains(attrs, "star-rating--readonly")
    ]
    assert len(readonly_ratings) == 1
    assert readonly_ratings[0]["aria-label"] == "評価 3点/5点"
    assert readonly_ratings[0]["role"] == "img"
    stars = [
        attrs
        for attrs in parser.find("span")
        if class_contains(attrs, "star")
    ]
    assert len(stars) == 5
    assert sum(class_contains(attrs, "star--active") for attrs in stars) == 3
    assert html.index("place-card__rating") < html.index("place-card__meta")
    assert parser.find("ol", attribute="class", value="place-grid")
    visited_marks = parser.find(
        "span", attribute="class", value="place-card__visited-mark"
    )
    assert len(visited_marks) == 1
    assert visited_marks[0]["aria-label"] == "訪問済み"

    admin_response = home(make_request("/", admin=True), sort="name_asc", db=db)
    _, admin_parser = render_html(admin_response)
    assert admin_parser.find("button", attribute="data-visited") == []
    assert admin_parser.find("span", attribute="data-rating") == []
    assert admin_parser.find(
        "span", attribute="class", value="star-rating--readonly"
    ) == readonly_ratings
    assert admin_parser.find(
        "span", attribute="class", value="place-card__visited-mark"
    ) == visited_marks

    detail_response = shop_detail(
        shop.id,
        make_request(f"/shop/{shop.id}"),
        db=db,
    )
    _, detail_parser = render_html(detail_response)
    detail_ratings = [
        attrs
        for attrs in detail_parser.find("span", attribute="aria-label")
        if class_contains(attrs, "star-rating--readonly")
    ]
    assert len(detail_ratings) == 1
    assert detail_ratings[0]["role"] == "img"


def test_unrated_cards_render_five_empty_stars(db: Session) -> None:
    add_public_shop(db, index=30, name="未評価店")
    db.commit()

    _, parser = render_html(home(make_request("/"), db=db))

    ratings = [
        attrs
        for attrs in parser.find("span", attribute="aria-label")
        if class_contains(attrs, "star-rating--readonly")
    ]
    assert len(ratings) == 1
    assert ratings[0]["aria-label"] == "未評価"
    assert class_contains(ratings[0], "star-rating--empty")
    stars = [attrs for attrs in parser.find("span") if class_contains(attrs, "star")]
    assert len(stars) == 5
    assert not any(class_contains(attrs, "star--active") for attrs in stars)


@pytest.mark.parametrize("admin", (False, True))
@pytest.mark.parametrize("visited", (False, True))
@pytest.mark.parametrize("rating", (None, 4))
def test_initial_and_incremental_cards_keep_visit_and_rating_readonly(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    admin: bool,
    visited: bool,
    rating: int | None,
) -> None:
    monkeypatch.setattr("web.routers.home.PER_PAGE", 1)
    for index in (51, 52):
        shop = add_public_shop(
            db, index=index, name=f"一覧表示{index}", visited=visited
        )
        shop.rating = rating
    db.commit()

    _, initial_parser = render_html(
        home(make_request("/", admin=admin), sort="name_asc", db=db)
    )
    incremental_response = api_shops(
        make_request("/api/shops?page=2", admin=admin),
        page=2,
        sort="name_asc",
        db=db,
    )
    assert incremental_response.status_code == 200
    incremental_html: str = json.loads(incremental_response.body)["html"]
    incremental_parser = HtmlElements()
    incremental_parser.feed(incremental_html)

    for parser in (initial_parser, incremental_parser):
        cards = parser.find("article", attribute="class", value="place-card")
        assert len(cards) == 1
        assert not class_contains(cards[0], "place-card--editable")
        assert parser.find("button", attribute="data-visited") == []
        assert parser.find("button", attribute="class", value="star") == []
        assert parser.find("span", attribute="data-rating") == []
        ratings = parser.find("span", attribute="class", value="star-rating--readonly")
        assert len(ratings) == 1
        assert ratings[0]["role"] == "img"
        assert ratings[0]["aria-label"] == (
            f"評価 {rating}点/5点" if rating is not None else "未評価"
        )
        stars = parser.find("span", attribute="class", value="star")
        assert len(stars) == 5
        assert sum(class_contains(star, "star--active") for star in stars) == (rating or 0)
        assert len(parser.find(
            "span", attribute="class", value="place-card__visited-mark"
        )) == int(visited)
        assert parser.find("div", attribute="class", value="place-card__signals") == []
        assert parser.find("div", attribute="class", value="place-card__status") == []


@pytest.mark.parametrize("admin", (False, True))
def test_readonly_cards_keep_memo_and_admin_review_notice(db: Session, admin: bool) -> None:
    shop = add_public_shop(db, index=53, name="メモと確認表示")
    shop.memo = "ランチは予約がおすすめ"
    db.add(ShopMention(
        message=Message(message_id="80000000000000054"),
        shop=shop,
        occurrence_index=0,
        extracted_name=shop.shop_name,
        review_status="pending",
        metadata_review_status="pending",
        extraction_source="test",
    ))
    db.commit()

    html, parser = render_html(home(make_request("/", admin=admin), db=db))

    assert len(parser.find("div", attribute="class", value="place-card__signals")) == 1
    assert len(parser.find("p", attribute="class", value="place-card__memo")) == 1
    assert "ランチは予約がおすすめ" in html
    assert len(parser.find("span", attribute="class", value="place-status--review")) == int(admin)
    assert len(parser.find("div", attribute="class", value="place-card__status")) == int(admin)
    assert parser.find("button", attribute="data-visited") == []
    assert parser.find("button", attribute="class", value="star") == []


def test_home_cards_reference_their_visible_heading(db: Session) -> None:
    shops = [
        add_public_shop(db, index=3, name="行見出しA"),
        add_public_shop(db, index=4, name="行見出しB"),
    ]
    db.commit()

    response = home(make_request("/"), sort="name_asc", db=db)
    _, parser = render_html(response)

    heading_ids = {
        attrs["id"]
        for attrs in parser.find("h3", attribute="id")
    }
    assert heading_ids == {f"shop-name-{shop.id}" for shop in shops}

    cards = parser.find("article", attribute="aria-labelledby")
    assert {attrs["aria-labelledby"] for attrs in cards} == heading_ids
    rows = parser.find("li", attribute="data-shop-row")
    assert {attrs["id"] for attrs in rows} == {
        f"shop-row-{shop.id}" for shop in shops
    }


@pytest.mark.parametrize(("sort", "label"), [
    ("area_order", "地域順（東京から）"),
    ("name_asc", "店名順"),
    ("name_desc", "店名順（逆順）"),
    ("area_asc", "エリア名順"),
    ("area_desc", "エリア名順（逆順）"),
    ("rating_desc", "評価が高い順"),
    ("rating_asc", "評価が低い順"),
])
def test_sort_select_exposes_and_marks_the_current_order(
    db: Session, sort: str, label: str,
) -> None:
    add_public_shop(db, index=5, name="並び替え対象")
    db.commit()

    response = home(make_request("/"), sort=sort, db=db)
    html, parser = render_html(response)

    selected_options = [
        attrs
        for attrs in parser.find("option", attribute="selected")
        if attrs.get("value") in {
            "area_order",
            "name_asc",
            "name_desc",
            "area_asc",
            "area_desc",
            "rating_desc",
            "rating_asc",
        }
    ]
    assert [attrs["value"] for attrs in selected_options] == [sort]
    assert f'<option value="{sort}" selected>{label}</option>' in html
    if sort == "area_order":
        assert response.context["active_filters"] == []
    else:
        assert f"{label}の条件を解除" in html
    assert parser.find("a", attribute="class", value="sort-link") == []


def test_public_ui_omits_import_registration_dates(db: Session) -> None:
    shop = add_public_shop(db, index=51, name="日時非表示店")
    db.commit()

    recent_sort_response = home(
        make_request("/?sort=created_at_desc"),
        sort="created_at_desc",
        db=db,
    )
    assert recent_sort_response.status_code == 200
    assert recent_sort_response.context["selected_sort"] == "created_at_desc"

    list_response = home(make_request("/"), db=db)
    list_html, list_parser = render_html(list_response)
    detail_response = shop_detail(
        shop.id,
        make_request(f"/shop/{shop.id}"),
        db=db,
    )
    detail_html, _ = render_html(detail_response)

    assert list_response.context["selected_sort"] == "area_order"
    assert "created_at_desc" in {
        attrs.get("value")
        for attrs in list_parser.find("option", attribute="value")
    }
    assert "created_at_asc" not in list_html
    assert "最近保存した順" not in list_html
    assert "古く保存した順" not in list_html
    assert "登録日" not in list_html
    assert "保存日" not in detail_html


def test_filter_chip_returns_focus_to_query_input(db: Session) -> None:
    add_public_shop(db, index=6, name="絞り込み対象")
    db.commit()

    response = home(make_request("/"), q="絞り込み", db=db)
    _, parser = render_html(response)

    filter_chips = [
        attrs
        for attrs in parser.find("a", attribute="class")
        if class_contains(attrs, "filter-chip")
    ]
    assert len(filter_chips) == 1
    assert (filter_chips[0]["href"] or "").endswith("#q-input")


def test_keyword_search_matches_multiple_fields_and_all_terms(db: Session) -> None:
    target = add_public_shop(
        db,
        index=31,
        name="灯台",
        area="清澄白河",
        category="カフェ・喫茶店",
    )
    target.memo = "週末は予約推奨"
    add_public_shop(
        db,
        index=32,
        name="別の店",
        area="清澄白河",
        category="中華料理",
    )
    percent_shop = add_public_shop(
        db,
        index=33,
        name="100% COFFEE",
        area="銀座",
        category="カフェ・喫茶店",
    )
    db.commit()

    combined = home(
        make_request("/"),
        q="清澄白河 カフェ",
        sort="name_asc",
        db=db,
    )
    memo = home(make_request("/"), q="予約推奨", db=db)
    literal_percent = home(make_request("/"), q="%", db=db)

    assert [shop.id for shop in combined.context["shops"]] == [target.id]
    assert [shop.id for shop in memo.context["shops"]] == [target.id]
    assert [shop.id for shop in literal_percent.context["shops"]] == [
        percent_shop.id
    ]


def test_status_facets_apply_other_filters_and_preserve_list_state(
    db: Session,
) -> None:
    add_public_shop(
        db,
        index=41,
        name="銀座寿司 未訪問",
        area="銀座",
        category="寿司",
        visited=False,
    )
    add_public_shop(
        db,
        index=42,
        name="銀座寿司 訪問済み",
        area="銀座",
        category="寿司",
        visited=True,
    )
    add_public_shop(
        db,
        index=43,
        name="銀座カフェ",
        area="銀座",
        category="カフェ",
        visited=True,
    )
    db.commit()

    response = home(
        make_request("/"),
        q="銀座",
        area="銀座",
        category="寿司",
        status="visited",
        sort="name_asc",
        page=1,
        db=db,
    )

    facets = response.context["status_facets"]
    assert [(facet["label"], facet["count"], facet["selected"]) for facet in facets] == [
        ("すべて", 2, False),
        ("未訪問", 1, False),
        ("訪問済み", 1, True),
    ]
    for facet in facets:
        params = query_values(facet["url"])
        assert params["q"] == ["銀座"]
        assert params["area"] == ["銀座"]
        assert params["category"] == ["寿司"]
        assert params["sort"] == ["name_asc"]
        assert "page" not in params
    assert "status" not in query_values(facets[0]["url"])
    assert query_values(facets[1]["url"])["status"] == ["unvisited"]
    assert query_values(facets[2]["url"])["status"] == ["visited"]


def test_full_filter_reset_links_request_result_focus(db: Session) -> None:
    add_public_shop(db, index=7, name="解除対象")
    db.commit()

    filtered_response = home(make_request("/"), q="解除対象", db=db)
    _, filtered_parser = render_html(filtered_response)
    empty_response = home(make_request("/"), q="一致しない条件", db=db)
    _, empty_parser = render_html(empty_response)

    filtered_reset_links = [
        attrs
        for attrs in filtered_parser.find("a", attribute="data-result-navigation")
        if class_contains(attrs, "explorer-active-filters__clear")
    ]
    empty_reset_links = [
        attrs
        for attrs in empty_parser.find("a", attribute="data-result-navigation")
        if class_contains(attrs, "explorer-empty__reset")
    ]

    assert len(filtered_reset_links) == 1
    assert len(empty_reset_links) == 1
    assert filtered_reset_links[0]["href"] == "/?sort=area_order#q-input"
    assert empty_reset_links[0]["href"] == "/?sort=area_order"


def test_public_detail_is_semantic_read_only_html(db: Session) -> None:
    shop = add_public_shop(db, index=10, name="一般公開店")
    db.commit()

    response = shop_detail(
        shop.id,
        make_request(f"/shop/{shop.id}?return_to=%2F"),
        return_to="/",
        db=db,
    )
    _, parser = render_html(response)

    assert parser.find("article", attribute="data-shop-detail-content")
    assert parser.find("dl", attribute="class", value="place-detail__facts") == []
    form_actions = {attrs.get("action") for attrs in parser.find("form")}
    assert f"/shop/{shop.id}/edit" not in form_actions
    assert f"/shop/{shop.id}/delete" not in form_actions
    csrf_hidden = [
        attrs
        for attrs in parser.find("input", attribute="name", value="csrf_token")
        if attrs.get("type") == "hidden"
    ]
    assert csrf_hidden == []
    assert response.headers["Cache-Control"] == "private, no-store"


def test_public_detail_only_lists_supplemental_facts(db: Session) -> None:
    shop = add_public_shop(db, index=52, name="補足情報あり店", visited=True)
    shop.visited_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    shop.address = "東京都中央区銀座2-2-2"
    shop.phone = "03-1234-5678"
    db.commit()

    response = shop_detail(
        shop.id,
        make_request(f"/shop/{shop.id}"),
        db=db,
    )
    html, parser = render_html(response)

    assert parser.find("dl", attribute="class", value="place-detail__facts")
    assert "詳細情報" in html
    assert "訪問日" in html
    assert shop.address in html
    assert shop.phone in html
    assert "<dt>エリア</dt>" not in html
    assert "<dt>カテゴリ</dt>" not in html
    assert "<dt>訪問状況</dt>" not in html


def test_admin_detail_keeps_csrf_and_return_to_in_edit_and_delete_forms(
    db: Session,
) -> None:
    shop = add_public_shop(db, index=11, name="管理対象店")
    db.commit()
    return_to = "/?q=寿司+%26+酒&area=銀座&sort=name_asc&page=2"

    response = shop_detail(
        shop.id,
        make_request(f"/shop/{shop.id}", admin=True),
        return_to=return_to,
        db=db,
    )
    _, parser = render_html(response)

    forms = {
        attrs.get("action"): attrs
        for attrs in parser.find("form")
        if attrs.get("action") is not None
    }
    assert f"/shop/{shop.id}/edit" in forms
    assert f"/shop/{shop.id}/delete" in forms

    csrf_hidden = [
        attrs
        for attrs in parser.find("input", attribute="name", value="csrf_token")
        if attrs.get("type") == "hidden"
    ]
    assert len(csrf_hidden) == 2
    assert {attrs.get("value") for attrs in csrf_hidden} == {"test-token"}

    return_to_hidden = [
        attrs
        for attrs in parser.find("input", attribute="name", value="return_to")
        if attrs.get("type") == "hidden"
    ]
    assert len(return_to_hidden) == 2
    assert {attrs.get("value") for attrs in return_to_hidden} == {return_to}
    assert response.headers["Cache-Control"] == "private, no-store"


def test_list_navigation_links_preserve_unicode_filters_sort_and_page(
    db: Session,
) -> None:
    query = "寿司 & 酒"
    area = "銀座 & 有楽町"
    category = "和食 & 酒"
    for index in range(100, 151):
        add_public_shop(
            db,
            index=index,
            name=f"{query} {index}",
            area=area,
            category=category,
            visited=True,
        )
    db.commit()

    response = home(
        make_request("/"),
        q=query,
        area=area,
        category=category,
        status="visited",
        sort="name_asc",
        page=2,
        db=db,
    )
    _, parser = render_html(response)
    expected_state = {
        "q": [query],
        "area": [area],
        "category": [category],
        "status": ["visited"],
        "sort": ["name_asc"],
    }

    pager_links = [
        attrs
        for attrs in parser.find("a")
        if class_contains(attrs, "pagination__control")
        or class_contains(attrs, "pagination__page")
    ]
    assert pager_links
    for attrs in pager_links:
        href = attrs.get("href")
        if href is None:
            continue
        params = query_values(href)
        for key, expected_value in expected_state.items():
            assert params[key] == expected_value
        assert "page" in params

    csv_links = parser.find(
        "a",
        attribute="aria-label",
        value="現在の絞り込み結果をCSVでダウンロード",
    )
    assert csv_links == []

    detail_links = parser.find("a", attribute="data-shop-detail")
    assert len(detail_links) == response.context["per_page"]
    for detail_link in detail_links:
        detail_href = detail_link["href"]
        assert detail_href is not None
        detail_params = query_values(detail_href)
        assert set(detail_params) == {"return_to"}
        return_to = detail_params["return_to"][0]
        assert urlsplit(return_to).path == "/"
        assert query_values(return_to) == {**expected_state, "page": ["2"]}
