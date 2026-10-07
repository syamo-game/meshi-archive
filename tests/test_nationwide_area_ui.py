from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from html.parser import HTMLParser

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from db.models import Base, Message, Shop, ShopMention
from web.area_groups import canonicalize_area
from web.routers.home import home, shop_detail, shop_edit


@dataclass(frozen=True)
class AreaOption:
    value: str
    selected: bool


class AreaSelectParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_area_select: bool = False
        self.options: list[AreaOption] = []
        self.groups: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "select":
            self.in_area_select = attributes.get("name") == "area"
        if not self.in_area_select:
            return
        if tag == "optgroup":
            self.groups.append(attributes.get("label") or "")
        if tag == "option":
            self.options.append(AreaOption(
                value=attributes.get("value") or "",
                selected="selected" in attributes,
            ))

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.in_area_select = False


class LocationTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_location: bool = False
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p":
            classes = (dict(attrs).get("class") or "").split()
            self.in_location = bool({"place-detail__location", "place-card__meta"} & set(classes))

    def handle_endtag(self, tag: str) -> None:
        if tag == "p":
            self.in_location = False

    def handle_data(self, data: str) -> None:
        if self.in_location and data.strip():
            self.text.append(data.strip())


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Generator[Session, None, None]:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_request(path: str, *, admin: bool = False) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "session": {
            "authenticated": True,
            "admin_authenticated": admin,
            "csrf_token": "area-test-token",
        },
    })


def add_shop(db: Session, area: str | None = "銀座") -> Shop:
    shop = Shop(shop_name="地域を編集する店", area=area)
    mention = ShopMention(
        message=Message(message_id="12345678901234567"),
        shop=shop,
        occurrence_index=0,
        extracted_name=shop.shop_name,
        extracted_area=area,
        resolution_status="resolved",
        review_status="approved",
        metadata_review_status="approved",
        extraction_source="test",
    )
    db.add(mention)
    db.commit()
    return shop


def edit_area(db: Session, shop: Shop, area: str | None) -> Response:
    return shop_edit(
        expected_version=str(shop.version),
        shop_id=shop.id,
        request=make_request(f"/shop/{shop.id}/edit", admin=True),
        shop_name=shop.shop_name,
        area=area,
        category=None,
        url=None,
        address=None,
        phone=None,
        memo="エリア以外の変更",
        rating=None,
        is_visited=None,
        visited_at=None,
        csrf_token="area-test-token",
        db=db,
    )


def parse_areas(response: Response) -> AreaSelectParser:
    parser = AreaSelectParser()
    parser.feed(bytes(response.body).decode("utf-8"))
    return parser


@pytest.mark.parametrize("area", [
    "長野県小布施町",
    "東京都檜原村",
    "沖縄県与那国町",
    "神奈川県横浜市中区",
])
def test_editor_offers_and_saves_municipalities_without_shops(
    db: Session,
    area: str,
) -> None:
    shop = add_shop(db)
    canonical = canonicalize_area(area)
    assert canonical is not None

    response = shop_detail(shop.id, make_request(f"/shop/{shop.id}", admin=True), db=db)
    parser = parse_areas(response)
    assert canonical in {option.value for option in parser.options}
    assert len({group.split(" / ")[0] for group in parser.groups}) == 47
    assert [option.value for option in parser.options if option.selected] == ["銀座"]

    saved = edit_area(db, shop, canonical)
    assert saved.status_code == 302
    db.refresh(shop)
    assert shop.area == canonical


@pytest.mark.parametrize("area", ["銀座", "陸前高田市", '旧エリア <外部> & "記録"'])
def test_editor_preserves_current_legacy_or_unrecognized_area(
    db: Session,
    area: str,
) -> None:
    shop = add_shop(db, area)
    response = shop_detail(shop.id, make_request(f"/shop/{shop.id}", admin=True), db=db)
    parser = parse_areas(response)
    assert [option.value for option in parser.options if option.selected] == [area]

    saved = edit_area(db, shop, area)
    assert saved.status_code == 302
    db.refresh(shop)
    assert shop.area == area
    assert shop.memo == "エリア以外の変更"


def test_editor_rejects_new_unrecognized_area(db: Session) -> None:
    shop = add_shop(db, "元の未登録エリア")

    response = edit_area(db, shop, "別の未登録エリア")
    assert response.status_code == 400
    assert "候補にある市区町村・エリアを選んでください。" in bytes(response.body).decode("utf-8")
    db.refresh(shop)
    assert shop.area == "元の未登録エリア"


def test_public_search_only_offers_areas_with_public_shops(db: Session) -> None:
    add_shop(db)
    response = home(make_request("/"), db=db)
    parser = parse_areas(response)
    values = {option.value for option in parser.options}

    assert "銀座" in values
    assert canonicalize_area("長野県小布施町") not in values
    assert "東京都中央区" in values
    assert parser.groups == []


@pytest.mark.parametrize(("area", "parent", "label"), [
    ("長野県小布施町", "長野県", "上高井郡小布施町"),
    ("東京都台東区 / 鶯谷", "台東区", "鶯谷"),
    ("東京都江東区 / 木場", "江東区", "木場"),
    ("東京都江東区 / 新木場", "江東区", "新木場"),
    ("東京都府中市", "東京都", "府中市"),
    ("広島県府中市", "広島県", "府中市"),
    ("銀座", "中央区", "銀座"),
])
def test_public_location_keeps_prefecture_context_without_repeating_names(
    db: Session,
    area: str,
    parent: str,
    label: str,
) -> None:
    canonical = canonicalize_area(area)
    assert canonical is not None
    shop = add_shop(db, canonical)

    for response in (
        home(make_request("/"), db=db),
        shop_detail(shop.id, make_request(f"/shop/{shop.id}"), db=db),
    ):
        parser = LocationTextParser()
        parser.feed(bytes(response.body).decode("utf-8"))
        assert parser.text == [parent, "・", label]
