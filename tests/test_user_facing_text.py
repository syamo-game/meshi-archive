from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from db.models import Base, Message, Shop, ShopMention
from web.routers import home, review


PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
USER_FACING_SOURCES: tuple[Path, ...] = (
    PROJECT_ROOT / "bot" / "discord_bot.py",
    PROJECT_ROOT / "bot" / "sync_logic.py",
    PROJECT_ROOT / "web" / "read_only.py",
    PROJECT_ROOT / "web" / "templates" / "_place_detail_summary.html",
    PROJECT_ROOT / "web" / "templates" / "_shop_cards.html",
    PROJECT_ROOT / "web" / "templates" / "admin.html",
    PROJECT_ROOT / "web" / "templates" / "base.html",
    PROJECT_ROOT / "web" / "templates" / "explore.html",
    PROJECT_ROOT / "web" / "templates" / "review.html",
    PROJECT_ROOT / "web" / "templates" / "shop.html",
)


def test_user_facing_sources_use_data_review_label() -> None:
    text: str = "\n".join(path.read_text(encoding="utf-8") for path in USER_FACING_SOURCES)

    assert "データ確認" in text
    assert "要確認" not in text
    assert "精査" not in text


def test_read_only_message_describes_the_current_mode() -> None:
    text: str = (PROJECT_ROOT / "web" / "read_only.py").read_text(encoding="utf-8")

    assert "読取専用モード" in text
    assert "データ再構築中" not in text


@pytest.fixture
def slash_content() -> Generator[tuple[Session, ShopMention], None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            shop = Shop(
                shop_name="合成店／別名", area="神田", category="食事／喫茶",
                rating=4, memo="初回／再訪\n引用「窓側／奥の席」",
            )
            mention = ShopMention(
                message=Message(
                    message_id="90000000000000001", content="元投稿の引用「昼／夜に訪問」",
                ),
                shop=shop, occurrence_index=0, extracted_name=shop.shop_name,
                resolution_status="resolved", review_status="approved",
                metadata_review_status="approved", extraction_source="test",
                confidence_reason="本文引用「昼／夜に訪問」",
            )
            db.add(mention)
            db.commit()
            yield db, mention
    finally:
        engine.dispose()


def _request(path: str, *, admin: bool = False) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": path, "query_string": b"",
        "headers": [], "scheme": "http", "server": ("testserver", 80),
        "session": {
            "authenticated": True, "admin_authenticated": admin, "csrf_token": "test-token",
        },
    })


@pytest.mark.parametrize("template_name", [
    "_shop_cards.html", "_place_detail_summary.html",
])
def test_shop_fragments_use_ascii_separators_without_rewriting_content(
    slash_content: tuple[Session, ShopMention], template_name: str,
) -> None:
    _db, mention = slash_content
    shop = mention.shop
    rendered = home.templates.get_template(template_name).render(
        shops=[shop], shop=shop, shop_links=home._shop_links_by_id([shop], None),
        is_admin=False, saved=False, current_list_url="/", return_to="/",
    )

    assert 'aria-label="評価 4点/5点"' in rendered
    assert "4点／5点" not in rendered
    assert "合成店／別名" in rendered
    assert "食事／喫茶" in rendered
    assert '<span aria-hidden="true">/</span><span>食事／喫茶</span>' in rendered
    if template_name == "_place_detail_summary.html":
        assert "初回／再訪\n引用「窓側／奥の席」" in rendered


def test_public_pages_and_review_quote_preserve_stored_full_width_slashes(
    slash_content: tuple[Session, ShopMention],
) -> None:
    db, mention = slash_content
    shop = mention.shop
    list_response = home.home(_request("/"), db=db)
    detail_response = home.shop_detail(shop.id, _request(f"/shop/{shop.id}"), db=db)
    review_item = review.review_item(
        mention.id, _request(f"/api/admin/reviews/{mention.id}", admin=True), Response(), db=db,
    )

    for response in (list_response, detail_response):
        assert response.status_code == 200
        rendered = bytes(response.body).decode("utf-8")
        assert "合成店／別名" in rendered
        assert '<span aria-hidden="true">/</span><span>食事／喫茶</span>' in rendered
        assert 'aria-label="評価 4点/5点"' in rendered
    assert "初回／再訪\n引用「窓側／奥の席」" in bytes(detail_response.body).decode("utf-8")
    assert review_item.message_content == "元投稿の引用「昼／夜に訪問」"
    assert review_item.confidence_reason == "本文引用「昼／夜に訪問」"
    assert review_item.extracted_name == "合成店／別名"
    assert review_item.shop is not None
    assert review_item.shop.memo == "初回／再訪\n引用「窓側／奥の席」"
    db.expire_all()
    assert mention.shop.shop_name == "合成店／別名"
    assert mention.shop.category == "食事／喫茶"
    assert mention.shop.memo == "初回／再訪\n引用「窓側／奥の席」"
    assert mention.message.content == "元投稿の引用「昼／夜に訪問」"


def test_conflict_separator_preserves_slashes_in_input_and_latest_values(
    slash_content: tuple[Session, ShopMention],
) -> None:
    db, mention = slash_content
    shop = mention.shop
    edit_values = replace(
        home._shop_edit_values(shop), shop_name="入力／店名",
        memo="入力／メモ 引用「朝／夕」", expected_version="0",
    )
    response = home._render_shop_page(
        _request(f"/shop/{shop.id}", admin=True), shop, db, "/",
        edit_values=edit_values, status_code=409,
    )
    rendered = bytes(response.body).decode("utf-8")

    assert response.status_code == 409
    assert "店名：入力「入力／店名」/最新「合成店／別名」" in rendered
    assert "メモ：入力「入力／メモ 引用「朝／夕」」/最新「初回／再訪\n引用「窓側／奥の席」」" in rendered
    assert "」／最新「" not in rendered
    db.refresh(shop)
    assert shop.shop_name == "合成店／別名"
    assert shop.memo == "初回／再訪\n引用「窓側／奥の席」"
