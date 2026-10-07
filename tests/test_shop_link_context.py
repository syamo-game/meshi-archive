from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import StreamingResponse

from db.models import Base, Message, Shop, ShopMention, SourceAsset
from services.import_service import parse_csv_bytes
from services.shop_image_cache import processed_image_path
from web.routers import home as home_router


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


def add_mention(
    db: Session,
    shop: Shop,
    *,
    message_id: str,
    source_url: str | None,
    review_status: str = "approved",
    metadata_review_status: str = "approved",
) -> ShopMention:
    mention = ShopMention(
        message=Message(message_id=message_id),
        shop=shop,
        occurrence_index=len(shop.mentions),
        extracted_name=shop.shop_name,
        source_url=source_url,
        resolution_status="resolved",
        review_status=review_status,
        metadata_review_status=metadata_review_status,
        extraction_source="test",
    )
    db.add(mention)
    return mention


def test_shop_links_keep_canonical_and_fully_approved_source_separate(db: Session) -> None:
    shop = Shop(
        shop_name="鮨みなと",
        branch_name="銀座店",
        area="銀座",
        address="東京都中央区銀座1-2-3",
        canonical_url="https://restaurant.example/ginza",
    )
    add_mention(
        db,
        shop,
        message_id="90000000000000001",
        source_url="https://private.example/post",
        metadata_review_status="pending",
    )
    add_mention(
        db,
        shop,
        message_id="90000000000000002",
        source_url="https://social.example/post/2",
    )
    db.commit()

    links = home_router._shop_links(
        shop,
        "https://discord.com/channels/guild/channel",
    )

    assert links["source_url"] == "https://social.example/post/2"
    assert links["canonical_url"] == "https://restaurant.example/ginza"
    assert links["discord_url"] == (
        "https://discord.com/channels/guild/channel/90000000000000002"
    )
    assert parse_qs(urlparse(links["maps_url"]).query) == {
        "api": ["1"],
        "query": ["鮨みなと 銀座店 東京都中央区銀座1-2-3"],
    }


def test_shop_links_do_not_choose_unreviewed_images_even_when_cached(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    shop = Shop(shop_name="画像のある店")
    mention = add_mention(
        db,
        shop,
        message_id="90000000000000031",
        source_url="https://x.com/example/status/31",
    )
    mention.message.assets.extend(
        [
            SourceAsset(
                kind="image",
                url="https://media.discordapp.net/external/first.jpg",
                fetch_status="available",
            ),
            SourceAsset(
                kind="image",
                url="https://media.discordapp.net/external/second.jpg",
                fetch_status="available",
            ),
        ]
    )
    db.commit()
    first_url = "https://media.discordapp.net/external/first.jpg"
    cached_path = processed_image_path(first_url)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(b"processed image")

    links = home_router._shop_links(shop, None)

    assert links["image_url"] is None
    assert len(mention.message.assets) == 2
    assert all(asset.fetch_status == "available" for asset in mention.message.assets)


def test_shop_links_omit_unavailable_or_unapproved_images(db: Session) -> None:
    shop = Shop(shop_name="非表示画像の店")
    approved = add_mention(
        db,
        shop,
        message_id="90000000000000032",
        source_url="https://x.com/example/status/32",
    )
    approved.message.assets.append(
        SourceAsset(
            kind="image",
            url="https://media.discordapp.net/external/unavailable.jpg",
            fetch_status="unavailable",
        )
    )
    pending = add_mention(
        db,
        shop,
        message_id="90000000000000033",
        source_url="https://x.com/example/status/33",
        review_status="pending",
    )
    pending.message.assets.append(
        SourceAsset(
            kind="image",
            url="https://media.discordapp.net/external/pending.jpg",
            fetch_status="available",
        )
    )
    db.commit()

    links = home_router._shop_links(shop, None)

    assert links["image_url"] is None


@pytest.mark.parametrize(
    ("address", "area", "expected_query"),
    [
        (None, "浅草橋", "珈琲アーカイブ 東口店 浅草橋"),
        (None, None, "珈琲アーカイブ 東口店"),
    ],
)
def test_google_maps_query_uses_area_then_shop_name_fallback(
    address: str | None,
    area: str | None,
    expected_query: str,
) -> None:
    shop = Shop(
        shop_name="珈琲アーカイブ",
        branch_name="東口店",
        address=address,
        area=area,
    )

    query = parse_qs(urlparse(home_router._google_maps_url(shop)).query)

    assert query == {"api": ["1"], "query": [expected_query]}


@pytest.mark.parametrize(
    "source_url",
    [
        "javascript:alert(1)",
        "ftp://source.example/post",
        "https://user:secret@source.example/post",
        "https://source.example:invalid/post",
        "https://source.example/post\nheader: value",
        "https://source.example/post\x00suffix",
        "https://source.example/post\x01suffix",
        "https://source.example/post\x7fsuffix",
        "https://exa mple.com/post",
        "https://example.com\\post",
        "https://./post",
    ],
)
def test_shop_links_safely_reject_invalid_public_source_urls(
    db: Session,
    source_url: str,
) -> None:
    shop = Shop(shop_name="不正URLテスト")
    add_mention(
        db,
        shop,
        message_id="90000000000000003",
        source_url=source_url,
    )
    db.commit()

    links = home_router._shop_links(shop, None)

    assert links["source_url"] is None


def test_shop_links_classify_map_sources_and_deduplicate_equivalent_urls(
    db: Session,
) -> None:
    map_shop = Shop(shop_name="地図出典店")
    add_mention(
        db,
        map_shop,
        message_id="90000000000000021",
        source_url="https://maps.app.goo.gl/example-map",
    )
    duplicate_shop = Shop(
        shop_name="重複URL店",
        canonical_url=(
            "https://EXAMPLE.com:443/shop/?utm_source=archive&gclid=tracking"
        ),
    )
    add_mention(
        db,
        duplicate_shop,
        message_id="90000000000000022",
        source_url="https://example.com/shop",
    )
    db.commit()

    map_links = home_router._shop_links(map_shop, None)
    duplicate_links = home_router._shop_links(duplicate_shop, None)

    assert map_links["source_is_map"] is True
    assert duplicate_links["source_is_map"] is False
    assert duplicate_links["canonical_url"] is None


@pytest.mark.parametrize(
    ("source_url", "expected_label"),
    (
        ("https://x.com/example/status/123", "X"),
        ("https://twitter.com/example/status/123?ref=x", "X"),
        ("https://x.com/i/web/status/123", "X"),
        ("https://www.twitter.com/i/web/status/123/photo/1", "X"),
        ("https://mobile.x.com/example/status/123", "X"),
        ("https://mobile.twitter.com/i/web/status/123?ref=x", "X"),
        ("https://x.com/example", "X"),
        ("https://x.com/example/status/not-a-post", "X"),
        ("https://x.com/i/web/status/not-a-post", "X"),
        ("https://mobile.x.com/example", "mobile.x.com"),
        ("https://mobile.twitter.com/i/web/status/not-a-post", "mobile.twitter.com"),
        ("https://not-x.com/example/status/123", "not-x.com"),
        ("https://tabelog.com/tokyo/example", "食べログ"),
        ("https://s.tabelog.com/tokyo/example", "食べログ"),
        ("https://dancyu.jp/read/example.html", "dancyu"),
        ("https://www.youtube.com/watch?v=example", "YouTube"),
        ("https://source.example/article", "source.example"),
        ("https://www.matsuo-toyama.com/", "matsuo-toyama.com"),
        ("https://www.instagram.com/p/example/", "instagram.com"),
        ("https://source.example/menu.PDF?download=1", "PDF"),
        ("https://not-tabelog.com/article", "not-tabelog.com"),
    ),
)
def test_shop_links_label_the_external_destination(
    db: Session,
    source_url: str,
    expected_label: str,
) -> None:
    shop = Shop(shop_name="リンク表示店")
    add_mention(
        db,
        shop,
        message_id="90000000000000023",
        source_url=source_url,
    )
    db.commit()

    links = home_router._shop_links(shop, None)

    assert links["source_label"] == expected_label
    assert links["source_url"] == source_url


@pytest.mark.parametrize(
    "maps_url",
    (
        "https://maps.google.co.jp/maps/place/example",
        "https://google.co.jp/maps/place/example",
        "https://www.google.co.jp/maps/place/example",
    ),
)
def test_google_maps_japan_urls_are_classified_as_maps(maps_url: str) -> None:
    assert home_router._is_google_maps_url(maps_url) is True


def test_source_url_is_hidden_without_both_approvals(db: Session) -> None:
    shop = Shop(shop_name="審査中の店")
    add_mention(
        db,
        shop,
        message_id="90000000000000004",
        source_url="https://social.example/pending",
        review_status="pending",
        metadata_review_status="approved",
    )
    db.commit()

    assert home_router._shop_links(shop, None)["source_url"] is None


def test_shop_links_use_first_valid_approved_source_in_id_order(db: Session) -> None:
    shop = Shop(shop_name="複数投稿の店")
    first = add_mention(
        db,
        shop,
        message_id="90000000000000011",
        source_url=None,
    )
    add_mention(
        db,
        shop,
        message_id="90000000000000012",
        source_url="javascript:alert(1)",
    )
    expected = add_mention(
        db,
        shop,
        message_id="90000000000000013",
        source_url="https://social.example/first-valid",
    )
    add_mention(
        db,
        shop,
        message_id="90000000000000014",
        source_url="https://social.example/later-valid",
    )
    db.commit()
    shop.mentions.reverse()

    links = home_router._shop_links(
        shop,
        "https://discord.com/channels/guild/channel",
    )

    assert first.id is not None
    assert expected.id is not None
    assert links["source_url"] == "https://social.example/first-valid"
    assert links["discord_url"] == (
        "https://discord.com/channels/guild/channel/90000000000000011"
    )


def test_home_and_detail_receive_shop_links(db: Session) -> None:
    shop = Shop(shop_name="文脈テスト", area="銀座")
    add_mention(
        db,
        shop,
        message_id="90000000000000005",
        source_url="https://social.example/context",
    )
    db.commit()

    home_response = home_router.home(request(), db=db)
    detail_response = home_router.shop_detail(shop.id, request(f"/shop/{shop.id}"), db=db)

    assert home_response.context["shop_links"][shop.id]["source_url"] == (
        "https://social.example/context"
    )
    assert detail_response.context["shop_links"][shop.id]["source_url"] == (
        "https://social.example/context"
    )


def test_incremental_api_passes_shop_links_to_row_template(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = Shop(shop_name="1ページ目")
    add_mention(
        db,
        first,
        message_id="90000000000000006",
        source_url="https://social.example/first",
    )
    second = Shop(shop_name="2ページ目")
    add_mention(
        db,
        second,
        message_id="90000000000000007",
        source_url="https://social.example/second",
    )
    db.commit()
    captured_context: dict[str, object] = {}

    class CapturingTemplate:
        def render(self, **context: object) -> str:
            captured_context.update(context)
            return ""

    monkeypatch.setattr(home_router, "PER_PAGE", 1)
    monkeypatch.setattr(
        home_router.templates.env,
        "get_template",
        lambda _name: CapturingTemplate(),
    )

    home_router.api_shops(request("/api/shops"), sort="name_asc", page=2, db=db)

    shop_links = captured_context["shop_links"]
    assert isinstance(shop_links, dict)
    assert shop_links[second.id]["source_url"] == "https://social.example/second"


async def _csv_bytes(response: StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


def test_merged_legacy_duplicate_keeps_exact_primary_source_and_csv(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url = "https://x.com/example/status/1990014704857870468?s=20"
    created_at = datetime(2026, 3, 12, 17, 52, 11, tzinfo=timezone.utc)
    old_shop = Shop(id=209, shop_name="司", area="門前仲町")
    shop = Shop(
        id=565,
        shop_name="天然本マグロ専門店 司",
        area="東京都江東区 / 木場",
        category="海鮮・刺身",
        created_at=created_at,
    )
    legacy = add_mention(
        db, old_shop, message_id="1439906584594610000", source_url=source_url
    )
    legacy.extraction_source = "legacy_import"
    db.flush()
    unrelated = add_mention(
        db, shop, message_id="1439906584594611111",
        source_url="https://x.com/example/status/1990014704857870469",
    )
    db.flush()
    reparsed = add_mention(
        db, shop, message_id="1439906584594612224", source_url=source_url
    )
    reparsed.extraction_source = "responses_structured"
    db.flush()
    legacy.shop = shop
    db.flush()
    db.delete(old_shop)
    db.commit()
    shop.mentions.reverse()

    assert shop.primary_mention() is reparsed
    assert shop.message_id == "1439906584594612224"
    assert home_router._public_mentions(shop) == [reparsed, legacy, unrelated]
    assert all(mention.review_status == "approved" for mention in shop.mentions)
    assert legacy.message_id == "1439906584594610000"
    assert legacy.extraction_source == "legacy_import"

    monkeypatch.setattr(home_router, "DISCORD_GUILD_ID", "guild")
    monkeypatch.setattr(home_router, "DISCORD_CHANNEL_ID", "channel")
    response = home_router.shop_detail(shop.id, request(f"/shop/{shop.id}"), db=db)
    html = bytes(response.body).decode("utf-8")
    assert 'href="https://discord.com/channels/guild/channel/1439906584594612224"' in html
    assert "1439906584594610000" not in html

    export_request = request("/export.csv")
    export_request.session["admin_authenticated"] = True
    exported = home_router.export_csv(export_request, db=db)
    assert isinstance(exported, StreamingResponse)
    rows = parse_csv_bytes(asyncio.run(_csv_bytes(exported)), "merged.csv").rows
    assert len(rows) == 1
    assert rows[0].message_id == "1439906584594612224"
    assert rows[0].source_url == source_url
    assert rows[0].extraction_source == "responses_structured"
    assert rows[0].created_at == created_at


@pytest.mark.parametrize(
    ("identity_status", "metadata_status"),
    [("pending", "approved"), ("rejected", "approved"), ("approved", "pending")],
)
def test_nonpublic_reparsed_duplicate_cannot_replace_public_source(
    db: Session,
    identity_status: str,
    metadata_status: str,
) -> None:
    shop = Shop(shop_name="公開範囲を維持する店")
    source_url = "https://x.com/example/status/1990014704857870468"
    legacy = add_mention(
        db, shop, message_id="1439906584594610000", source_url=source_url
    )
    legacy.extraction_source = "legacy_import"
    db.flush()
    add_mention(
        db, shop, message_id="1439906584594612224", source_url=source_url,
        review_status=identity_status, metadata_review_status=metadata_status,
    )
    db.commit()

    assert home_router._public_mentions(shop) == [legacy]
    assert home_router._shop_links(shop, "https://discord.com/channels/guild/channel")[
        "discord_url"
    ] == "https://discord.com/channels/guild/channel/1439906584594610000"
    if identity_status != "approved":
        assert shop.primary_mention() is legacy


@pytest.mark.parametrize(
    ("first_source", "later_source", "later_extraction"),
    [
        ("https://x.com/example/status/1", "https://x.com/example/status/1", "legacy_import"),
        ("https://x.com/example/status/1", "https://x.com/example/status/2", "responses_structured"),
        (None, None, "responses_structured"),
    ],
)
def test_legacy_only_or_different_sources_keep_original_primary(
    db: Session,
    first_source: str | None,
    later_source: str | None,
    later_extraction: str,
) -> None:
    shop = Shop(shop_name="元の代表投稿を維持する店")
    first = add_mention(
        db, shop, message_id="1439906584594610000", source_url=first_source
    )
    first.extraction_source = "legacy_import"
    db.flush()
    later = add_mention(
        db, shop, message_id="1439906584594612224", source_url=later_source
    )
    later.extraction_source = later_extraction
    db.commit()
    shop.mentions.reverse()

    assert shop.primary_mention() is first
    assert home_router._public_mentions(shop) == [first, later]
