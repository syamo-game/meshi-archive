from __future__ import annotations

from collections.abc import Generator
from html.parser import HTMLParser

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from db.models import Base, Message, Shop, ShopMention
from web.routers import home


class FormFields(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self.invalid: dict[str, str] = {}
        self.select_name: str = ""
        self.textarea_name: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        name = attributes.get("name") or ""
        if tag == "input" and name:
            if attributes.get("type") != "checkbox" or "checked" in attributes:
                self.fields[name] = attributes.get("value") or ""
        elif tag == "select":
            self.select_name = name
        elif tag == "option" and "selected" in attributes:
            self.fields[self.select_name] = attributes.get("value") or ""
        elif tag == "textarea":
            self.textarea_name = name
        if attributes.get("aria-invalid") == "true":
            self.invalid[name] = attributes.get("aria-describedby") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.select_name = ""
        elif tag == "textarea":
            self.textarea_name = ""

    def handle_data(self, data: str) -> None:
        if self.textarea_name:
            self.fields[self.textarea_name] = self.fields.get(self.textarea_name, "") + data


@pytest.fixture
def editor(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, sessionmaker[Session], int], None, None]:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        shop = Shop(shop_name="元の店名", area="銀座", category="寿司")
        message = Message(message_id="90000000000000001")
        mention = ShopMention(
            shop=shop, message=message, occurrence_index=0, extracted_name="元の店名",
            resolution_status="resolved", review_status="approved",
            metadata_review_status="approved", extraction_source="test",
        )
        db.add(mention)
        db.commit()
        shop_id = shop.id

    def get_db() -> Generator[Session, None, None]:
        with sessions() as db:
            yield db

    app = FastAPI()

    @app.middleware("http")
    async def add_session(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.scope["session"] = {
            "authenticated": True, "admin_authenticated": True, "csrf_token": "edit-token",
        }
        return await call_next(request)

    app.include_router(home.router)
    app.dependency_overrides[home.get_db] = get_db
    try:
        with TestClient(app) as client:
            yield client, sessions, shop_id
    finally:
        engine.dispose()


@pytest.mark.parametrize(("field", "value", "message"), [
    ("shop_name", "", "店名を入力してください。"),
    ("shop_name", "   ", "店名を入力してください。"),
    ("area", "存在しない新エリア", "候補にある市区町村・エリアを選んでください。"),
    ("url", "ftp://example.com", "有効なURLを入力してください。"),
    ("url", "https://name:secret@example.com", "有効なURLを入力してください。"),
    ("rating", "6", "評価は1〜5から選んでください。"),
    ("rating", "²", "評価は1〜5から選んでください。"),
    ("visited_at", "2026-02-30", "実在する日付をYYYY-MM-DD形式で入力してください。"),
])
def test_invalid_edit_returns_html_with_all_input_and_field_error(
    editor: tuple[TestClient, sessionmaker[Session], int], field: str, value: str, message: str,
) -> None:
    client, sessions, shop_id = editor
    payload = {
        "shop_name": "入力した店名 <保存前>", "area": "銀座", "category": "カフェ",
        "url": "https://example.com/edited", "address": "入力した住所", "phone": "03-1234-5678",
        "memo": "メモの1行目\n2行目 <保持>", "rating": "4", "is_visited": "on",
        "visited_at": "2026-09-01", "csrf_token": "edit-token", "return_to": "/?q=cafe",
        "expected_version": "1",
    }
    payload[field] = value
    response = client.post(f"/shop/{shop_id}/edit", data=payload, follow_redirects=False)

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "private, no-store"
    assert message in response.text
    parser = FormFields()
    parser.feed(response.text)
    for key, entered in payload.items():
        assert parser.fields[key] == entered, key
    assert field in parser.invalid
    assert "-error" in parser.invalid[field]
    assert "<保存前>" not in response.text
    with sessions() as db:
        shop = db.get(Shop, shop_id)
        assert shop is not None
        assert shop.shop_name == "元の店名"
        assert shop.area == "銀座"
        assert shop.memo is None
        assert shop.version == 1
        assert shop.mentions[0].metadata_review_status == "approved"


def test_all_fields_are_validated_before_modifying_shop_or_mentions(
    editor: tuple[TestClient, sessionmaker[Session], int],
) -> None:
    client, sessions, shop_id = editor
    response = client.post(f"/shop/{shop_id}/edit", data={
        "shop_name": "", "area": "", "url": "javascript:alert(1)", "rating": "6",
        "is_visited": "on", "visited_at": "invalid", "csrf_token": "edit-token",
        "expected_version": "1",
    })
    parser = FormFields()
    parser.feed(response.text)
    assert response.status_code == 400
    assert set(parser.invalid) == {"shop_name", "url", "rating", "visited_at"}
    with sessions() as db:
        shop = db.get(Shop, shop_id)
        assert shop is not None
        assert shop.area == "銀座"
        assert shop.mentions[0].metadata_review_status == "approved"
        assert shop.mentions[0].version == 1


def test_corrected_edit_saves_the_retained_fields(
    editor: tuple[TestClient, sessionmaker[Session], int],
) -> None:
    client, sessions, shop_id = editor
    payload = {
        "shop_name": "修正した店名", "area": "銀座", "url": "ftp://example.com",
        "memo": "保持するメモ", "rating": "5", "csrf_token": "edit-token",
    }
    payload["expected_version"] = "1"
    failed = client.post(f"/shop/{shop_id}/edit", data=payload)
    assert failed.status_code == 400
    parser = FormFields()
    parser.feed(failed.text)
    parser.fields["url"] = "https://example.com"
    saved = client.post(f"/shop/{shop_id}/edit", data=parser.fields, follow_redirects=False)
    assert saved.status_code == 302
    with sessions() as db:
        shop = db.get(Shop, shop_id)
        assert shop is not None
        assert shop.shop_name == "修正した店名"
        assert shop.memo == "保持するメモ"
        assert shop.canonical_url == "https://example.com"
        assert shop.rating == 5
