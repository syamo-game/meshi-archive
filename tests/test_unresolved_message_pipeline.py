from __future__ import annotations

import asyncio
import html
from collections.abc import AsyncIterator, Callable, Generator
from datetime import datetime, timezone
from typing import cast

import discord
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from bot.restaurant_extractor import (
    CandidateSearchResult,
    ExtractedMention,
    ExtractedMessage,
    ExtractionCallResult,
    ExtractionError,
    FetchedHtmlDocument,
    ImageAnalysisResult,
    ImageClues,
    ModelCallMetrics,
    SearchCandidate,
    SearchCandidateSet,
)
from bot import sync_logic
from db.models import (
    Base,
    CandidateProvenance,
    LookupCache,
    Message,
    MetadataReviewStatus,
    ProcessingRun,
    ResolutionCandidate,
    Shop,
    ShopMention,
    SourceAsset,
    SyncState,
)
from services import identification_pipeline
from services.identification_pipeline import (
    CandidateBatch,
    MessageEnvelope,
    PipelineCandidate,
    SourceAssetInput,
)
from services.resolution import CandidateIdentity, name_similarity
from services.review_service import (
    EditAndApproveDecision,
    EditableShop,
    apply_review_decision,
)


MESSAGE_ID = "1436975355658244097"
NEXT_MESSAGE_ID = "1436975355658244098"
CHANNEL_ID = "1432348507410534512"
SOURCE_URL = "https://x.com/aptasakusabashi/status/1924813456236278183?s=46"
IMAGE_URL = "https://cdn.discordapp.com/attachments/1/2/evidence.jpg"
SECOND_IMAGE_URL = "https://cdn.discordapp.com/attachments/1/2/menu.jpg"
LEGACY_MESSAGE_ID = "12345678901234567"


@pytest.fixture
def db_factory() -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def metrics(*, web_search_calls: int = 0) -> ModelCallMetrics:
    return ModelCallMetrics(
        model="test-model",
        input_tokens=10,
        output_tokens=5,
        web_search_calls=web_search_calls,
        image_count=0,
        latency_ms=1,
        estimated_cost_microusd=1,
    )


def unresolved_extraction() -> ExtractionCallResult:
    return ExtractionCallResult(
        message=ExtractedMessage(
            is_restaurant_message=True,
            ignore_reason=None,
            unresolved_reason="投稿に店名がありません。",
            mentions=[],
        ),
        metrics=metrics(),
    )


def ignored_extraction() -> ExtractionCallResult:
    return ExtractionCallResult(
        message=ExtractedMessage(
            is_restaurant_message=False,
            ignore_reason="飲食店への言及ではありません。",
            unresolved_reason=None,
            mentions=[],
        ),
        metrics=metrics(),
    )


def source_discovery_extraction(
    *,
    shop_name: str = "abcdefghij",
    area: str = "銀座",
) -> ExtractionCallResult:
    return ExtractionCallResult(
        message=ExtractedMessage(
            is_restaurant_message=True,
            ignore_reason=None,
            unresolved_reason=None,
            mentions=[
                ExtractedMention(
                    shop_name=shop_name,
                    branch_name=None,
                    area=area,
                    category="寿司・回転寿司",
                    source_url=SOURCE_URL,
                    needs_review=False,
                    confidence_reason="出典URLに店名と所在地がある",
                )
            ],
        ),
        metrics=metrics(web_search_calls=1),
        source_urls=(SOURCE_URL,),
    )


def unknown_category_extraction() -> ExtractionCallResult:
    return ExtractionCallResult(
        message=ExtractedMessage(
            is_restaurant_message=True,
            ignore_reason=None,
            unresolved_reason=None,
            mentions=[
                ExtractedMention(
                    shop_name="PACO（パコ）",
                    branch_name=None,
                    area="学芸大学",
                    category="メキシコ料理",
                    needs_review=False,
                    confidence_reason="投稿本文に店名と料理の記載がある",
                )
            ],
        ),
        metrics=metrics(),
    )


def branch_extraction() -> ExtractionCallResult:
    return ExtractionCallResult(
        message=ExtractedMessage(
            is_restaurant_message=True,
            ignore_reason=None,
            unresolved_reason=None,
            mentions=[
                ExtractedMention(
                    shop_name="銀座 鮨はな",
                    branch_name="本店",
                    area="銀座",
                    category="メキシコ料理",
                    needs_review=False,
                    confidence_reason="投稿本文に店名、支店名、エリアの記載がある",
                )
            ],
        ),
        metrics=metrics(),
    )


def known_category_extraction() -> ExtractionCallResult:
    return ExtractionCallResult(
        message=ExtractedMessage(
            is_restaurant_message=True,
            ignore_reason=None,
            unresolved_reason=None,
            mentions=[
                ExtractedMention(
                    shop_name="浅草橋 焼肉はな",
                    branch_name=None,
                    area="浅草橋",
                    category="焼肉",
                    needs_review=False,
                    confidence_reason="投稿本文に店名とエリアの記載がある",
                )
            ],
        ),
        metrics=metrics(),
    )


@pytest.mark.parametrize(
    "area",
    [
        "未知の街",
        "東京",
        "中央区",
        "東京国際フォーラム",
        "門前仲町/木場",
    ],
)
def test_wrong_granularity_area_requires_metadata_review(area: str) -> None:
    status, difference_type = identification_pipeline._metadata_review_state(
        area,
        "焼肉",
    )

    assert status == MetadataReviewStatus.PENDING
    assert difference_type == "unknown_area"


def test_new_shop_prefers_canonical_mention_area_to_candidate_area() -> None:
    mention = ExtractedMention(
        shop_name="中目黒 焼肉はな",
        branch_name=None,
        area="中目黒駅",
        category="焼肉",
        needs_review=False,
        confidence_reason="投稿本文に店名とエリアの記載がある",
    )
    candidate = PipelineCandidate(
        name="中目黒 焼肉はな",
        area=None,
        category="焼肉",
        address="東京都目黒区上目黒1-2-3",
        canonical_url="https://directory.example/nakameguro-yakiniku-hana",
        evidence_url="https://directory.example/nakameguro-yakiniku-hana",
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
    )

    shop = identification_pipeline._new_shop_from_candidate(mention, candidate)

    assert shop.area == "中目黒"


@pytest.mark.parametrize(
    ("raw_area", "candidate_area", "expected_area"),
    [("台東区", "東京都台東区", "東京都台東区"), ("架空区", "東京都架空区", None)],
)
def test_new_shop_classifies_municipality_and_rejects_unknown_candidate_area(
    raw_area: str,
    candidate_area: str,
    expected_area: str | None,
) -> None:
    mention = ExtractedMention(
        shop_name="台東区 焼肉はな",
        branch_name=None,
        area=raw_area,
        category="焼肉",
        needs_review=False,
        confidence_reason="投稿本文に店名と市区町村の記載がある",
    )
    candidate = PipelineCandidate(
        name="台東区 焼肉はな",
        area=candidate_area,
        category="焼肉",
        canonical_url="https://directory.example/taito-yakiniku-hana",
        evidence_url="https://directory.example/taito-yakiniku-hana",
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
    )

    shop = identification_pipeline._new_shop_from_candidate(mention, candidate)
    status, difference_type = identification_pipeline._metadata_review_state(
        mention.area,
        shop.category,
    )

    assert shop.area == expected_area
    assert status == (
        MetadataReviewStatus.APPROVED if expected_area else MetadataReviewStatus.PENDING
    )
    assert difference_type == (None if expected_area else "unknown_area")


def test_new_shop_stores_branch_separately_from_candidate_name() -> None:
    mention = ExtractedMention(
        shop_name="Cafe Alpha",
        branch_name="渋谷店",
        area="渋谷",
        category="カフェ・喫茶店",
        needs_review=True,
        confidence_reason="出典URLに店名と支店名がある",
    )
    candidate = PipelineCandidate(
        name="Cafe Alpha 渋谷店",
        area="渋谷",
        category="カフェ・喫茶店",
        address="東京都渋谷区渋谷1-2-3",
        canonical_url="https://directory.example/cafe-alpha-shibuya",
        evidence_url="https://directory.example/cafe-alpha-shibuya",
        provenance=CandidateProvenance.WEB_SEARCH,
        is_verified=False,
        verification_reason="web search result",
    )

    shop = identification_pipeline._new_shop_from_candidate(mention, candidate)

    assert shop.shop_name == "Cafe Alpha"
    assert shop.branch_name == "渋谷店"


def test_verified_candidate_cannot_overwrite_conflicting_area_with_mention_area() -> None:
    mention = ExtractedMention(
        shop_name="銀座 焼肉はな",
        branch_name=None,
        area="銀座",
        category="焼肉",
        needs_review=False,
        confidence_reason="投稿本文に店名とエリアの記載がある",
    )
    candidate = PipelineCandidate(
        name="新宿 焼肉はな",
        area="新宿",
        category="焼肉",
        address="東京都新宿区新宿1-2-3",
        canonical_url="https://official.example/shinjuku-yakiniku-hana",
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
    )

    with pytest.raises(RuntimeError, match="area conflicts"):
        identification_pipeline._new_shop_from_candidate(mention, candidate)


def add_approved_shop(db: Session) -> Shop:
    message = Message(
        message_id=LEGACY_MESSAGE_ID,
        channel_id=CHANNEL_ID,
        content="既存の承認済み投稿",
        processing_status="succeeded",
    )
    shop = Shop(
        shop_name="銀座 鮨はな",
        branch_name="本店",
        area="銀座",
        category="メキシコ料理",
    )
    mention = ShopMention(
        message=message,
        shop=shop,
        occurrence_index=0,
        extracted_name=shop.shop_name,
        extracted_branch_name=shop.branch_name,
        extracted_area=shop.area,
        extracted_category=shop.category,
        resolution_status="resolved",
        review_status="approved",
        metadata_review_status="pending",
        metadata_difference_type="unknown_category",
        resolution_method="manual",
        extraction_source="legacy_import",
    )
    db.add(mention)
    db.commit()
    return shop


def add_approved_source_shop(db: Session, *, message_id: str = LEGACY_MESSAGE_ID) -> Shop:
    message = Message(
        message_id=message_id,
        channel_id=CHANNEL_ID,
        content=SOURCE_URL,
        processing_status="succeeded",
    )
    message.assets.append(
        SourceAsset(
            kind="link",
            url=SOURCE_URL,
            source_service="x",
            source_item_id="1924813456236278183",
            normalized_url="https://x.com/i/status/1924813456236278183",
            content_fingerprint="a" * 64,
            fetch_status="available",
        )
    )
    shop = Shop(
        shop_name="浅草橋 焼肉はな",
        area="浅草橋",
        category="焼肉",
    )
    message.mentions.append(
        ShopMention(
            shop=shop,
            occurrence_index=0,
            extracted_name=shop.shop_name,
            extracted_area=shop.area,
            extracted_category=shop.category,
            resolution_status="resolved",
            review_status="approved",
            metadata_review_status="approved",
            resolution_method="manual",
            extraction_source="legacy_import",
        )
    )
    db.add(message)
    db.commit()
    return shop


def test_same_social_source_reuses_one_approved_shop_before_ai(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_extraction(_content: str) -> ExtractionCallResult:
        raise AssertionError("source reuse must run before extraction")

    monkeypatch.setattr(
        identification_pipeline,
        "extract_restaurant_message",
        unexpected_extraction,
    )
    db = db_factory()
    try:
        shop = add_approved_source_shop(db)
        message_envelope = MessageEnvelope(
            message_id=NEXT_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content="https://twitter.com/another/status/1924813456236278183?s=20",
            created_at=datetime.now(timezone.utc),
            assets=(
                SourceAssetInput(
                    kind="link",
                    url="https://twitter.com/another/status/1924813456236278183?s=20",
                ),
                SourceAssetInput(
                    kind="image",
                    url="https://media.discordapp.net/external/preview.jpg",
                    is_embed_preview=True,
                ),
            ),
        )

        result = asyncio.run(
            identification_pipeline.process_message(db, message_envelope)
        )
        mention = (
            db.query(ShopMention)
            .filter(ShopMention.message_id == NEXT_MESSAGE_ID)
            .one()
        )
        asset = (
            db.query(SourceAsset)
            .filter(
                SourceAsset.message_id == NEXT_MESSAGE_ID,
                SourceAsset.kind == "link",
            )
            .one()
        )

        assert result.shop_ids == (shop.id,)
        assert result.pending_mention_ids == ()
        assert mention.shop_id == shop.id
        assert mention.review_status == "approved"
        assert mention.resolution_basis == "source_reuse"
        assert mention.reused_from_mention_id == shop.mentions[0].id
        assert asset.source_service == "x"
        assert asset.source_item_id == "1924813456236278183"
        assert asset.normalized_url == "https://x.com/i/status/1924813456236278183"
        assert len(asset.content_fingerprint or "") == 64
        assert (
            db.query(SourceAsset)
            .filter(SourceAsset.message_id == NEXT_MESSAGE_ID)
            .count()
            == 2
        )
    finally:
        db.close()


def test_social_source_with_multiple_non_rejected_mentions_is_not_reused(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction_calls = 0

    async def ignored_after_reuse_check(_content: str) -> ExtractionCallResult:
        nonlocal extraction_calls
        extraction_calls += 1
        return ignored_extraction()

    monkeypatch.setattr(
        identification_pipeline,
        "extract_restaurant_message",
        ignored_after_reuse_check,
    )
    db = db_factory()
    try:
        shop = add_approved_source_shop(db)
        source_message = db.query(Message).filter(Message.message_id == LEGACY_MESSAGE_ID).one()
        source_message.mentions.append(
            ShopMention(
                shop=None,
                occurrence_index=1,
                extracted_name="別の店舗候補",
                extracted_area="銀座",
                extracted_category="焼肉",
                resolution_status="ambiguous",
                review_status="pending",
                metadata_review_status="approved",
                extraction_source="responses_structured",
            )
        )
        db.commit()
        message_envelope = MessageEnvelope(
            message_id=NEXT_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content=SOURCE_URL,
            created_at=datetime.now(timezone.utc),
            assets=(SourceAssetInput(kind="link", url=SOURCE_URL),),
        )

        result = asyncio.run(
            identification_pipeline.process_message(db, message_envelope)
        )

        assert extraction_calls == 1
        assert result.ignored is True
        assert result.shop_ids == ()
        assert db.query(Shop).count() == 1
        assert shop.id is not None
    finally:
        db.close()


def test_social_source_with_conflicting_selected_candidate_is_not_reused(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        shop = add_approved_source_shop(db)
        shop.external_source = "tabelog"
        shop.external_id = "13000001"
        source_mention = shop.mentions[0]
        source_mention.candidates.append(
            ResolutionCandidate(
                rank=1,
                name=shop.shop_name,
                area=shop.area,
                external_source="tabelog",
                external_id="13000002",
                provenance="web_search",
                is_verified=True,
                is_strong_match=True,
                name_similarity_milli=1_000,
            )
        )
        db.commit()
        message_envelope = MessageEnvelope(
            message_id=NEXT_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content=SOURCE_URL,
            created_at=datetime.now(timezone.utc),
            assets=(SourceAssetInput(kind="link", url=SOURCE_URL),),
        )

        match = identification_pipeline._find_source_reuse_match(
            db,
            message_envelope,
        )

        assert match is None
    finally:
        db.close()


def test_social_source_reuse_requires_url_only_or_embed_only_content() -> None:
    message_envelope = MessageEnvelope(
        message_id=NEXT_MESSAGE_ID,
        channel_id=CHANNEL_ID,
        content=f"別の店かもしれない {SOURCE_URL}",
        created_at=datetime.now(timezone.utc),
        assets=(SourceAssetInput(kind="link", url=SOURCE_URL),),
    )

    assert identification_pipeline._source_identity_for_reuse(message_envelope) is None


def test_social_source_reuse_rejects_a_second_source_url() -> None:
    message_envelope = MessageEnvelope(
        message_id=NEXT_MESSAGE_ID,
        channel_id=CHANNEL_ID,
        content=f"{SOURCE_URL}\nhttps://youtu.be/dQw4w9WgXcQ",
        created_at=datetime.now(timezone.utc),
        assets=(
            SourceAssetInput(kind="link", url=SOURCE_URL),
            SourceAssetInput(kind="embed", url="https://youtu.be/dQw4w9WgXcQ"),
        ),
    )

    assert identification_pipeline._source_identity_for_reuse(message_envelope) is None


def test_social_source_reuse_allows_link_and_embed_for_the_same_source() -> None:
    message_envelope = MessageEnvelope(
        message_id=NEXT_MESSAGE_ID,
        channel_id=CHANNEL_ID,
        content=(
            f"{SOURCE_URL}\n"
            "[Embed URL] https://twitter.com/food/status/1924813456236278183"
        ),
        created_at=datetime.now(timezone.utc),
        assets=(
            SourceAssetInput(kind="link", url=SOURCE_URL),
            SourceAssetInput(
                kind="embed",
                url="https://twitter.com/food/status/1924813456236278183",
            ),
        ),
    )

    identity = identification_pipeline._source_identity_for_reuse(message_envelope)

    assert identity is not None
    assert identity.service.value == "x"
    assert identity.item_id == "1924813456236278183"


def test_social_source_reuse_ignores_discord_embed_previews() -> None:
    message_envelope = MessageEnvelope(
        message_id=NEXT_MESSAGE_ID,
        channel_id=CHANNEL_ID,
        content=(
            f"{SOURCE_URL}\n"
            "[Embed URL] https://twitter.com/food/status/1924813456236278183"
        ),
        created_at=datetime.now(timezone.utc),
        assets=(
            SourceAssetInput(kind="link", url=SOURCE_URL),
            SourceAssetInput(
                kind="embed",
                url="https://twitter.com/food/status/1924813456236278183",
            ),
            SourceAssetInput(
                kind="image",
                url="https://media.discordapp.net/external/preview.jpg",
                is_embed_preview=True,
            ),
            SourceAssetInput(
                kind="image",
                url="https://images-ext-1.discordapp.net/external/thumb.jpg",
                is_embed_preview=True,
            ),
        ),
    )

    identity = identification_pipeline._source_identity_for_reuse(message_envelope)

    assert identity is not None
    assert identity.service.value == "x"
    assert identity.item_id == "1924813456236278183"


@pytest.mark.parametrize("extra_kind", ["image", "attachment"])
def test_social_source_reuse_rejects_image_or_attachment(extra_kind: str) -> None:
    message_envelope = MessageEnvelope(
        message_id=NEXT_MESSAGE_ID,
        channel_id=CHANNEL_ID,
        content=SOURCE_URL,
        created_at=datetime.now(timezone.utc),
        assets=(
            SourceAssetInput(kind="link", url=SOURCE_URL),
            SourceAssetInput(
                kind=extra_kind,
                url="https://cdn.discordapp.com/attachments/1/2/evidence.jpg",
            ),
        ),
    )

    assert identification_pipeline._source_identity_for_reuse(message_envelope) is None


def test_approved_mention_identity_is_used_as_shop_alias(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        shop = add_approved_source_shop(db)
        shop.shop_name = "Yakiniku Hana"
        db.commit()
        mention = ExtractedMention(
            shop_name="浅草橋 焼肉はな",
            branch_name=None,
            area="浅草橋",
            category="焼肉",
            needs_review=False,
            confidence_reason="投稿本文に店名とエリアがある",
        )

        selected, reason = identification_pipeline._find_existing_shop_from_mention(
            db,
            mention,
            None,
        )

        assert selected is not None
        assert selected.id == shop.id
        assert "一意に一致" in (reason or "")
    finally:
        db.close()


def test_verified_candidate_collision_links_one_approved_shop(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        shop = add_approved_source_shop(db)
        shop.canonical_url = "https://official.example/yakiniku-hana"
        db.commit()
        mention = known_category_extraction().message.mentions[0].model_copy(
            update={"shop_name": "浅草橋 焼肉はなあ"}
        )
        candidate = PipelineCandidate(
            name="浅草橋 焼肉はな",
            area="浅草橋",
            category="焼肉",
            canonical_url="https://official.example/yakiniku-hana/",
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            verification_reason="server-fetched JSON-LD",
        )

        remaining, selected, reason = (
            identification_pipeline._guard_new_shop_creation_or_link(
                db,
                mention,
                candidate,
                None,
            )
        )

        assert remaining is None
        assert selected is not None
        assert selected.id == shop.id
        assert "承認済み既存店舗一件" in (reason or "")
    finally:
        db.close()


def test_name_area_collision_rejects_asymmetric_branch_presence(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        add_approved_source_shop(db)
        mention = ExtractedMention(
            shop_name="浅草橋 焼肉はな",
            branch_name="本店",
            area="浅草橋",
            category="焼肉",
            needs_review=False,
            confidence_reason="投稿本文に店名と支店名とエリアがある",
        )
        candidate = PipelineCandidate(
            name="浅草橋 焼肉はな",
            area="浅草橋",
            category="焼肉",
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            verification_reason="server-fetched JSON-LD",
        )

        remaining, selected, reason = (
            identification_pipeline._guard_new_shop_creation_or_link(
                db,
                mention,
                candidate,
                None,
            )
        )

        assert remaining is None
        assert selected is None
        assert "支店名が矛盾" in (reason or "")
    finally:
        db.close()


def test_exact_external_id_allows_asymmetric_branch_presence(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        shop = add_approved_source_shop(db)
        shop.external_source = "tabelog"
        shop.external_id = "13000001"
        db.commit()
        mention = ExtractedMention(
            shop_name="浅草橋 焼肉はな",
            branch_name="本店",
            area="浅草橋",
            category="焼肉",
            needs_review=False,
            confidence_reason="投稿本文に店名と支店名とエリアがある",
        )
        candidate = PipelineCandidate(
            name="浅草橋 焼肉はな",
            area="浅草橋",
            category="焼肉",
            external_source="tabelog",
            external_id="13000001",
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            verification_reason="server-fetched JSON-LD",
        )

        remaining, selected, reason = (
            identification_pipeline._guard_new_shop_creation_or_link(
                db,
                mention,
                candidate,
                None,
            )
        )

        assert remaining is None
        assert selected is not None
        assert selected.id == shop.id
        assert "承認済み既存店舗一件" in (reason or "")
    finally:
        db.close()


def test_evidence_url_alone_does_not_link_existing_shop(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        shop = add_approved_source_shop(db)
        shop.canonical_url = "https://social.example/shared-post"
        db.commit()
        mention = ExtractedMention(
            shop_name="新宿 焼肉はな",
            branch_name=None,
            area="新宿",
            category="焼肉",
            needs_review=False,
            confidence_reason="投稿本文に店名とエリアがある",
        )
        candidate = PipelineCandidate(
            name="新宿 焼肉はな",
            area="新宿",
            category="焼肉",
            evidence_url="https://social.example/shared-post/",
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            verification_reason="server-fetched JSON-LD",
        )

        remaining, selected, reason = (
            identification_pipeline._guard_new_shop_creation_or_link(
                db,
                mention,
                candidate,
                None,
            )
        )

        assert remaining is None
        assert selected is None
        assert "根拠URLだけ" in (reason or "")
    finally:
        db.close()


def test_verified_external_id_match_rejects_phone_and_address_conflicts(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        shop = add_approved_shop(db)
        shop.phone = "03-1111-1111"
        shop.address = "東京都中央区銀座1-1-1"
        shop.external_source = "tabelog"
        shop.external_id = "13000001"
        db.commit()
        candidate = PipelineCandidate(
            name="銀座 鮨はな 本店",
            area="銀座",
            category="メキシコ料理",
            phone="03-9999-9999",
            address="東京都中央区銀座9-9-9",
            canonical_url="https://tabelog.com/tokyo/A1301/A130101/13000001/",
            external_source="tabelog",
            external_id="13000001",
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            verification_reason="server-fetched JSON-LD",
        )

        selected, reason = identification_pipeline._find_existing_shop(
            db,
            branch_extraction().message.mentions[0],
            [candidate],
        )

        assert selected is None
        assert reason == "強い一致根拠と候補情報が矛盾: fields=address,phone"
    finally:
        db.close()


def test_existing_shop_with_conflicting_external_ids_is_not_merged(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        shop = add_approved_shop(db)
        shop.external_source = "tabelog"
        shop.external_id = "13000001"
        shop.canonical_url = (
            "https://tabelog.com/tokyo/A1301/A130101/13000002/"
        )
        db.commit()
        candidate = PipelineCandidate(
            name="銀座 鮨はな 本店",
            area="銀座",
            category="寿司・回転寿司",
            canonical_url=(
                "https://tabelog.com/tokyo/A1301/A130101/13000001/"
            ),
            external_source="tabelog",
            external_id="13000001",
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            verification_reason="server-fetched JSON-LD",
        )

        selected, reason = identification_pipeline._find_existing_shop(
            db,
            branch_extraction().message.mentions[0],
            [candidate],
        )

        assert selected is None
        assert reason == "候補の根拠に一致する既存店舗内で外部IDが矛盾"
    finally:
        db.close()


def test_external_id_mapped_to_two_shops_blocks_every_existing_merge_path(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        approved = add_approved_shop(db)
        approved.external_source = "tabelog"
        approved.external_id = "13000001"
        pending_message = Message(
            message_id="12345678901234568",
            channel_id=CHANNEL_ID,
            content="別店舗の未確認投稿",
            processing_status="succeeded",
        )
        pending_shop = Shop(
            shop_name="別の鮨店",
            area="新宿",
            external_source="食べログ",
            external_id="13000001",
        )
        db.add(
            ShopMention(
                message=pending_message,
                shop=pending_shop,
                occurrence_index=0,
                extracted_name=pending_shop.shop_name,
                extracted_area=pending_shop.area,
                resolution_status="ambiguous",
                review_status="pending",
                resolution_method="automatic",
                extraction_source="responses_structured",
            )
        )
        db.commit()
        mention = branch_extraction().message.mentions[0]
        canonical_url = "https://tabelog.com/tokyo/A1301/A130101/13000001/"
        candidate = PipelineCandidate(
            name="銀座 鮨はな 本店",
            area="銀座",
            category="寿司・回転寿司",
            canonical_url=canonical_url,
            external_source="tabelog",
            external_id="13000001",
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            verification_reason="server-fetched JSON-LD",
        )

        structured_shop, structured_reason = identification_pipeline._find_existing_shop(
            db,
            mention,
            [candidate],
        )
        posted_shop, posted_reason = (
            identification_pipeline._find_existing_shop_from_mention(
                db,
                mention,
                canonical_url,
            )
        )

        assert structured_shop is None
        assert structured_reason == "候補の外部IDまたはURLが複数の既存店舗に紐付いている"
        assert posted_shop is None
        assert posted_reason == "投稿URLの外部IDに複数の店舗が紐付いている"
    finally:
        db.close()


def add_approved_identity_shop(
    db: Session,
    *,
    message_id: str,
    shop_name: str,
    phone: str | None = None,
    address: str | None = None,
    area: str = "台東区",
    category: str = "焼肉",
) -> Shop:
    message = Message(
        message_id=message_id,
        channel_id=CHANNEL_ID,
        content="画像照合用の承認済み投稿",
        processing_status="succeeded",
    )
    shop = Shop(
        shop_name=shop_name,
        area=area,
        category=category,
        phone=phone,
        address=address,
    )
    db.add(
        ShopMention(
            message=message,
            shop=shop,
            occurrence_index=0,
            extracted_name=shop_name,
            extracted_area=area,
            extracted_category=category,
            resolution_status="resolved",
            review_status="approved",
            metadata_review_status="approved",
            resolution_method="manual",
            extraction_source="test",
        )
    )
    db.commit()
    return shop


def envelope(message_id: str = MESSAGE_ID) -> MessageEnvelope:
    return MessageEnvelope(
        message_id=message_id,
        channel_id=CHANNEL_ID,
        content=f"一人焼肉へ。お店イチオシの厚切り上タン。 {SOURCE_URL}",
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        assets=(
            SourceAssetInput(
                kind="link",
                url=SOURCE_URL,
                title="abcdefghij abcdefghxy Cafe Alpha 銀座",
            ),
            SourceAssetInput(kind="image", url=IMAGE_URL, mime_type="image/jpeg"),
        ),
    )


def install_verified_page(
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    address: str,
    url: str,
) -> None:
    async def fetch_page(requested_url: str) -> FetchedHtmlDocument:
        assert requested_url == url
        page = (
            f"<html><head><title>{html.escape(name)}</title></head>"
            f"<body><h1>{html.escape(name)}</h1>"
            f"<p>{html.escape(name)} {html.escape(address)}</p></body></html>"
        )
        return FetchedHtmlDocument(html=page, final_url=url)

    monkeypatch.setattr(
        identification_pipeline,
        "fetch_html_document",
        fetch_page,
    )


def image_analysis_result(
    *,
    names: list[str],
    phones: list[str] | None = None,
    addresses: list[str] | None = None,
    reason: str,
) -> ImageAnalysisResult:
    return ImageAnalysisResult(
        clues=ImageClues(
            usable=True,
            image_type="receipt",
            visible_shop_names=names,
            address_clues=addresses or [],
            phone_clues=phones or [],
            reason=reason,
        ),
        metrics=metrics(),
    )


def test_unresolved_restaurant_message_becomes_pending_review(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    monkeypatch.setattr(
        identification_pipeline,
        "extract_restaurant_message",
        fake_extract,
    )
    db = db_factory()
    try:
        message_envelope = envelope().model_copy(
            update={"assets": (SourceAssetInput(kind="link", url=SOURCE_URL),)}
        )
        result = asyncio.run(
            identification_pipeline.process_message(db, message_envelope)
        )
        message = db.query(Message).filter(Message.message_id == MESSAGE_ID).one()
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert result.ignored is False
        assert message.processing_status == "succeeded"
        assert message.fetch_error is None
        assert mention.shop_id is None
        assert mention.extracted_name == identification_pipeline.UNRESOLVED_SHOP_LABEL
        assert mention.resolution_status == "not_found"
        assert mention.review_status == "pending"
        assert mention.difference_type == "extraction_not_found"
        assert mention.source_url == SOURCE_URL
        assert db.query(SourceAsset).filter(SourceAsset.message_id == MESSAGE_ID).count() == 1
    finally:
        db.close()


@pytest.mark.parametrize("extraction", [unresolved_extraction, ignored_extraction])
def test_message_transaction_uses_legacy_hint_only_for_candidate_search(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    extraction: Callable[[], ExtractionCallResult],
) -> None:
    legacy_hint = known_category_extraction().message.mentions[0].model_copy(
        update={"confidence_reason": "旧" * 2_000}
    )

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def one_web_candidate(
        _db: Session,
        _message_id: str,
        mention: ExtractedMention,
    ) -> CandidateBatch:
        assert mention.shop_name == legacy_hint.shop_name
        assert mention.area == legacy_hint.area
        assert mention.needs_review is True
        assert "新規抽出結果" in mention.confidence_reason
        return CandidateBatch(
            (
                PipelineCandidate(
                    name="浅草橋 焼肉はな",
                    area="東京都台東区",
                    category="焼肉",
                    address="東京都台東区浅草橋1-2-3",
                    canonical_url="https://directory.example/yakiniku-hana",
                    evidence_url="https://directory.example/yakiniku-hana",
                    provenance=CandidateProvenance.WEB_SEARCH,
                    is_verified=False,
                    verification_reason="web search result",
                ),
            )
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", one_web_candidate)
    install_verified_page(
        monkeypatch,
        name="浅草橋 焼肉はな",
        address="東京都台東区浅草橋1-2-3",
        url="https://directory.example/yakiniku-hana",
    )
    db = db_factory()
    try:
        message_envelope = envelope().model_copy(
            update={"assets": (SourceAssetInput(kind="link", url=SOURCE_URL),)}
        )
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                message_envelope,
                allow_image_fallback=False,
                fallback_mentions=(legacy_hint,),
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()
        shop = db.query(Shop).filter_by(id=mention.shop_id).one()

        assert result.ignored is False
        assert result.shop_ids == (shop.id,)
        assert result.pending_mention_ids == ()
        assert mention.extraction_source == "legacy_hint"
        assert mention.review_status == "approved"
        assert "新規抽出結果" in (mention.confidence_reason or "")
        assert shop.shop_name == "浅草橋 焼肉はな"
        assert shop.area == "浅草橋"
    finally:
        db.close()


def test_message_transaction_does_not_confirm_legacy_hint_without_candidate(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_hint = known_category_extraction().message.mentions[0].model_copy(
        update={"confidence_reason": "既存の抽出値は候補検索ヒント"}
    )

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidate(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidate)
    db = db_factory()
    try:
        message_envelope = envelope().model_copy(
            update={"assets": (SourceAssetInput(kind="link", url=SOURCE_URL),)}
        )
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                message_envelope,
                allow_image_fallback=False,
                fallback_mentions=(legacy_hint,),
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert db.query(Shop).count() == 0
        assert mention.extraction_source == "legacy_hint"
        assert mention.review_status == "pending"
        assert mention.extracted_name == legacy_hint.shop_name
        assert "投稿に店名がありません" in (mention.confidence_reason or "")
        assert (mention.confidence_reason or "").startswith("新規抽出結果")
        assert len(mention.confidence_reason or "") <= 2_000
    finally:
        db.close()


def test_legacy_hint_does_not_shortcut_to_existing_shop_without_candidate(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_hint = branch_extraction().message.mentions[0]

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidate(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidate)
    db = db_factory()
    try:
        add_approved_shop(db)
        message_envelope = envelope().model_copy(
            update={"assets": (SourceAssetInput(kind="link", url=SOURCE_URL),)}
        )
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                message_envelope,
                allow_image_fallback=False,
                fallback_mentions=(legacy_hint,),
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert mention.shop_id is None
        assert db.query(Shop).count() == 1
    finally:
        db.close()


def test_legacy_hint_does_not_create_shop_from_posted_url_without_candidate(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_hint = known_category_extraction().message.mentions[0]

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def no_candidates(*_args: object) -> list[PipelineCandidate]:
        return []

    async def no_web_candidate(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "_structured_candidates", no_candidates)
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidate)
    db = db_factory()
    try:
        posted_url = "https://tabelog.com/tokyo/A1311/A131103/13000001/"
        message_envelope = envelope().model_copy(
            update={"assets": (SourceAssetInput(kind="link", url=posted_url),)}
        )
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                message_envelope,
                allow_image_fallback=False,
                fallback_mentions=(legacy_hint,),
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert mention.shop_id is None
        assert db.query(Shop).count() == 0
    finally:
        db.close()


def test_legacy_hint_does_not_use_image_resolution(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_hint = known_category_extraction().message.mentions[0]

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidate(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    async def unexpected_image(*_args: object) -> ImageAnalysisResult:
        raise AssertionError("legacy hint must not use image resolution")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidate)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", unexpected_image)
    db = db_factory()
    try:
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                fallback_mentions=(legacy_hint,),
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert mention.shop_id is None
    finally:
        db.close()


def test_fresh_mentions_take_precedence_over_legacy_hints(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh_mention = known_category_extraction().message.mentions[0]
    legacy_hint = branch_extraction().message.mentions[0]

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidate(
        _db: Session,
        _message_id: str,
        mention: ExtractedMention,
    ) -> CandidateBatch:
        assert mention == fresh_mention
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidate)
    db = db_factory()
    try:
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                allow_image_fallback=False,
                fallback_mentions=(legacy_hint,),
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert result.pending_mention_ids == (mention.id,)
        assert mention.extracted_name == fresh_mention.shop_name
        assert mention.extracted_name != legacy_hint.shop_name
        assert mention.extraction_source == "responses_structured"
    finally:
        db.close()


def test_failed_processing_preserves_source_assets(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_extract(_content: str) -> ExtractionCallResult:
        raise ExtractionError("structured output is invalid")

    monkeypatch.setattr(
        identification_pipeline,
        "extract_restaurant_message",
        failing_extract,
    )
    db = db_factory()
    try:
        with pytest.raises(ExtractionError, match="structured output is invalid"):
            asyncio.run(identification_pipeline.process_message(db, envelope()))

        message = db.query(Message).filter(Message.message_id == MESSAGE_ID).one()
        assets = (
            db.query(SourceAsset)
            .filter(SourceAsset.message_id == MESSAGE_ID)
            .order_by(SourceAsset.kind.asc())
            .all()
        )
        assert message.processing_status == "failed"
        assert message.fetch_error == "ExtractionError: structured output is invalid"
        assert [(asset.kind, asset.url) for asset in assets] == [
            ("image", IMAGE_URL),
            ("link", SOURCE_URL),
        ]
        stages = {
            run.stage
            for run in db.query(ProcessingRun)
            .filter(ProcessingRun.message_id == MESSAGE_ID)
            .all()
        }
        assert stages == {"message_extraction", "message_pipeline"}
    finally:
        db.close()


def test_paid_call_metrics_and_cache_survive_later_pipeline_failure(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def cached_structured_lookup(
        db: Session,
        _message_id: str,
        url: str,
    ) -> list[PipelineCandidate]:
        identification_pipeline._put_cache(db, "url_metadata", url, '{"candidates":[]}')
        return []

    async def failing_web_lookup(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        raise ExtractionError("candidate search failed after billing")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        cached_structured_lookup,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", failing_web_lookup)
    db = db_factory()
    try:
        with pytest.raises(ExtractionError, match="candidate search failed after billing"):
            asyncio.run(identification_pipeline.process_message(db, envelope()))

        assert (
            db.query(ProcessingRun)
            .filter(
                ProcessingRun.message_id == MESSAGE_ID,
                ProcessingRun.stage == "message_extraction",
            )
            .count()
            == 1
        )
        assert db.query(LookupCache).filter(LookupCache.kind == "url_metadata").count() == 1
    finally:
        db.close()


def test_ambiguous_unknown_category_stays_shopless_with_separate_review_states(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unknown_category_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    message_envelope = envelope().model_copy(
        update={
            "assets": (SourceAssetInput(kind="link", url=SOURCE_URL),),
        }
    )
    db = db_factory()
    try:
        result = asyncio.run(identification_pipeline.process_message(db, message_envelope))
        message = db.query(Message).filter(Message.message_id == MESSAGE_ID).one()
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert message.processing_status == "succeeded"
        assert result.pending_mention_ids == (mention.id,)
        assert result.shop_ids == ()
        assert mention.shop_id is None
        assert db.query(Shop).count() == 0
        assert mention.extracted_category == "メキシコ料理"
        assert mention.source_url == SOURCE_URL
        assert mention.review_status == "pending"
        assert mention.difference_type == "new_ambiguous"
        assert mention.metadata_review_status == "pending"
        assert mention.metadata_difference_type == "unknown_category"
        assert "未知カテゴリ" in (mention.confidence_reason or "")
    finally:
        db.close()


def test_approved_name_branch_area_match_skips_lookup_and_preserves_shop_count(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return branch_extraction()

    async def unexpected_structured_lookup(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        raise AssertionError("structured lookup must be skipped for an exact approved match")

    async def unexpected_web_lookup(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        raise AssertionError("web lookup must be skipped for an exact approved match")

    async def unexpected_image_lookup(
        _mention: ExtractedMention,
        _image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        raise AssertionError("image lookup must be skipped for an exact approved match")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        unexpected_structured_lookup,
    )
    monkeypatch.setattr(
        identification_pipeline,
        "_web_candidates",
        unexpected_web_lookup,
    )
    monkeypatch.setattr(
        identification_pipeline,
        "analyze_restaurant_images",
        unexpected_image_lookup,
    )
    db = db_factory()
    try:
        existing_shop = add_approved_shop(db)
        original_shop_count = db.query(Shop).count()

        result = asyncio.run(identification_pipeline.process_message(db, envelope()))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert db.query(Shop).count() == original_shop_count
        assert result.shop_ids == (existing_shop.id,)
        assert result.pending_mention_ids == ()
        assert mention.shop_id == existing_shop.id
        assert mention.extracted_branch_name == "本店"
        assert mention.resolution_status == "resolved"
        assert mention.review_status == "approved"
        assert mention.resolution_method == "automatic"
        assert mention.difference_type is None
        assert mention.metadata_review_status == "pending"
        assert mention.metadata_difference_type == "unknown_category"
    finally:
        db.close()


def test_page_verified_top_web_candidate_creates_shop_before_image_analysis(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def one_web_candidate(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(
            (
                PipelineCandidate(
                    name="浅草橋 焼肉はな",
                    area="浅草橋",
                    category="焼肉",
                    address="東京都台東区浅草橋1-2-3",
                    canonical_url="https://directory.example/yakiniku-hana",
                    evidence_url="https://directory.example/yakiniku-hana",
                    provenance=CandidateProvenance.WEB_SEARCH,
                    is_verified=False,
                    verification_reason="web search result",
                ),
            )
        )

    async def unexpected_image_lookup(
        _mention: ExtractedMention | None,
        _image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        raise AssertionError("image analysis must be skipped after page verification")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", one_web_candidate)
    monkeypatch.setattr(
        identification_pipeline,
        "analyze_restaurant_images",
        unexpected_image_lookup,
    )
    install_verified_page(
        monkeypatch,
        name="浅草橋 焼肉はな",
        address="東京都台東区浅草橋1-2-3",
        url="https://directory.example/yakiniku-hana",
    )
    message_envelope = envelope().model_copy(
        update={
            "assets": (
                SourceAssetInput(kind="link", url=SOURCE_URL),
                SourceAssetInput(kind="image", url=IMAGE_URL),
            )
        }
    )
    db = db_factory()
    try:
        result = asyncio.run(identification_pipeline.process_message(db, message_envelope))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert len(result.shop_ids) == 1
        assert result.pending_mention_ids == ()
        assert mention.shop_id == result.shop_ids[0]
        assert db.query(Shop).count() == 1
        assert mention.review_status == "approved"
        assert mention.difference_type is None
        assert mention.metadata_review_status == "approved"
        assert mention.metadata_difference_type is None
        assert "policy=unique_top_web_matching_score_080" in (
            mention.confidence_reason or ""
        )
        assert "matching_score=1.000" in (mention.confidence_reason or "")
        assert "source_verified=true" in (mention.confidence_reason or "")
        assert len(mention.candidates) == 1
        assert mention.candidates[0].provenance == CandidateProvenance.WEB_SEARCH.value
        assert mention.candidates[0].is_verified is True
        assert mention.candidates[0].is_strong_match is True
        shop = db.query(Shop).filter(Shop.id == mention.shop_id).one()
        assert shop.shop_name == "浅草橋 焼肉はな"
        assert shop.area == "浅草橋"
        assert shop.category == "焼肉"
        assert shop.address == "東京都台東区浅草橋1-2-3"
        assert shop.phone is None
        assert shop.canonical_url == "https://directory.example/yakiniku-hana"
        assert shop.external_source is None
        assert shop.external_id is None
    finally:
        db.close()


def test_web_candidate_is_automatic_only_after_server_structured_verification(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_url = "https://directory.example/yakiniku-hana"

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def structured_candidates(
        _db: Session,
        _message_id: str,
        url: str,
    ) -> list[PipelineCandidate]:
        if url != candidate_url:
            return []
        return [
            PipelineCandidate(
                name="浅草橋 焼肉はな",
                category="焼肉",
                address="東京都台東区浅草橋1-2-3",
                canonical_url=candidate_url,
                evidence_url=candidate_url,
                provenance=CandidateProvenance.STRUCTURED_DATA,
                is_verified=True,
                verification_reason="server-fetched JSON-LD",
            )
        ]

    async def web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(
            (
                PipelineCandidate(
                    name="浅草橋 焼肉はな",
                    area="浅草橋",
                    category="焼肉",
                    canonical_url=candidate_url,
                    evidence_url=candidate_url,
                    provenance=CandidateProvenance.WEB_SEARCH,
                    is_verified=False,
                    verification_reason="web search result",
                ),
            )
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", web_candidates)
    message_envelope = envelope().model_copy(
        update={"assets": (SourceAssetInput(kind="link", url=SOURCE_URL),)}
    )
    db = db_factory()
    try:
        result = asyncio.run(identification_pipeline.process_message(db, message_envelope))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert len(result.shop_ids) == 1
        assert result.pending_mention_ids == ()
        assert mention.review_status == "approved"
        assert mention.shop is not None
        assert mention.shop.canonical_url == candidate_url
        assert {
            (candidate.provenance, candidate.is_verified)
            for candidate in mention.candidates
        } == {
            (CandidateProvenance.STRUCTURED_DATA.value, True),
            (CandidateProvenance.WEB_SEARCH.value, False),
        }
    finally:
        db.close()


def test_image_phone_match_can_select_one_approved_existing_shop(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def matching_web_candidate(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(
            (
                PipelineCandidate(
                    name="浅草橋 焼肉はな",
                    area="浅草橋",
                    category="焼肉",
                    canonical_url="https://directory.example/yakiniku-hana",
                    evidence_url="https://directory.example/yakiniku-hana",
                    provenance=CandidateProvenance.WEB_SEARCH,
                    is_verified=False,
                    verification_reason="web search result",
                ),
            )
        )

    async def image_clues(
        mention: ExtractedMention | None,
        image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        assert mention is None
        assert image_urls == (IMAGE_URL,)
        return ImageAnalysisResult(
            clues=ImageClues(
                usable=True,
                image_type="receipt",
                visible_shop_names=["浅草橋 焼肉はな"],
                address_clues=[],
                phone_clues=["03-1234-5678"],
                reason="レシートに店名と電話番号が見える",
            ),
            metrics=metrics(),
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(
        identification_pipeline,
        "_web_candidates",
        matching_web_candidate,
    )
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    db = db_factory()
    try:
        existing_message = Message(
            message_id=LEGACY_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content="既存投稿",
            processing_status="succeeded",
        )
        existing_shop = Shop(
            shop_name="浅草橋 焼肉はな",
            area="台東区",
            category="焼肉",
            phone="0312345678",
        )
        db.add(
            ShopMention(
                message=existing_message,
                shop=existing_shop,
                occurrence_index=0,
                extracted_name=existing_shop.shop_name,
                extracted_area=existing_shop.area,
                extracted_category=existing_shop.category,
                resolution_status="resolved",
                review_status="approved",
                metadata_review_status="approved",
                extraction_source="test",
            )
        )
        db.commit()

        result = asyncio.run(identification_pipeline.process_message(db, envelope()))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert result.shop_ids == (existing_shop.id,)
        assert result.pending_mention_ids == ()
        assert mention.shop_id == existing_shop.id
        assert mention.resolution_method == "automatic"
        assert mention.candidates[0].provenance == CandidateProvenance.IMAGE.value
        assert mention.candidates[0].is_verified is True
        assert mention.candidates[1].provenance == CandidateProvenance.WEB_SEARCH.value
        assert "電話番号が一致" in (mention.confidence_reason or "")
    finally:
        db.close()


def test_image_exact_name_with_only_municipality_stays_pending(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        mention = known_category_extraction().message.mentions[0].model_copy(
            update={
                "shop_name": "浅草橋 焼肉はなあ",
                "area": "台東区",
            }
        )
        return ExtractionCallResult(
            message=ExtractedMessage(
                is_restaurant_message=True,
                ignore_reason=None,
                unresolved_reason=None,
                mentions=[mention],
            ),
            metrics=metrics(),
        )

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    async def image_clues(
        mention: ExtractedMention | None,
        image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        assert mention is None
        assert image_urls == (IMAGE_URL,)
        return image_analysis_result(
            names=["浅草橋 焼肉はな"],
            reason="看板に正式な店名が見える",
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    db = db_factory()
    try:
        existing_shop = add_approved_identity_shop(
            db,
            message_id=LEGACY_MESSAGE_ID,
            shop_name="浅草橋 焼肉はな",
            area="台東区",
        )

        result = asyncio.run(identification_pipeline.process_message(db, envelope()))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert mention.shop_id is None
        assert db.query(Shop).count() == 1
        assert mention.candidates[0].provenance == CandidateProvenance.IMAGE.value
        assert mention.candidates[0].is_strong_match is False
    finally:
        db.close()


def test_image_and_server_structured_phone_match_can_create_shop(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_url = "https://official.example/yakiniku-hana"

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def structured_candidates(
        _db: Session,
        _message_id: str,
        url: str,
    ) -> list[PipelineCandidate]:
        if url != candidate_url:
            return []
        return [
            PipelineCandidate(
                name="浅草橋 焼肉はな",
                category="焼肉",
                address="東京都台東区浅草橋1-2-3",
                phone="03-1234-5678",
                canonical_url=candidate_url,
                evidence_url=candidate_url,
                provenance=CandidateProvenance.STRUCTURED_DATA,
                is_verified=True,
                verification_reason="server-fetched JSON-LD",
            )
        ]

    async def web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(
            (
                PipelineCandidate(
                    name="浅草橋 焼肉はな",
                    canonical_url=candidate_url,
                    evidence_url=candidate_url,
                    provenance=CandidateProvenance.WEB_SEARCH,
                    is_verified=False,
                    verification_reason="web search result",
                ),
            )
        )

    async def image_clues(
        _mention: ExtractedMention | None,
        _image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        return ImageAnalysisResult(
            clues=ImageClues(
                usable=True,
                image_type="sign",
                visible_shop_names=["浅草橋 焼肉はな"],
                address_clues=[],
                phone_clues=["0312345678"],
                reason="看板に店名と電話番号が見える",
            ),
            metrics=metrics(),
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", web_candidates)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    db = db_factory()
    try:
        result = asyncio.run(identification_pipeline.process_message(db, envelope()))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert len(result.shop_ids) == 1
        assert result.pending_mention_ids == ()
        assert mention.shop is not None
        assert mention.shop.canonical_url == candidate_url
        assert mention.shop.phone == "0312345678"
        assert "画像と構造化データ" in (mention.confidence_reason or "")
    finally:
        db.close()


def test_image_name_alone_does_not_create_shop(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    async def image_clues(
        _mention: ExtractedMention | None,
        _image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        return ImageAnalysisResult(
            clues=ImageClues(
                usable=True,
                image_type="sign",
                visible_shop_names=["浅草橋 焼肉はな"],
                address_clues=[],
                phone_clues=[],
                reason="看板に店名だけが見える",
            ),
            metrics=metrics(),
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    db = db_factory()
    try:
        result = asyncio.run(identification_pipeline.process_message(db, envelope()))

        assert result.shop_ids == ()
        assert len(result.pending_mention_ids) == 1
        assert db.query(Shop).count() == 0
    finally:
        db.close()


def test_multiple_image_shop_names_never_share_phone_or_auto_resolve() -> None:
    result = ImageAnalysisResult(
        clues=ImageClues(
            usable=True,
            image_type="menu",
            visible_shop_names=["店舗A", "店舗B"],
            address_clues=["東京都中央区銀座1-1-1"],
            phone_clues=["03-1234-5678"],
            reason="複数の店舗名が見える",
        ),
        metrics=metrics(),
    )

    candidates = identification_pipeline._candidates_from_image(result, IMAGE_URL)

    assert len(candidates) == 2
    assert all(candidate.phone is None for candidate in candidates)
    assert all(candidate.address is None for candidate in candidates)
    assert identification_pipeline._single_image_candidate(candidates) is None


def test_image_phone_match_with_conflicting_address_is_not_automatic() -> None:
    image_candidate = PipelineCandidate(
        name="浅草橋 焼肉はな",
        address="東京都台東区浅草橋1-2-3",
        phone="0312345678",
        evidence_url=IMAGE_URL,
        provenance=CandidateProvenance.IMAGE,
        is_verified=False,
        verification_reason="image observation",
    )
    structured_candidate = PipelineCandidate(
        name="浅草橋 焼肉はな",
        address="東京都新宿区新宿4-5-6",
        phone="0312345678",
        canonical_url="https://official.example/yakiniku-hana",
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
    )

    selected, reason = identification_pipeline._structured_candidate_from_image(
        image_candidate,
        [structured_candidate],
    )

    assert selected is None
    assert reason is not None
    assert "矛盾" in reason


def test_image_match_to_multiple_structured_candidates_is_not_automatic() -> None:
    image_candidate = PipelineCandidate(
        name="浅草橋 焼肉はな",
        phone="0312345678",
        evidence_url=IMAGE_URL,
        provenance=CandidateProvenance.IMAGE,
        is_verified=False,
        verification_reason="image observation",
    )
    structured_candidates = [
        PipelineCandidate(
            name="浅草橋 焼肉はな",
            phone="0312345678",
            canonical_url=f"https://official.example/yakiniku-hana-{index}",
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            verification_reason="server-fetched JSON-LD",
        )
        for index in range(2)
    ]

    selected, reason = identification_pipeline._structured_candidate_from_image(
        image_candidate,
        structured_candidates,
    )

    assert selected is None
    assert reason is not None
    assert "複数" in reason


def test_image_name_mismatch_blocks_phone_based_existing_match(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        add_approved_identity_shop(
            db,
            message_id=LEGACY_MESSAGE_ID,
            shop_name="浅草橋 焼肉はな",
            phone="0312345678",
        )
        extracted = ExtractedMention(
            shop_name="横浜 焼肉つき",
            branch_name=None,
            area="横浜",
            category="焼肉",
            needs_review=True,
            confidence_reason="投稿本文の店名",
        )
        image_candidate = PipelineCandidate(
            name="浅草橋 焼肉はな",
            phone="0312345678",
            evidence_url=IMAGE_URL,
            provenance=CandidateProvenance.IMAGE,
            is_verified=False,
            verification_reason="image observation",
        )

        shop, selected, reason = identification_pipeline._find_existing_shop_from_image(
            db,
            extracted,
            image_candidate,
        )

        assert shop is None
        assert selected is None
        assert reason is not None
        assert "一致しない" in reason
    finally:
        db.close()


def test_image_only_phone_match_selects_approved_existing_shop(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def image_clues(
        mention: ExtractedMention | None,
        image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        assert mention is None
        assert image_urls == (IMAGE_URL,)
        return image_analysis_result(
            names=["浅草橋 焼肉はな"],
            phones=["03-1234-5678"],
            reason="レシートに店名と電話番号が見える",
        )

    async def unexpected_web_lookup(*_args: object) -> CandidateBatch:
        raise AssertionError("approved image identity must skip web search")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    monkeypatch.setattr(identification_pipeline, "_web_candidates", unexpected_web_lookup)
    db = db_factory()
    try:
        existing_shop = add_approved_identity_shop(
            db,
            message_id=LEGACY_MESSAGE_ID,
            shop_name="浅草橋 焼肉はな",
            phone="0312345678",
        )

        result = asyncio.run(identification_pipeline.process_message(db, envelope()))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert result.shop_ids == (existing_shop.id,)
        assert result.pending_mention_ids == ()
        assert mention.shop_id == existing_shop.id
        assert mention.extracted_name == "浅草橋 焼肉はな"
        assert mention.resolution_method == "automatic"
        assert mention.extraction_source == "responses_vision"
        assert mention.candidates[0].provenance == CandidateProvenance.IMAGE.value
        assert mention.candidates[0].is_verified is True
        assert mention.candidates[0].is_strong_match is True
        assert db.query(Shop).count() == 1
    finally:
        db.close()


def test_image_only_name_without_phone_or_address_stays_pending(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def image_clues(
        _mention: ExtractedMention | None,
        _image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        return image_analysis_result(
            names=["浅草橋 焼肉はな"],
            reason="看板に店名だけが見える",
        )

    async def unexpected_web_lookup(*_args: object) -> CandidateBatch:
        raise AssertionError("image name alone must not trigger web search")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    monkeypatch.setattr(identification_pipeline, "_web_candidates", unexpected_web_lookup)
    db = db_factory()
    try:
        result = asyncio.run(identification_pipeline.process_message(db, envelope()))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert db.query(Shop).count() == 0
        assert mention.extracted_name == "浅草橋 焼肉はな"
        assert mention.difference_type == "image_unverified"
        assert mention.candidates[0].is_verified is False
        assert mention.candidates[0].is_strong_match is False
    finally:
        db.close()


def test_image_only_helper_can_leave_commit_to_caller(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def image_clues(
        _mention: ExtractedMention | None,
        _image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        return image_analysis_result(
            names=["浅草橋 焼肉はな"],
            phones=["03-1234-5678"],
            reason="看板に店名だけが見える",
        )

    def unexpected_image_identity(*_args: object) -> object:
        raise AssertionError("disabled automatic image resolution must skip identity matching")

    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    monkeypatch.setattr(
        identification_pipeline,
        "_find_existing_shop_from_image",
        unexpected_image_identity,
    )
    db = db_factory()
    try:
        message_envelope = envelope()
        message = Message(
            message_id=message_envelope.message_id,
            channel_id=message_envelope.channel_id,
            content=message_envelope.content,
            source_created_at=message_envelope.created_at,
            processing_status="processing",
        )
        db.add(message)
        db.commit()

        def unexpected_commit() -> None:
            raise AssertionError("caller-owned image processing must not commit")

        monkeypatch.setattr(db, "commit", unexpected_commit)
        result = asyncio.run(
            identification_pipeline._process_image_only_message(
                db,
                message,
                message_envelope,
                (IMAGE_URL,),
                SOURCE_URL,
                "投稿に店名がありません。",
                allow_automatic_resolution=False,
                commit=False,
            )
        )

        assert result.shop_ids == ()
        assert len(result.pending_mention_ids) == 1
        assert db.query(ShopMention).filter_by(message_id=MESSAGE_ID).count() == 1
        db.rollback()
        assert db.query(ShopMention).filter_by(message_id=MESSAGE_ID).count() == 0
    finally:
        db.close()


def test_message_transaction_can_leave_commit_to_caller(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def image_clues(
        _mention: ExtractedMention | None,
        _image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        return image_analysis_result(
            names=["浅草橋 焼肉はな"],
            reason="看板に店名だけが見える",
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    db = db_factory()
    try:
        message_envelope = envelope()

        def unexpected_commit() -> None:
            raise AssertionError("caller-owned message processing must not commit")

        monkeypatch.setattr(db, "commit", unexpected_commit)
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                message_envelope,
                commit=False,
            )
        )

        assert result.shop_ids == ()
        assert len(result.pending_mention_ids) == 1
        assert db.query(Message).filter_by(message_id=MESSAGE_ID).count() == 1
        assert db.query(ShopMention).filter_by(message_id=MESSAGE_ID).count() == 1
        db.rollback()
        assert db.query(Message).filter_by(message_id=MESSAGE_ID).count() == 0
        assert db.query(ShopMention).filter_by(message_id=MESSAGE_ID).count() == 0
    finally:
        db.close()


def test_message_transaction_failure_leaves_existing_placeholder_for_caller_rollback(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_extract(_content: str) -> ExtractionCallResult:
        raise ExtractionError("structured output is invalid")

    monkeypatch.setattr(
        identification_pipeline,
        "extract_restaurant_message",
        failing_extract,
    )
    db = db_factory()
    try:
        original_created_at = datetime(2024, 1, 2, 3, 4, tzinfo=timezone.utc)
        original_processed_at = datetime(2024, 1, 2, 3, 5, tzinfo=timezone.utc)
        message = Message(
            message_id=MESSAGE_ID,
            channel_id=LEGACY_MESSAGE_ID,
            content="店舗名を抽出できなかった元の投稿",
            source_created_at=original_created_at,
            is_target=True,
            processing_status="succeeded",
            fetch_error=None,
            processed_at=original_processed_at,
        )
        mention = ShopMention(
            message=message,
            occurrence_index=0,
            extracted_name=identification_pipeline.UNRESOLVED_SHOP_LABEL,
            extracted_area="浅草橋",
            source_url="https://example.com/original",
            resolution_status="not_found",
            review_status="pending",
            metadata_review_status="approved",
            resolution_method=None,
            difference_type="extraction_not_found",
            extraction_source="legacy_import",
            extraction_error="元の抽出エラー",
            confidence_reason="元の判定理由",
        )
        asset = SourceAsset(
            message=message,
            kind="image",
            url="https://cdn.discordapp.com/attachments/1/2/original.jpg",
            title="元の画像",
            description="元の説明",
            mime_type="image/jpeg",
            extracted_text="元の抽出文字",
            fetch_status="available",
            fetch_error=None,
        )
        db.add_all((message, mention, asset))
        db.commit()
        db.expire_all()

        original_message = db.query(Message).filter_by(message_id=MESSAGE_ID).one()
        original_mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()
        original_asset = db.query(SourceAsset).filter_by(message_id=MESSAGE_ID).one()
        message_snapshot = (
            original_message.message_id,
            original_message.channel_id,
            original_message.content,
            original_message.source_created_at,
            original_message.is_target,
            original_message.processing_status,
            original_message.fetch_error,
            original_message.processed_at,
        )
        mention_snapshot = (
            original_mention.id,
            original_mention.message_id,
            original_mention.shop_id,
            original_mention.occurrence_index,
            original_mention.extracted_name,
            original_mention.extracted_branch_name,
            original_mention.extracted_area,
            original_mention.extracted_category,
            original_mention.source_url,
            original_mention.resolution_status,
            original_mention.review_status,
            original_mention.metadata_review_status,
            original_mention.metadata_difference_type,
            original_mention.resolution_method,
            original_mention.difference_type,
            original_mention.extraction_source,
            original_mention.extraction_error,
            original_mention.confidence_reason,
            original_mention.reviewed_at,
            original_mention.metadata_reviewed_at,
            original_mention.version,
            original_mention.created_at,
        )
        asset_snapshot = (
            original_asset.id,
            original_asset.message_id,
            original_asset.kind,
            original_asset.url,
            original_asset.title,
            original_asset.description,
            original_asset.mime_type,
            original_asset.extracted_text,
            original_asset.fetch_status,
            original_asset.fetch_error,
            original_asset.created_at,
        )

        original_message.processing_status = "pending"
        db.flush()
        caller_rollback = db.rollback

        def unexpected_commit() -> None:
            raise AssertionError("caller-owned message processing must not commit")

        def unexpected_rollback() -> None:
            raise AssertionError("caller-owned message processing must not roll back")

        monkeypatch.setattr(db, "commit", unexpected_commit)
        monkeypatch.setattr(db, "rollback", unexpected_rollback)
        replacement = envelope().model_copy(
            update={
                "channel_id": CHANNEL_ID,
                "content": "再処理する投稿本文",
                "assets": (
                    SourceAssetInput(kind="link", url=SOURCE_URL),
                    SourceAssetInput(
                        kind="image",
                        url=IMAGE_URL,
                        mime_type="image/jpeg",
                    ),
                ),
            }
        )

        with pytest.raises(ExtractionError, match="structured output is invalid"):
            asyncio.run(
                identification_pipeline._process_message_transaction(
                    db,
                    replacement,
                    commit=False,
                    allow_image_fallback=False,
                )
            )

        caller_rollback()
        db.expire_all()
        restored_message = db.query(Message).filter_by(message_id=MESSAGE_ID).one()
        restored_mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()
        restored_asset = db.query(SourceAsset).filter_by(message_id=MESSAGE_ID).one()

        assert (
            restored_message.message_id,
            restored_message.channel_id,
            restored_message.content,
            restored_message.source_created_at,
            restored_message.is_target,
            restored_message.processing_status,
            restored_message.fetch_error,
            restored_message.processed_at,
        ) == message_snapshot
        assert (
            restored_mention.id,
            restored_mention.message_id,
            restored_mention.shop_id,
            restored_mention.occurrence_index,
            restored_mention.extracted_name,
            restored_mention.extracted_branch_name,
            restored_mention.extracted_area,
            restored_mention.extracted_category,
            restored_mention.source_url,
            restored_mention.resolution_status,
            restored_mention.review_status,
            restored_mention.metadata_review_status,
            restored_mention.metadata_difference_type,
            restored_mention.resolution_method,
            restored_mention.difference_type,
            restored_mention.extraction_source,
            restored_mention.extraction_error,
            restored_mention.confidence_reason,
            restored_mention.reviewed_at,
            restored_mention.metadata_reviewed_at,
            restored_mention.version,
            restored_mention.created_at,
        ) == mention_snapshot
        assert (
            restored_asset.id,
            restored_asset.message_id,
            restored_asset.kind,
            restored_asset.url,
            restored_asset.title,
            restored_asset.description,
            restored_asset.mime_type,
            restored_asset.extracted_text,
            restored_asset.fetch_status,
            restored_asset.fetch_error,
            restored_asset.created_at,
        ) == asset_snapshot
        assert db.query(ProcessingRun).filter_by(message_id=MESSAGE_ID).count() == 0
    finally:
        db.close()


def test_message_transaction_can_disable_image_fallback(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def unexpected_image(*_args: object) -> ImageAnalysisResult:
        raise AssertionError("disabled image fallback must not analyze embed images")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", unexpected_image)
    db = db_factory()
    try:
        def unexpected_commit() -> None:
            raise AssertionError("caller-owned message processing must not commit")

        monkeypatch.setattr(db, "commit", unexpected_commit)
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                commit=False,
                allow_image_fallback=False,
            )
        )

        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()
        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert mention.extraction_source == "responses_structured"
        assert db.query(SourceAsset).filter_by(message_id=MESSAGE_ID, kind="image").count() == 1
        db.rollback()
        assert db.query(Message).filter_by(message_id=MESSAGE_ID).count() == 0
    finally:
        db.close()


def test_image_only_multiple_existing_matches_stay_pending_without_search(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def image_clues(
        _mention: ExtractedMention | None,
        _image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        return image_analysis_result(
            names=["浅草橋 焼肉はな"],
            phones=["03-1234-5678"],
            reason="レシートに店名と電話番号が見える",
        )

    async def unexpected_web_lookup(*_args: object) -> CandidateBatch:
        raise AssertionError("ambiguous existing identities must block web search")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    monkeypatch.setattr(identification_pipeline, "_web_candidates", unexpected_web_lookup)
    db = db_factory()
    try:
        add_approved_identity_shop(
            db,
            message_id=LEGACY_MESSAGE_ID,
            shop_name="浅草橋 焼肉はな",
            phone="0312345678",
        )
        add_approved_identity_shop(
            db,
            message_id="12345678901234568",
            shop_name="浅草橋 焼肉はな",
            phone="0312345678",
        )

        result = asyncio.run(identification_pipeline.process_message(db, envelope()))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert db.query(Shop).count() == 2
        assert "既存店舗が複数" in (mention.confidence_reason or "")
        assert mention.candidates[0].is_verified is False
    finally:
        db.close()


def test_image_only_structured_phone_match_creates_from_structured_data(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_url = "https://official.example/yakiniku-hana"

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def image_clues(
        mention: ExtractedMention | None,
        image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        assert mention is None
        assert image_urls == (IMAGE_URL,)
        return image_analysis_result(
            names=["浅草橋 焼肉はな"],
            phones=["03-1234-5678"],
            reason="レシートに店名と電話番号が見える",
        )

    async def web_candidates(
        _db: Session,
        _message_id: str,
        mention: ExtractedMention,
    ) -> CandidateBatch:
        assert mention.shop_name == "浅草橋 焼肉はな"
        assert mention.area is None
        return CandidateBatch(
            (
                PipelineCandidate(
                    name="浅草橋 焼肉はな",
                    canonical_url=candidate_url,
                    evidence_url=candidate_url,
                    provenance=CandidateProvenance.WEB_SEARCH,
                    is_verified=False,
                    verification_reason="web search result",
                ),
            )
        )

    async def structured_candidates(
        _db: Session,
        _message_id: str,
        url: str,
    ) -> list[PipelineCandidate]:
        assert url == candidate_url
        return [
            PipelineCandidate(
                name="浅草橋 焼肉はな",
                area="浅草橋",
                category="焼肉",
                address="東京都台東区浅草橋1-2-3",
                phone="0312345678",
                canonical_url=candidate_url,
                evidence_url=candidate_url,
                provenance=CandidateProvenance.STRUCTURED_DATA,
                is_verified=True,
                verification_reason="server-fetched JSON-LD",
            )
        ]

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    monkeypatch.setattr(identification_pipeline, "_web_candidates", web_candidates)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        structured_candidates,
    )
    db = db_factory()
    try:
        result = asyncio.run(identification_pipeline.process_message(db, envelope()))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()
        shop = db.query(Shop).one()

        assert result.shop_ids == (shop.id,)
        assert result.pending_mention_ids == ()
        assert shop.shop_name == "浅草橋 焼肉はな"
        assert shop.area == "浅草橋"
        assert shop.category == "焼肉"
        assert shop.address == "東京都台東区浅草橋1-2-3"
        assert shop.phone == "0312345678"
        assert shop.canonical_url == candidate_url
        assert mention.resolution_method == "automatic"
        assert mention.candidates[0].provenance == CandidateProvenance.STRUCTURED_DATA.value
        assert mention.candidates[0].is_verified is True
        assert mention.candidates[0].is_strong_match is True
    finally:
        db.close()


def test_image_only_multiple_images_never_auto_resolve(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def image_clues(
        mention: ExtractedMention | None,
        image_urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        assert mention is None
        assert image_urls == (IMAGE_URL,)
        return image_analysis_result(
            names=["浅草橋 焼肉はな"],
            phones=["03-1234-5678"],
            reason="1枚目のレシートに店名と電話番号が見える",
        )

    async def unexpected_web_lookup(*_args: object) -> CandidateBatch:
        raise AssertionError("multiple images must block automatic search")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image_clues)
    monkeypatch.setattr(identification_pipeline, "_web_candidates", unexpected_web_lookup)
    db = db_factory()
    try:
        add_approved_identity_shop(
            db,
            message_id=LEGACY_MESSAGE_ID,
            shop_name="浅草橋 焼肉はな",
            phone="0312345678",
        )
        message_envelope = envelope().model_copy(
            update={
                "assets": (
                    SourceAssetInput(kind="link", url=SOURCE_URL),
                    SourceAssetInput(kind="image", url=IMAGE_URL, mime_type="image/jpeg"),
                    SourceAssetInput(
                        kind="image",
                        url=SECOND_IMAGE_URL,
                        mime_type="image/jpeg",
                    ),
                )
            }
        )

        result = asyncio.run(
            identification_pipeline.process_message(db, message_envelope)
        )
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert db.query(Shop).count() == 1
        assert "複数画像" in (mention.confidence_reason or "")
        assert mention.candidates[0].is_verified is False
    finally:
        db.close()


def test_multiple_mentions_never_use_shared_image_evidence(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        first = known_category_extraction().message.mentions[0]
        second = first.model_copy(
            update={"shop_name": "横浜 焼肉つき", "area": "横浜"}
        )
        return ExtractionCallResult(
            message=ExtractedMessage(
                is_restaurant_message=True,
                ignore_reason=None,
                unresolved_reason=None,
                mentions=[first, second],
            ),
            metrics=metrics(),
        )

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    async def unexpected_image_lookup(*_args: object) -> ImageAnalysisResult:
        raise AssertionError("one image cannot be assigned across multiple mentions")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    monkeypatch.setattr(
        identification_pipeline,
        "analyze_restaurant_images",
        unexpected_image_lookup,
    )
    db = db_factory()
    try:
        result = asyncio.run(identification_pipeline.process_message(db, envelope()))

        assert result.shop_ids == ()
        assert len(result.pending_mention_ids) == 2
        assert db.query(Shop).count() == 0
    finally:
        db.close()


def test_shared_structured_data_cannot_select_a_different_existing_shop(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return [
            PipelineCandidate(
                name="銀座 鮨はな 本店",
                area="銀座",
                category="寿司・回転寿司",
                phone="03-1234-5678",
                canonical_url="https://official.example/sushi-hana",
                provenance=CandidateProvenance.STRUCTURED_DATA,
                is_verified=True,
                verification_reason="server-fetched JSON-LD",
            )
        ]

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    db = db_factory()
    try:
        existing_shop = add_approved_shop(db)
        existing_shop.phone = "03-1234-5678"
        db.commit()
        message_envelope = envelope().model_copy(
            update={"assets": (SourceAssetInput(kind="link", url=SOURCE_URL),)}
        )

        result = asyncio.run(identification_pipeline.process_message(db, message_envelope))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert mention.shop_id is None
        assert db.query(Shop).count() == 1
    finally:
        db.close()


def test_unapproved_exact_shop_blocks_automatic_adoption_and_new_shop(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def unexpected_lookup(*_args: object) -> list[PipelineCandidate]:
        raise AssertionError("lookup must stop at the unapproved identity collision")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        unexpected_lookup,
    )
    db = db_factory()
    try:
        pending_message = Message(
            message_id=LEGACY_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content="未承認の既存投稿",
            processing_status="succeeded",
        )
        pending_shop = Shop(
            shop_name="浅草橋 焼肉はな",
            area="浅草橋",
            category="焼肉",
        )
        db.add(
            ShopMention(
                message=pending_message,
                shop=pending_shop,
                occurrence_index=0,
                extracted_name=pending_shop.shop_name,
                extracted_area=pending_shop.area,
                extracted_category=pending_shop.category,
                resolution_status="ambiguous",
                review_status="pending",
                extraction_source="legacy_import",
            )
        )
        db.commit()

        result = asyncio.run(identification_pipeline.process_message(db, envelope()))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert mention.shop_id is None
        assert db.query(Shop).count() == 1
        assert "未承認店舗" in (mention.confidence_reason or "")
    finally:
        db.close()


def test_unapproved_external_id_blocks_duplicate_shop_creation(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_url = "https://tabelog.com/tokyo/A1301/A130101/13000001/"

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def unexpected_lookup(*_args: object) -> list[PipelineCandidate]:
        raise AssertionError("lookup must stop at the external identity collision")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        unexpected_lookup,
    )
    db = db_factory()
    try:
        pending_shop = Shop(
            shop_name="別の表示名",
            area="新宿",
            category="焼肉",
            canonical_url=canonical_url,
            external_source="tabelog",
            external_id="13000001",
        )
        db.add(pending_shop)
        db.commit()
        message_envelope = envelope().model_copy(
            update={"assets": (SourceAssetInput(kind="link", url=canonical_url),)}
        )

        result = asyncio.run(identification_pipeline.process_message(db, message_envelope))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert mention.shop_id is None
        assert db.query(Shop).count() == 1
        assert "外部ID" in (mention.confidence_reason or "")
    finally:
        db.close()


@pytest.mark.parametrize(
    ("external_source", "external_id", "existing_url"),
    [
        ("食べログ", "13000002", None),
        (
            None,
            None,
            "https://tabelog.com/tokyo/A1301/A130101/13000002/",
        ),
    ],
)
def test_posted_external_id_conflict_blocks_exact_legacy_shop_adoption(
    db_factory: sessionmaker[Session],
    external_source: str | None,
    external_id: str | None,
    existing_url: str | None,
) -> None:
    posted_url = "https://tabelog.com/tokyo/A1301/A130101/13000001/"
    extracted = known_category_extraction().message.mentions[0]
    db = db_factory()
    try:
        message = Message(
            message_id=LEGACY_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content="承認済みの既存投稿",
            processing_status="succeeded",
        )
        shop = Shop(
            shop_name=extracted.shop_name,
            area=extracted.area,
            category=extracted.category,
            canonical_url=existing_url,
            external_source=external_source,
            external_id=external_id,
        )
        db.add(
            ShopMention(
                message=message,
                shop=shop,
                occurrence_index=0,
                extracted_name=extracted.shop_name,
                extracted_area=extracted.area,
                extracted_category=extracted.category,
                resolution_status="resolved",
                review_status="approved",
                resolution_method="manual",
                extraction_source="legacy_import",
            )
        )
        db.commit()

        selected, reason = identification_pipeline._find_existing_shop_from_mention(
            db,
            extracted,
            posted_url,
        )

        assert selected is None
        assert reason == "投稿URLの外部IDが店名・支店名・エリア一致の既存店舗と競合"
    finally:
        db.close()


def test_posted_external_id_accepts_legacy_source_alias_with_same_id(
    db_factory: sessionmaker[Session],
) -> None:
    posted_url = "https://tabelog.com/tokyo/A1301/A130101/13000001/"
    extracted = known_category_extraction().message.mentions[0]
    db = db_factory()
    try:
        message = Message(
            message_id=LEGACY_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content="承認済みの既存投稿",
            processing_status="succeeded",
        )
        shop = Shop(
            shop_name=extracted.shop_name,
            area=extracted.area,
            category=extracted.category,
            external_source="食べログ",
            external_id="13000001",
        )
        db.add(
            ShopMention(
                message=message,
                shop=shop,
                occurrence_index=0,
                extracted_name=extracted.shop_name,
                extracted_area=extracted.area,
                extracted_category=extracted.category,
                resolution_status="resolved",
                review_status="approved",
                resolution_method="manual",
                extraction_source="legacy_import",
            )
        )
        db.commit()

        selected, reason = identification_pipeline._find_existing_shop_from_mention(
            db,
            extracted,
            posted_url,
        )

        assert selected is not None
        assert selected.id == shop.id
        assert reason == "投稿URLの外部IDと店名・支店名・エリアが既存店舗と一致"
    finally:
        db.close()


def test_general_canonical_url_blocks_duplicate_shop_creation(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        existing = Shop(
            shop_name="既存の別表記",
            area="新宿",
            canonical_url="https://official.example/shared-shop/",
        )
        db.add(existing)
        db.commit()
        candidate = PipelineCandidate(
            name="浅草橋 焼肉はな",
            area="浅草橋",
            canonical_url="https://official.example/shared-shop",
            provenance=CandidateProvenance.WEB_SEARCH,
            is_verified=False,
            verification_reason="web search result",
        )

        selected, reason = identification_pipeline._guard_new_shop_creation(
            db,
            known_category_extraction().message.mentions[0],
            candidate,
            None,
        )

        assert selected is None
        assert reason == "新規候補のcanonical URLまたは根拠URLが既存店舗と競合"
    finally:
        db.close()


def test_evidence_url_blocks_duplicate_shop_creation(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        db.add(
            Shop(
                shop_name="既存の別表記",
                area="新宿",
                canonical_url="https://official.example/shared-shop",
            )
        )
        db.commit()
        candidate = PipelineCandidate(
            name="浅草橋 焼肉はな",
            area="浅草橋",
            evidence_url="https://official.example/shared-shop/",
            provenance=CandidateProvenance.WEB_SEARCH,
            is_verified=False,
            verification_reason="web search result",
        )

        selected, reason = identification_pipeline._guard_new_shop_creation(
            db,
            known_category_extraction().message.mentions[0],
            candidate,
            None,
        )

        assert selected is None
        assert reason == "新規候補のcanonical URLまたは根拠URLが既存店舗と競合"
        assert db.query(Shop).count() == 1
    finally:
        db.close()


def test_external_source_alias_blocks_duplicate_shop_creation(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        db.add(
            Shop(
                shop_name="既存の別表記",
                area="新宿",
                external_source="食べログ",
                external_id="ABC123",
            )
        )
        db.commit()
        candidate = PipelineCandidate(
            name="浅草橋 焼肉はな",
            area="浅草橋",
            external_source="Tabelog.com",
            external_id="abc123",
            evidence_url="https://directory.example/asakusabashi-hana",
            provenance=CandidateProvenance.WEB_SEARCH,
            is_verified=False,
            verification_reason="web search result",
        )

        selected, reason = identification_pipeline._guard_new_shop_creation(
            db,
            known_category_extraction().message.mentions[0],
            candidate,
            None,
        )

        assert selected is None
        assert reason == "新規候補の外部IDが既存店舗と競合"
        assert db.query(Shop).count() == 1
    finally:
        db.close()


def test_candidate_name_and_area_block_duplicate_when_extracted_name_varies(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        existing = Shop(shop_name="浅草橋 焼肉はな", area="浅草橋")
        db.add(existing)
        db.commit()
        mention = known_category_extraction().message.mentions[0].model_copy(
            update={"shop_name": "浅草橋 焼肉はなあ"}
        )
        candidate = PipelineCandidate(
            name="浅草橋 焼肉はな",
            area="浅草橋",
            evidence_url="https://directory.example/yakiniku-hana",
            provenance=CandidateProvenance.WEB_SEARCH,
            is_verified=False,
            verification_reason="web search result",
        )

        selected, reason = identification_pipeline._guard_new_shop_creation(
            db,
            mention,
            candidate,
            None,
        )

        assert selected is None
        assert reason == "新規候補の店名・支店名・エリアが既存店舗と競合"
    finally:
        db.close()


def test_candidate_decision_uses_all_candidates_but_stores_five(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_url = "https://official.example/yakiniku-hana"

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def six_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        unrelated = [
            PipelineCandidate(
                name=f"別の店{i}",
                area="新宿",
                canonical_url=f"https://official.example/other-{i}",
                provenance=CandidateProvenance.STRUCTURED_DATA,
                is_verified=True,
                verification_reason="server-fetched JSON-LD",
            )
            for i in range(5)
        ]
        selected = PipelineCandidate(
            name="浅草橋 焼肉はな",
            area="浅草橋",
            category="焼肉",
            canonical_url=selected_url,
            provenance=CandidateProvenance.STRUCTURED_DATA,
            is_verified=True,
            verification_reason="server-fetched JSON-LD",
        )
        return unrelated + [selected]

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        six_structured_candidates,
    )
    db = db_factory()
    try:
        message_envelope = envelope().model_copy(
            update={"assets": (SourceAssetInput(kind="link", url=SOURCE_URL),)}
        )
        result = asyncio.run(identification_pipeline.process_message(db, message_envelope))
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert len(result.shop_ids) == 1
        assert mention.shop is not None
        assert mention.shop.canonical_url == selected_url
        assert len(mention.candidates) == 5
        assert mention.candidates[0].canonical_url == selected_url
    finally:
        db.close()


def test_broken_url_cache_is_recorded_and_refetched(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://official.example/yakiniku-hana"
    fetched = False

    async def fetch_candidates(_url: str) -> list[CandidateIdentity]:
        nonlocal fetched
        fetched = True
        return [CandidateIdentity(name="浅草橋 焼肉はな", area="浅草橋")]

    monkeypatch.setattr(
        identification_pipeline,
        "fetch_structured_candidates",
        fetch_candidates,
    )
    db = db_factory()
    try:
        identification_pipeline._put_cache(db, "url_metadata", url, "not-json")
        db.commit()

        candidates = asyncio.run(
            identification_pipeline._structured_candidates(db, MESSAGE_ID, url)
        )
        db.commit()
        cached = identification_pipeline._get_cache(db, "url_metadata", url)

        assert fetched is True
        assert [candidate.name for candidate in candidates] == ["浅草橋 焼肉はな"]
        assert cached is not None
        assert cached.payload != "not-json"
        assert (
            db.query(ProcessingRun)
            .filter(
                ProcessingRun.message_id == MESSAGE_ID,
                ProcessingRun.stage == "url_metadata_cache",
                ProcessingRun.status == "failed",
            )
            .count()
            == 1
        )
    finally:
        db.close()


def test_url_cache_preserves_more_than_five_decision_candidates(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://directory.example/restaurant-list"
    fetch_count = 0

    async def fetch_candidates(_url: str) -> list[CandidateIdentity]:
        nonlocal fetch_count
        fetch_count += 1
        return [
            CandidateIdentity(
                name=f"候補店{i}",
                area="銀座",
                canonical_url=f"https://directory.example/shop-{i}",
            )
            for i in range(6)
        ]

    monkeypatch.setattr(
        identification_pipeline,
        "fetch_structured_candidates",
        fetch_candidates,
    )
    db = db_factory()
    try:
        first = asyncio.run(
            identification_pipeline._structured_candidates(db, MESSAGE_ID, url)
        )
        db.commit()
        second = asyncio.run(
            identification_pipeline._structured_candidates(db, MESSAGE_ID, url)
        )

        assert len(first) == 6
        assert len(second) == 6
        assert fetch_count == 1
    finally:
        db.close()


def test_broken_web_cache_is_recorded_and_refetched(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention = known_category_extraction().message.mentions[0]
    query_key = identification_pipeline._web_search_query_key(mention)
    fetched = False

    async def search_candidates(_mention: ExtractedMention) -> CandidateSearchResult:
        nonlocal fetched
        fetched = True
        return CandidateSearchResult(
            candidates=SearchCandidateSet(
                candidates=[
                    SearchCandidate(
                        name="浅草橋 焼肉はな",
                        area="浅草橋",
                        evidence_url=(
                            "https://directory.example/asakusabashi-yakiniku-hana"
                        ),
                        confidence_reason="店名とエリアが一致",
                    )
                ]
            ),
            metrics=metrics(web_search_calls=1),
            source_urls=("https://directory.example/asakusabashi-yakiniku-hana",),
        )

    monkeypatch.setattr(
        identification_pipeline,
        "search_restaurant_candidates",
        search_candidates,
    )
    db = db_factory()
    try:
        identification_pipeline._put_cache(db, "web_search", query_key, "not-json")
        db.commit()

        batch = asyncio.run(
            identification_pipeline._web_candidates(db, MESSAGE_ID, mention)
        )
        db.commit()
        cached = identification_pipeline._get_cache(db, "web_search", query_key)

        assert fetched is True
        assert [candidate.name for candidate in batch.candidates] == ["浅草橋 焼肉はな"]
        assert cached is not None
        assert cached.payload != "not-json"
        assert (
            db.query(ProcessingRun)
            .filter(
                ProcessingRun.message_id == MESSAGE_ID,
                ProcessingRun.stage == "candidate_search_cache",
                ProcessingRun.status == "failed",
            )
            .count()
            == 1
        )
    finally:
        db.close()


def test_web_candidate_sanitizes_each_url_and_external_id_before_cache(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention = known_category_extraction().message.mentions[0]
    cited_url = "https://tabelog.com/tokyo/A1302/A130204/13000001/dtlmenu/"
    uncited_url = "https://tabelog.com/tokyo/A1302/A130204/13000002/"
    search_calls = 0

    async def search_candidates(_mention: ExtractedMention) -> CandidateSearchResult:
        nonlocal search_calls
        search_calls += 1
        return CandidateSearchResult(
            candidates=SearchCandidateSet(
                candidates=[
                    SearchCandidate(
                        name=mention.shop_name,
                        area=mention.area,
                        canonical_url=uncited_url,
                        evidence_url=cited_url,
                        external_source="tabelog",
                        external_id="13000002",
                        confidence_reason="店名とエリアが一致",
                    )
                ]
            ),
            metrics=metrics(web_search_calls=1),
            source_urls=(
                "https://tabelog.com/tokyo/A1302/A130204/13000001/",
            ),
        )

    monkeypatch.setattr(
        identification_pipeline,
        "search_restaurant_candidates",
        search_candidates,
    )
    db = db_factory()
    try:
        query_key = identification_pipeline._web_search_query_key(mention)
        old_query_key = query_key.replace(
            identification_pipeline.CANDIDATE_SEARCH_PROMPT_VERSION,
            "candidate-search-v4",
            1,
        )
        unsafe = SearchCandidateSet(
            candidates=[
                SearchCandidate(
                    name=mention.shop_name,
                    area=mention.area,
                    canonical_url=uncited_url,
                    evidence_url=cited_url,
                    external_source="tabelog",
                    external_id="13000002",
                    confidence_reason="old cache",
                )
            ]
        )
        identification_pipeline._put_cache(
            db,
            "web_search",
            old_query_key,
            unsafe.model_dump_json(),
        )
        db.commit()

        batch = asyncio.run(
            identification_pipeline._web_candidates(db, MESSAGE_ID, mention)
        )
        db.commit()
        cached = identification_pipeline._get_cache(db, "web_search", query_key)

        assert search_calls == 1
        assert len(batch.candidates) == 1
        assert batch.candidates[0].canonical_url is None
        assert batch.candidates[0].evidence_url == cited_url
        assert batch.candidates[0].external_source == "tabelog"
        assert batch.candidates[0].external_id == "13000001"
        assert cached is not None
        cached_set = SearchCandidateSet.model_validate_json(cached.payload)
        assert cached_set.candidates[0].canonical_url is None
        assert cached_set.candidates[0].evidence_url == cited_url
        assert cached_set.candidates[0].external_id == "13000001"
    finally:
        db.close()


def test_candidate_search_without_web_call_is_rejected_and_not_cached(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention = known_category_extraction().message.mentions[0]

    async def search_without_tool(
        _mention: ExtractedMention,
    ) -> CandidateSearchResult:
        return CandidateSearchResult(
            candidates=SearchCandidateSet(candidates=[]),
            metrics=metrics(web_search_calls=0),
        )

    monkeypatch.setattr(
        identification_pipeline,
        "search_restaurant_candidates",
        search_without_tool,
    )
    db = db_factory()
    try:
        with pytest.raises(
            ExtractionError,
            match="must use one web search per API attempt",
        ):
            asyncio.run(
                identification_pipeline._web_candidates(db, MESSAGE_ID, mention)
            )

        query_key = identification_pipeline._web_search_query_key(mention)
        assert identification_pipeline._get_cache(db, "web_search", query_key) is None
        assert (
            db.query(ProcessingRun)
            .filter_by(stage="candidate_search_validation", status="failed")
            .count()
            == 1
        )
    finally:
        db.close()


def test_retry_attempts_are_counted_as_separate_model_calls(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        db.add(
            Message(
                message_id=MESSAGE_ID,
                channel_id=CHANNEL_ID,
                content="retry metrics",
                processing_status="processing",
            )
        )
        db.flush()
        identification_pipeline._record_metrics(
            db,
            MESSAGE_ID,
            "candidate_search",
            ModelCallMetrics(
                model="test-model",
                input_tokens=10,
                output_tokens=5,
                web_search_calls=2,
                image_count=0,
                latency_ms=1,
                estimated_cost_microusd=112_345,
                api_attempts=2,
            ),
        )
        rows = tuple(
            db.query(ProcessingRun)
            .filter(ProcessingRun.message_id == MESSAGE_ID)
            .order_by(ProcessingRun.id)
            .all()
        )

        assert len(rows) == 2
        assert sum(row.model is not None for row in rows) == 2
        assert sum(row.web_search_calls for row in rows) == 2
        assert sum(row.estimated_cost_microusd for row in rows) == 112_345
    finally:
        db.close()


def test_broken_web_cache_stays_deleted_when_refetch_fails(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention = known_category_extraction().message.mentions[0]
    query_key = identification_pipeline._web_search_query_key(mention)

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def failing_search(_mention: ExtractedMention) -> CandidateSearchResult:
        raise ExtractionError("web refetch failed")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(
        identification_pipeline,
        "search_restaurant_candidates",
        failing_search,
    )
    db = db_factory()
    try:
        identification_pipeline._put_cache(db, "web_search", query_key, "not-json")
        db.commit()
        message_envelope = envelope().model_copy(
            update={"assets": (SourceAssetInput(kind="link", url=SOURCE_URL),)}
        )

        with pytest.raises(ExtractionError, match="web refetch failed"):
            asyncio.run(
                identification_pipeline.process_message(db, message_envelope)
            )

        assert identification_pipeline._get_cache(db, "web_search", query_key) is None
        stages = {
            run.stage
            for run in db.query(ProcessingRun)
            .filter(ProcessingRun.message_id == MESSAGE_ID)
            .all()
        }
        assert "candidate_search_cache" in stages
        assert "candidate_search" in stages
    finally:
        db.close()


def test_shared_store_url_is_not_reused_for_multiple_mentions(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_url = "https://tabelog.com/tokyo/A1301/A130101/13000001/"

    async def fake_extract(_content: str) -> ExtractionCallResult:
        first = known_category_extraction().message.mentions[0]
        second = first.model_copy(update={"shop_name": "横浜 焼肉つき"})
        return ExtractionCallResult(
            message=ExtractedMessage(
                is_restaurant_message=True,
                ignore_reason=None,
                unresolved_reason=None,
                mentions=[first, second],
            ),
            metrics=metrics(),
        )

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    message_envelope = envelope().model_copy(
        update={"assets": (SourceAssetInput(kind="link", url=canonical_url),)}
    )
    db = db_factory()
    try:
        result = asyncio.run(identification_pipeline.process_message(db, message_envelope))

        assert result.shop_ids == ()
        assert len(result.pending_mention_ids) == 2
        assert db.query(Shop).count() == 0
        assert db.query(ShopMention).filter(ShopMention.shop_id.is_(None)).count() == 2
    finally:
        db.close()


def test_distinct_store_urls_do_not_auto_resolve_one_mention(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_url = "https://tabelog.com/tokyo/A1311/A131103/13000001/"
    second_url = "https://tabelog.com/tokyo/A1304/A130401/13000002/"

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def structured_candidates(
        _db: Session,
        _message_id: str,
        url: str,
    ) -> list[PipelineCandidate]:
        if url != first_url:
            return []
        return [
            PipelineCandidate(
                name="浅草橋 焼肉はな",
                area="浅草橋",
                category="焼肉",
                address="東京都台東区浅草橋1-2-3",
                canonical_url=first_url,
                evidence_url=first_url,
                provenance=CandidateProvenance.STRUCTURED_DATA,
                is_verified=True,
                verification_reason="server-fetched JSON-LD",
            )
        ]

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    message_envelope = envelope().model_copy(
        update={
            "assets": (
                SourceAssetInput(kind="link", url=first_url),
                SourceAssetInput(kind="link", url=second_url),
            )
        }
    )
    db = db_factory()
    try:
        result = asyncio.run(
            identification_pipeline.process_message(db, message_envelope)
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert db.query(Shop).count() == 0
        assert "複数の店舗ID付きURL" in (mention.confidence_reason or "")
    finally:
        db.close()


def _source_web_candidate(
    *,
    name: str = "abcdefghxy",
    area: str = "銀座",
    address: str = "〒104-0061 東京都中央区銀座1-2-3",
    evidence_url: str = "https://directory.example/ginza-hana",
) -> PipelineCandidate:
    return PipelineCandidate(
        name=name,
        area=area,
        category="寿司・回転寿司",
        address=address,
        canonical_url=evidence_url,
        evidence_url=evidence_url,
        provenance=CandidateProvenance.WEB_SEARCH,
        is_verified=False,
        verification_reason="web search result",
    )


def test_source_discovery_is_disabled_without_explicit_opt_in(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def unexpected_discovery(_content: str) -> ExtractionCallResult:
        raise AssertionError("source discovery requires explicit opt in")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "discover_restaurant_mentions",
        unexpected_discovery,
    )
    db = db_factory()
    try:
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                allow_image_fallback=False,
            )
        )

        assert result.ignored is False
        assert len(result.pending_mention_ids) == 1
        assert db.query(ProcessingRun).filter_by(stage="source_discovery").count() == 0
    finally:
        db.close()


def test_public_message_processing_enables_source_discovery_explicitly(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_calls = 0

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def discover(_content: str) -> ExtractionCallResult:
        nonlocal discovery_calls
        discovery_calls += 1
        return source_discovery_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "discover_restaurant_mentions", discover)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    db = db_factory()
    try:
        result = asyncio.run(
            identification_pipeline.process_message(
                db,
                envelope(),
                allow_source_discovery=True,
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert discovery_calls == 1
        assert result.ignored is False
        assert mention.extraction_source == "responses_source_discovery"
    finally:
        db.close()


def test_fresh_mentions_take_precedence_over_source_discovery(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return known_category_extraction()

    async def unexpected_discovery(_content: str) -> ExtractionCallResult:
        raise AssertionError("fresh mentions must skip source discovery")

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "discover_restaurant_mentions",
        unexpected_discovery,
    )
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    db = db_factory()
    try:
        asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                allow_image_fallback=False,
                allow_source_discovery=True,
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert mention.extraction_source == "responses_structured"
        assert mention.extracted_name == "浅草橋 焼肉はな"
    finally:
        db.close()


def test_fallback_mentions_take_precedence_over_source_discovery(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_hint = known_category_extraction().message.mentions[0]

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def unexpected_discovery(_content: str) -> ExtractionCallResult:
        raise AssertionError("fallback mentions must skip source discovery")

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        mention: ExtractedMention,
    ) -> CandidateBatch:
        assert mention.shop_name == legacy_hint.shop_name
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "discover_restaurant_mentions",
        unexpected_discovery,
    )
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    db = db_factory()
    try:
        asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                allow_image_fallback=False,
                allow_source_discovery=True,
                fallback_mentions=(legacy_hint,),
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert mention.extraction_source == "legacy_hint"
        assert mention.extracted_name == legacy_hint.shop_name
    finally:
        db.close()


@pytest.mark.parametrize(
    ("asset", "expected_calls"),
    [
        (SourceAssetInput(kind="link", url=SOURCE_URL), 1),
        (
            SourceAssetInput(
                kind="embed",
                url=SOURCE_URL,
                title="店舗紹介",
            ),
            1,
        ),
        (SourceAssetInput(kind="image", url=IMAGE_URL, mime_type="image/jpeg"), 0),
        (
            SourceAssetInput(
                kind="attachment",
                url="https://cdn.discordapp.com/attachments/1/2/menu.pdf",
                mime_type="application/pdf",
            ),
            0,
        ),
    ],
)
def test_source_discovery_uses_only_link_and_embed_assets(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    asset: SourceAssetInput,
    expected_calls: int,
) -> None:
    discovery_calls = 0

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def unresolved_discovery(_content: str) -> ExtractionCallResult:
        nonlocal discovery_calls
        discovery_calls += 1
        return ExtractionCallResult(
            message=ExtractedMessage(
                is_restaurant_message=True,
                ignore_reason=None,
                unresolved_reason="出典を検索しても店舗名を特定できない",
                mentions=[],
            ),
            metrics=metrics(web_search_calls=1),
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "discover_restaurant_mentions",
        unresolved_discovery,
    )
    db = db_factory()
    try:
        message_envelope = envelope().model_copy(update={"assets": (asset,)})
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                message_envelope,
                allow_image_fallback=False,
                allow_source_discovery=True,
            )
        )

        assert result.ignored is False
        assert discovery_calls == expected_calls
    finally:
        db.close()


def test_ignored_message_source_discovery_uses_independent_candidate_search(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_calls = 0

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return ignored_extraction()

    async def discover(_content: str) -> ExtractionCallResult:
        return source_discovery_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def web_candidates(
        _db: Session,
        _message_id: str,
        mention: ExtractedMention,
    ) -> CandidateBatch:
        nonlocal candidate_calls
        candidate_calls += 1
        assert mention.shop_name == "abcdefghij"
        assert mention.needs_review is True
        return CandidateBatch((_source_web_candidate(name="abcdefghij"),))

    async def unexpected_image(*_args: object) -> ImageAnalysisResult:
        raise AssertionError("source discovery must not use image resolution")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "discover_restaurant_mentions", discover)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", web_candidates)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", unexpected_image)
    install_verified_page(
        monkeypatch,
        name="abcdefghij",
        address="〒104-0061 東京都中央区銀座1-2-3",
        url="https://directory.example/ginza-hana",
    )
    db = db_factory()
    try:
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                allow_image_fallback=True,
                allow_source_discovery=True,
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()
        shop = db.query(Shop).one()

        assert result.ignored is False
        assert result.shop_ids == (shop.id,)
        assert candidate_calls == 1
        assert mention.extraction_source == "responses_source_discovery"
        assert mention.review_status == "approved"
        assert shop.shop_name == "abcdefghij"
        assert shop.area == "銀座"
    finally:
        db.close()


def test_source_discovery_does_not_resolve_from_original_structured_data_only(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_search_calls = 0

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def discover(_content: str) -> ExtractionCallResult:
        return source_discovery_extraction()

    async def source_structured_candidate(
        _db: Session,
        _message_id: str,
        url: str,
    ) -> list[PipelineCandidate]:
        assert url == SOURCE_URL
        return [
            PipelineCandidate(
                name="abcdefghij",
                area="銀座",
                category="寿司・回転寿司",
                address="東京都中央区銀座1-2-3",
                canonical_url=SOURCE_URL,
                evidence_url=SOURCE_URL,
                provenance=CandidateProvenance.STRUCTURED_DATA,
                is_verified=True,
                verification_reason="server-fetched JSON-LD",
            )
        ]

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        nonlocal candidate_search_calls
        candidate_search_calls += 1
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "discover_restaurant_mentions", discover)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        source_structured_candidate,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    db = db_factory()
    try:
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope().model_copy(
                    update={
                        "assets": (
                            SourceAssetInput(
                                kind="link",
                                url=SOURCE_URL,
                                title="abcdefghij 銀座",
                            ),
                        )
                    }
                ),
                allow_image_fallback=False,
                allow_source_discovery=True,
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert candidate_search_calls == 1
        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert db.query(Shop).count() == 0
    finally:
        db.close()


def test_source_discovery_links_unique_existing_shop_after_web_confirmation(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def discover(_content: str) -> ExtractionCallResult:
        return source_discovery_extraction(shop_name="abcdefghxy")

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch((_source_web_candidate(),))

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "discover_restaurant_mentions", discover)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", web_candidates)
    install_verified_page(
        monkeypatch,
        name="abcdefghxy",
        address="〒104-0061 東京都中央区銀座1-2-3",
        url="https://directory.example/ginza-hana",
    )
    db = db_factory()
    try:
        existing_message = Message(
            message_id=LEGACY_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content="既存店舗",
            processing_status="succeeded",
        )
        existing_shop = Shop(
            shop_name="abcdefghxy",
            area="銀座",
            category="寿司・回転寿司",
        )
        db.add(
            ShopMention(
                message=existing_message,
                shop=existing_shop,
                occurrence_index=0,
                extracted_name="abcdefghxy",
                extracted_area="銀座",
                extracted_category="寿司・回転寿司",
                resolution_status="resolved",
                review_status="approved",
                metadata_review_status="approved",
                resolution_method="manual",
                extraction_source="legacy_import",
            )
        )
        db.commit()

        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                allow_image_fallback=False,
                allow_source_discovery=True,
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert result.shop_ids == (existing_shop.id,)
        assert mention.shop_id == existing_shop.id
        assert mention.review_status == "approved"
        assert db.query(Shop).count() == 1
    finally:
        db.close()


def test_source_discovery_keeps_mention_branch_when_matching_existing_shop(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        existing_message = Message(
            message_id=LEGACY_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content="existing",
            processing_status="succeeded",
        )
        existing_shop = Shop(
            shop_name="Cafe Alpha",
            branch_name="西新宿",
            area="西新宿",
            category="カフェ・喫茶店",
        )
        db.add(
            ShopMention(
                message=existing_message,
                shop=existing_shop,
                occurrence_index=0,
                extracted_name="Cafe Alpha",
                extracted_branch_name="西新宿",
                extracted_area="西新宿",
                extracted_category="カフェ・喫茶店",
                resolution_status="resolved",
                review_status="approved",
                metadata_review_status="approved",
                resolution_method="manual",
                extraction_source="legacy_import",
            )
        )
        db.commit()
        mention = ExtractedMention(
            shop_name="Cafe Alpha",
            branch_name="西新宿",
            area="西新宿",
            category="カフェ・喫茶店",
            source_url=SOURCE_URL,
            needs_review=True,
            confidence_reason="source",
        )
        candidate = PipelineCandidate(
            name="Cafe Alpha 西新宿",
            area="西新宿",
            category="カフェ・喫茶店",
            address="東京都新宿区西新宿1-2-3",
            canonical_url="https://directory.example/cafe-alpha-nishi-shinjuku",
            evidence_url="https://directory.example/cafe-alpha-nishi-shinjuku",
            provenance=CandidateProvenance.WEB_SEARCH,
            is_verified=True,
            verification_reason="source_verified=true",
        )

        matched, reason = (
            identification_pipeline._find_existing_shop_after_candidate_confirmation(
                db,
                mention,
                candidate,
            )
        )

        assert matched == existing_shop
        assert reason == "店名・支店名・エリアが承認済み既存店舗と一意に一致"
    finally:
        db.close()


def test_source_discovery_keeps_web_candidate_after_structured_verification(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_url = "https://directory.example/cafe-alpha"

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def discover(_content: str) -> ExtractionCallResult:
        return source_discovery_extraction(shop_name="Cafe Alpha")

    async def structured_candidates(
        _db: Session,
        _message_id: str,
        url: str,
    ) -> list[PipelineCandidate]:
        if url != candidate_url:
            return []
        return [
            PipelineCandidate(
                name="Cafe Alpha",
                area="銀座",
                category="カフェ・喫茶店",
                address="東京都中央区銀座1-2-3",
                canonical_url=candidate_url,
                evidence_url=candidate_url,
                provenance=CandidateProvenance.STRUCTURED_DATA,
                is_verified=True,
                verification_reason="server-fetched JSON-LD",
            )
        ]

    async def web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(
            (
                _source_web_candidate(
                    name="Cafe Alpha",
                    evidence_url=candidate_url,
                ),
            )
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "discover_restaurant_mentions", discover)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", web_candidates)
    db = db_factory()
    try:
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                allow_image_fallback=False,
                allow_source_discovery=True,
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert result.shop_ids == (mention.shop_id,)
        assert result.pending_mention_ids == ()
        assert mention.review_status == "approved"
        assert {row.provenance for row in mention.candidates} == {
            CandidateProvenance.STRUCTURED_DATA.value,
            CandidateProvenance.WEB_SEARCH.value,
        }
    finally:
        db.close()


def test_source_discovery_rejects_web_and_structured_address_conflict() -> None:
    candidate_url = "https://directory.example/cafe-alpha"
    mention = source_discovery_extraction(
        shop_name="Cafe Alpha"
    ).message.mentions[0]
    web_candidate = _source_web_candidate(
        name="Cafe Alpha",
        evidence_url=candidate_url,
    )
    structured_candidate = PipelineCandidate(
        name="Cafe Alpha",
        area="新宿",
        category="カフェ・喫茶店",
        address="東京都新宿区新宿1-2-3",
        canonical_url=candidate_url,
        evidence_url=candidate_url,
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
    )

    assert identification_pipeline._top_web_candidate_for_new_shop(
        mention,
        [web_candidate, structured_candidate],
        require_precise_area_evidence=True,
    ) is None


def test_source_discovery_rejects_structured_branch_missing_from_source() -> None:
    candidate_url = "https://directory.example/cafe-alpha"
    mention = source_discovery_extraction(
        shop_name="Cafe Alpha"
    ).message.mentions[0]
    web_candidate = _source_web_candidate(
        name="Cafe Alpha",
        evidence_url=candidate_url,
    )
    structured_candidate = PipelineCandidate(
        name="Cafe Alpha 銀座",
        area="銀座",
        category="カフェ・喫茶店",
        address="東京都中央区銀座1-2-3",
        canonical_url=candidate_url,
        evidence_url=candidate_url,
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
    )

    assert identification_pipeline._top_web_candidate_for_new_shop(
        mention,
        [web_candidate, structured_candidate],
        require_precise_area_evidence=True,
    ) is None


def test_source_discovery_rejects_branch_conflict_from_original_source_data() -> None:
    mention = ExtractedMention(
        shop_name="スターバックス",
        branch_name="新宿店",
        area="新宿",
        category="カフェ・喫茶店",
        needs_review=True,
        confidence_reason="出典に店名と支店名がある",
    )
    web_candidate = PipelineCandidate(
        name="スターバックス 新宿店",
        area="新宿",
        category="カフェ・喫茶店",
        address="東京都新宿区新宿1-2-3",
        canonical_url="https://directory.example/starbucks-shinjuku",
        evidence_url="https://directory.example/starbucks-shinjuku",
        provenance=CandidateProvenance.WEB_SEARCH,
        is_verified=False,
        verification_reason="web search result",
    )
    source_structured = PipelineCandidate(
        name="スターバックス 渋谷店",
        area="渋谷",
        category="カフェ・喫茶店",
        address="東京都渋谷区渋谷1-2-3",
        canonical_url=SOURCE_URL,
        evidence_url=SOURCE_URL,
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="source JSON-LD",
    )

    assert identification_pipeline._top_web_candidate_for_new_shop(
        mention,
        [web_candidate, source_structured],
        require_precise_area_evidence=True,
        excluded_evidence_urls=(SOURCE_URL,),
    ) is None


def test_source_discovery_rejects_different_shop_from_original_source_data() -> None:
    mention = source_discovery_extraction(
        shop_name="Cafe Alpha",
    ).message.mentions[0]
    web_candidate = _source_web_candidate(
        name="Cafe Alpha",
        area="銀座",
        address="東京都中央区銀座1-2-3",
        evidence_url="https://directory.example/cafe-alpha",
    )
    source_structured = PipelineCandidate(
        name="Sushi Beta",
        area="渋谷",
        category="寿司・回転寿司",
        address="東京都渋谷区渋谷1-2-3",
        canonical_url=SOURCE_URL,
        evidence_url=SOURCE_URL,
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="source JSON-LD",
    )

    assert identification_pipeline._top_web_candidate_for_new_shop(
        mention,
        [web_candidate, source_structured],
        require_precise_area_evidence=True,
        excluded_evidence_urls=(SOURCE_URL,),
    ) is None


def test_source_discovery_does_not_merge_by_unconfirmed_discovery_name(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def discover(_content: str) -> ExtractionCallResult:
        return source_discovery_extraction(shop_name="Cafe Alpha")

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(
            (
                _source_web_candidate(
                    name="Cafe Alphaa",
                    address="東京都中央区銀座1-2-3",
                ),
            )
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "discover_restaurant_mentions", discover)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", web_candidates)
    db = db_factory()
    try:
        existing_message = Message(
            message_id=LEGACY_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content="既存店舗",
            processing_status="succeeded",
        )
        existing_shop = Shop(
            shop_name="Cafe Alpha",
            area="銀座",
            category="カフェ・喫茶店",
        )
        db.add(
            ShopMention(
                message=existing_message,
                shop=existing_shop,
                occurrence_index=0,
                extracted_name="Cafe Alpha",
                extracted_area="銀座",
                extracted_category="カフェ・喫茶店",
                resolution_status="resolved",
                review_status="approved",
                metadata_review_status="approved",
                resolution_method="manual",
                extraction_source="legacy_import",
            )
        )
        db.commit()

        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                allow_image_fallback=False,
                allow_source_discovery=True,
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert mention.shop_id is None
        assert mention.review_status == "pending"
        assert db.query(Shop).count() == 1
    finally:
        db.close()


def test_candidate_identity_alias_links_one_approved_existing_shop(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        message = Message(
            message_id=LEGACY_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content="既存店舗",
            processing_status="succeeded",
        )
        shop = Shop(
            shop_name="Cafe Alpha",
            area="銀座",
            category="カフェ・喫茶店",
            external_source="食べログ",
            external_id="ABC123",
        )
        db.add(
            ShopMention(
                message=message,
                shop=shop,
                occurrence_index=0,
                extracted_name=shop.shop_name,
                extracted_area=shop.area,
                extracted_category=shop.category,
                resolution_status="resolved",
                review_status="approved",
                resolution_method="manual",
                extraction_source="legacy_import",
            )
        )
        db.commit()
        mention = source_discovery_extraction(
            shop_name="Cafe Alpha"
        ).message.mentions[0]
        candidate = _source_web_candidate(
            name="Cafe Alpha",
            evidence_url="https://directory.example/cafe-alpha",
        ).model_copy(
            update={
                "external_source": "Tabelog.com",
                "external_id": "abc123",
                "canonical_url": "https://official.example/cafe-alpha",
            }
        )

        selected, reason = (
            identification_pipeline._find_existing_shop_after_candidate_confirmation(
                db,
                mention,
                candidate,
            )
        )

        assert selected is not None
        assert selected.id == shop.id
        assert "外部IDまたはURL" in (reason or "")
    finally:
        db.close()


def test_candidate_evidence_must_not_point_to_two_existing_shops(
    db_factory: sessionmaker[Session],
) -> None:
    db = db_factory()
    try:
        alpha_message = Message(
            message_id=LEGACY_MESSAGE_ID,
            channel_id=CHANNEL_ID,
            content="Alpha",
            processing_status="succeeded",
        )
        beta_message = Message(
            message_id="12345678901234568",
            channel_id=CHANNEL_ID,
            content="Beta",
            processing_status="succeeded",
        )
        alpha = Shop(
            shop_name="Cafe Alpha",
            area="銀座",
            external_source="tabelog",
            external_id="13000001",
        )
        beta = Shop(
            shop_name="Cafe Beta",
            area="新宿",
            canonical_url="https://official.example/cafe-beta",
        )
        db.add_all(
            (
                ShopMention(
                    message=alpha_message,
                    shop=alpha,
                    occurrence_index=0,
                    extracted_name=alpha.shop_name,
                    extracted_area=alpha.area,
                    resolution_status="resolved",
                    review_status="approved",
                    resolution_method="manual",
                    extraction_source="legacy_import",
                ),
                ShopMention(
                    message=beta_message,
                    shop=beta,
                    occurrence_index=0,
                    extracted_name=beta.shop_name,
                    extracted_area=beta.area,
                    resolution_status="resolved",
                    review_status="approved",
                    resolution_method="manual",
                    extraction_source="legacy_import",
                ),
            )
        )
        db.commit()
        mention = source_discovery_extraction(
            shop_name="Cafe Alpha"
        ).message.mentions[0]
        candidate = _source_web_candidate(
            name="Cafe Alpha",
            evidence_url="https://tabelog.com/tokyo/A1301/A130101/13000001/",
        ).model_copy(
            update={
                "external_source": "tabelog",
                "external_id": "13000001",
                "canonical_url": "https://official.example/cafe-beta",
            }
        )

        selected, reason = (
            identification_pipeline._find_existing_shop_after_candidate_confirmation(
                db,
                mention,
                candidate,
            )
        )

        assert selected is None
        assert reason == "候補の外部IDまたはURLが複数の既存店舗に紐付いている"
    finally:
        db.close()


@pytest.mark.parametrize(
    "candidates",
    [
        (
            _source_web_candidate(
                evidence_url="https://directory-one.example/ginza-hana"
            ),
            _source_web_candidate(
                evidence_url="https://directory-two.example/ginza-hana"
            ),
        ),
        (
            _source_web_candidate(
                name="abcdefghij",
                area="新宿",
                address="東京都新宿区新宿1-2-3",
                evidence_url="https://directory.example/shinjuku-hana",
            ),
        ),
        (
            _source_web_candidate(
                area="銀座",
                address="東京都中央区日本橋1-2-3",
                evidence_url="https://directory.example/nihonbashi-hana",
            ),
        ),
        (
            _source_web_candidate(
                area="銀座",
                address="東京都中央区湊1-2-3 銀座タワー",
                evidence_url="https://directory.example/minato-ginza-tower",
            ),
        ),
        (
            _source_web_candidate(
                address="",
                evidence_url="https://directory.example/addressless-hana",
            ),
        ),
        (
            _source_web_candidate(
                evidence_url=SOURCE_URL,
            ),
        ),
        (
            _source_web_candidate(
                evidence_url="https://directory.example/ginza-hana",
            ).model_copy(
                update={
                    "canonical_url": SOURCE_URL.replace(
                        "https://x.com",
                        "http://www.x.com",
                    ).split("?", 1)[0]
                }
            ),
        ),
        (
            _source_web_candidate(
                evidence_url=SOURCE_URL.replace("x.com", "mobile.twitter.com"),
            ),
        ),
        (
            _source_web_candidate(
                name="abcdefghij",
                evidence_url=SOURCE_URL,
            ),
            _source_web_candidate(
                evidence_url="https://directory.example/ginza-runner-up",
            ),
        ),
    ],
    ids=[
        "tied-top",
        "area-conflict",
        "same-ward-wrong-locality",
        "building-name-false-positive",
        "addressless",
        "same-source-url",
        "same-source-url-variant-in-canonical",
        "same-x-source-alias",
        "same-source-top-does-not-promote-runner-up",
    ],
)
def test_source_discovery_does_not_bypass_candidate_safety_rules(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    candidates: tuple[PipelineCandidate, ...],
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def discover(_content: str) -> ExtractionCallResult:
        return source_discovery_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(candidates)

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(identification_pipeline, "discover_restaurant_mentions", discover)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", web_candidates)
    db = db_factory()
    try:
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                envelope(),
                allow_image_fallback=False,
                allow_source_discovery=True,
            )
        )
        mention = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()

        assert result.shop_ids == ()
        assert result.pending_mention_ids == (mention.id,)
        assert mention.shop_id is None
        assert mention.extraction_source == "responses_source_discovery"
        assert db.query(Shop).count() == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    "candidate_name",
    [
        "スターバックス 新宿店",
        "スターバックス（新宿店）",
        "スターバックス［新宿店］",
    ],
)
def test_web_candidate_branch_requires_matching_source_branch(
    candidate_name: str,
) -> None:
    generic_mention = ExtractedMention(
        shop_name="スターバックス",
        branch_name=None,
        area="新宿",
        category="カフェ・喫茶店",
        needs_review=True,
        confidence_reason="出典に店名と地域がある",
    )
    candidate = PipelineCandidate(
        name=candidate_name,
        area="新宿",
        category="カフェ・喫茶店",
        address="東京都新宿区新宿1-2-3",
        evidence_url="https://directory.example/starbucks-shinjuku",
        provenance=CandidateProvenance.WEB_SEARCH,
        is_verified=False,
    )

    assert identification_pipeline._top_web_candidate_for_new_shop(
        generic_mention,
        [candidate],
        require_precise_area_evidence=True,
    ) is None
    assert identification_pipeline._top_web_candidate_for_new_shop(
        generic_mention.model_copy(update={"branch_name": "新宿店"}),
        [candidate],
        require_precise_area_evidence=True,
    ) is not None
    assert identification_pipeline._top_web_candidate_for_new_shop(
        generic_mention.model_copy(update={"branch_name": "新宿"}),
        [candidate.model_copy(update={"name": "スターバックス 西新宿"})],
        require_precise_area_evidence=True,
    ) is None
    assert identification_pipeline._top_web_candidate_for_new_shop(
        generic_mention.model_copy(
            update={"shop_name": "スターバックス新宿店"}
        ),
        [candidate],
        require_precise_area_evidence=True,
    ) is None


def test_source_discovery_requires_exact_name_without_source_branch() -> None:
    mention = ExtractedMention(
        shop_name="焼肉ホルモンたけ田",
        branch_name=None,
        area="新宿",
        category="焼肉",
        needs_review=True,
        confidence_reason="出典に店名と地域がある",
    )
    candidate = PipelineCandidate(
        name="焼肉ホルモンたけ田 新宿",
        area="新宿",
        category="焼肉",
        address="東京都新宿区新宿1-2-3",
        evidence_url="https://directory.example/takeda-shinjuku",
        provenance=CandidateProvenance.WEB_SEARCH,
        is_verified=False,
    )

    assert name_similarity(mention.shop_name, candidate.name) >= 0.8
    assert identification_pipeline._top_web_candidate_for_new_shop(
        mention,
        [candidate],
        require_precise_area_evidence=True,
    ) is None


def test_web_candidate_rejects_conflicting_ids_from_the_same_service() -> None:
    mention = source_discovery_extraction(
        shop_name="Cafe Alpha"
    ).message.mentions[0]
    candidate = _source_web_candidate(
        name="Cafe Alpha",
        evidence_url="https://tabelog.com/tokyo/A1301/A130101/13000001/",
    ).model_copy(
        update={
            "external_source": "食べログ",
            "external_id": "13000002",
        }
    )

    assert identification_pipeline._top_web_candidate_for_new_shop(
        mention,
        [candidate],
        require_precise_area_evidence=True,
    ) is None


def test_same_external_id_with_conflicting_web_data_is_not_deduplicated() -> None:
    mention = source_discovery_extraction(
        shop_name="Cafe Alpha"
    ).message.mentions[0]
    first = _source_web_candidate(
        name="Cafe Alpha",
        evidence_url="https://tabelog.com/tokyo/A1301/A130101/13000001/",
    ).model_copy(
        update={"external_source": "tabelog", "external_id": "13000001"}
    )
    second = first.model_copy(
        update={
            "name": "Cafe Alpha 新宿店",
            "address": "東京都新宿区新宿1-2-3",
            "canonical_url": (
                "https://tabelog.com/tokyo/A1304/A130401/13000001/dtlmenu/"
            ),
            "evidence_url": (
                "https://tabelog.com/tokyo/A1304/A130401/13000001/dtlmenu/"
            ),
        }
    )
    deduped = identification_pipeline._dedupe_candidates([first, second])

    assert len(deduped) == 2
    assert identification_pipeline._top_web_candidate_for_new_shop(
        mention,
        deduped,
        require_precise_area_evidence=True,
    ) is None


def test_candidate_deduplication_preserves_meaningful_query_identity() -> None:
    mention = source_discovery_extraction(
        shop_name="Cafe Alpha"
    ).message.mentions[0]
    first = _source_web_candidate(
        name="Cafe Alpha",
        evidence_url="https://official.example/shop?id=1&utm_source=search",
    )
    second = _source_web_candidate(
        name="Cafe Alpha",
        evidence_url="https://official.example/shop?id=2",
    )

    deduped = identification_pipeline._dedupe_candidates([first, second])

    assert len(deduped) == 2
    assert identification_pipeline._top_web_candidate_for_new_shop(
        mention,
        deduped,
        require_precise_area_evidence=True,
    ) is None


def test_source_discovery_excludes_source_urls_beyond_search_input_limit() -> None:
    source_urls = tuple(
        f"https://source-{index}.example/post"
        for index in range(1, 5)
    )
    message_envelope = envelope().model_copy(
        update={
            "content": "出典が複数ある投稿",
            "assets": tuple(
                SourceAssetInput(kind="link", url=url)
                for url in source_urls
            ),
        }
    )

    evidence = identification_pipeline._source_discovery_evidence(message_envelope)

    assert evidence is not None
    assert source_urls[3] not in evidence.input_text
    assert evidence.source_urls == source_urls
    assert (
        identification_pipeline._top_web_candidate_for_new_shop(
            source_discovery_extraction().message.mentions[0],
            [
                _source_web_candidate(
                    evidence_url=source_urls[3],
                )
            ],
            require_precise_area_evidence=True,
            excluded_evidence_urls=evidence.source_urls,
        )
        is None
    )


@pytest.mark.parametrize(
    "trailing_punctuation",
    [
        "。",
        "、",
        "）",
        "]",
        "}",
        ";",
        ":",
        "!",
        "?",
        "；",
        "：",
        "！",
        "？",
        "」",
        "』",
        "”",
        "’",
        "］",
        "｝",
    ],
)
def test_source_discovery_strips_trailing_punctuation_from_content_urls(
    trailing_punctuation: str,
) -> None:
    source_url = "https://source.example/shop"
    message_envelope = envelope().model_copy(
        update={
            "content": f"店舗の紹介 {source_url}{trailing_punctuation}",
            "assets": (SourceAssetInput(kind="link", url=SOURCE_URL),),
        }
    )

    evidence = identification_pipeline._source_discovery_evidence(message_envelope)

    assert evidence is not None
    assert source_url in evidence.source_urls
    assert f"{source_url}{trailing_punctuation}" not in evidence.source_urls


def test_source_discovery_excludes_urls_from_embed_metadata() -> None:
    metadata_url = "https://source.example/embedded-shop"
    message_envelope = envelope().model_copy(
        update={
            "content": "埋め込みを参照",
            "assets": (
                SourceAssetInput(
                    kind="embed",
                    url=SOURCE_URL,
                    title=f"店舗情報 {metadata_url}。",
                    description=f"詳細 {metadata_url}?ref=embed」",
                ),
            ),
        }
    )

    evidence = identification_pipeline._source_discovery_evidence(message_envelope)

    assert evidence is not None
    assert metadata_url in evidence.source_urls
    assert f"{metadata_url}?ref=embed" in evidence.source_urls


def test_source_evidence_identity_normalizes_youtube_redirect_variants() -> None:
    assert identification_pipeline._source_evidence_url_identity(
        "https://youtu.be/dQw4w9WgXcQ?t=30"
    ) == identification_pipeline._source_evidence_url_identity(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share"
    )


def test_source_evidence_identity_normalizes_x_web_status_variant() -> None:
    assert identification_pipeline._source_evidence_url_identity(
        "https://twitter.com/i/web/status/1234567890"
    ) == identification_pipeline._source_evidence_url_identity(
        "https://x.com/restaurant/status/1234567890/photo/1"
    )


@pytest.mark.parametrize(
    "wrapper_host",
    ["fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com"],
)
def test_source_evidence_identity_normalizes_x_wrapper_hosts(
    wrapper_host: str,
) -> None:
    assert identification_pipeline._source_evidence_url_identity(
        f"https://{wrapper_host}/restaurant/status/1234567890"
    ) == identification_pipeline._source_evidence_url_identity(
        "https://x.com/restaurant/status/1234567890"
    )


def test_source_evidence_identity_normalizes_tabelog_mobile_host() -> None:
    assert identification_pipeline._source_evidence_url_identity(
        "https://s.tabelog.com/tokyo/A1301/A130101/13234567/"
    ) == identification_pipeline._source_evidence_url_identity(
        "https://tabelog.com/tokyo/A1301/A130101/13234567/?svd=20260829"
    )


@pytest.mark.parametrize(
    ("root_url", "subpage_url"),
    [
        (
            "https://tabelog.com/tokyo/A1301/A130101/13234567/",
            "https://s.tabelog.com/tokyo/A1301/A130101/13234567/dtlmenu/",
        ),
        (
            "https://www.hotpepper.jp/strJ001234567/",
            "https://www.hotpepper.jp/strJ001234567/course/",
        ),
        (
            "https://r.gnavi.co.jp/a123456/",
            "https://r.gnavi.co.jp/a123456/menu1/",
        ),
    ],
)
def test_source_evidence_identity_normalizes_store_subpages(
    root_url: str,
    subpage_url: str,
) -> None:
    assert identification_pipeline._source_evidence_url_identity(
        root_url
    ) == identification_pipeline._source_evidence_url_identity(subpage_url)


def test_citation_identity_preserves_meaningful_query_parameters() -> None:
    first = "https://directory.example/shop?id=A&utm_source=search"
    same = "https://www.directory.example/shop?utm_medium=web&id=A"
    different = "https://directory.example/shop?id=B"

    assert identification_pipeline._citation_url_identity(
        first
    ) == identification_pipeline._citation_url_identity(same)
    assert identification_pipeline._citation_url_identity(
        first
    ) != identification_pipeline._citation_url_identity(different)


def test_candidate_requires_its_url_in_raw_web_sources() -> None:
    candidate = CandidateIdentity(
        name="Cafe Alpha",
        evidence_url="https://directory.example/shop?id=B",
    )

    assert identification_pipeline._candidate_has_cited_web_source(
        candidate,
        ("https://directory.example/shop?id=A",),
    ) is False
    assert identification_pipeline._candidate_has_cited_web_source(
        candidate,
        ("https://directory.example/shop?id=B&utm_source=search",),
    ) is True


@pytest.mark.parametrize(
    "alias_url",
    [
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
    ],
)
def test_source_evidence_identity_normalizes_youtube_host_and_path_aliases(
    alias_url: str,
) -> None:
    assert identification_pipeline._source_evidence_url_identity(
        alias_url
    ) == identification_pipeline._source_evidence_url_identity(
        "https://youtu.be/dQw4w9WgXcQ"
    )


@pytest.mark.parametrize(
    "short_url",
    ["https://t.co/abc123", "https://maps.app.goo.gl/abc123"],
)
def test_source_discovery_does_not_auto_resolve_from_unexpanded_short_url(
    short_url: str,
) -> None:
    mention = source_discovery_extraction(
        shop_name="Cafe Alpha"
    ).message.mentions[0]
    candidate = _source_web_candidate(
        name="Cafe Alpha",
        evidence_url="https://directory.example/cafe-alpha",
    )
    assert identification_pipeline._top_web_candidate_for_new_shop(
        mention,
        [candidate],
        require_precise_area_evidence=True,
        excluded_evidence_urls=(short_url,),
    ) is None
    assert identification_pipeline._top_web_candidate_for_new_shop(
        mention,
        [candidate.model_copy(update={"evidence_url": short_url})],
        require_precise_area_evidence=True,
        excluded_evidence_urls=(SOURCE_URL,),
    ) is None


def test_source_discovery_cache_hit_avoids_a_second_model_call(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_calls = 0

    async def discover(_content: str) -> ExtractionCallResult:
        nonlocal discovery_calls
        discovery_calls += 1
        return source_discovery_extraction()

    monkeypatch.setattr(identification_pipeline, "discover_restaurant_mentions", discover)
    db = db_factory()
    try:
        evidence = identification_pipeline._source_discovery_evidence(envelope())
        assert evidence is not None

        first = asyncio.run(
            identification_pipeline._source_discovery_message(db, MESSAGE_ID, evidence)
        )
        second = asyncio.run(
            identification_pipeline._source_discovery_message(
                db,
                NEXT_MESSAGE_ID,
                evidence,
            )
        )

        assert first == second
        assert discovery_calls == 1
        assert db.query(LookupCache).filter_by(kind="source_discovery").count() == 1
        assert db.query(ProcessingRun).filter_by(stage="source_discovery").count() == 1
    finally:
        db.close()


def test_source_discovery_keeps_unrelated_source_mention_for_review(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated_result = source_discovery_extraction()
    unrelated_result = ExtractionCallResult(
        message=unrelated_result.message,
        metrics=unrelated_result.metrics,
        source_urls=("https://unrelated.example/restaurant",),
    )

    async def unrelated_discovery(_content: str) -> ExtractionCallResult:
        return unrelated_result

    monkeypatch.setattr(
        identification_pipeline,
        "discover_restaurant_mentions",
        unrelated_discovery,
    )
    db = db_factory()
    try:
        evidence = identification_pipeline._source_discovery_evidence(envelope())
        assert evidence is not None

        result = asyncio.run(
            identification_pipeline._source_discovery_message(db, MESSAGE_ID, evidence)
        )

        assert len(result.mentions) == 1
        assert result.mentions[0].source_url is None
        assert result.mentions[0].needs_review is True
        assert "元出典未確認" in result.mentions[0].confidence_reason
        assert result.unresolved_reason is None
        assert (
            db.query(ProcessingRun)
            .filter_by(stage="source_discovery_grounding", status="failed")
            .count()
            == 1
        )
    finally:
        db.close()


def test_source_discovery_grounds_each_mention_to_its_own_input_source(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha_url = "https://source.example/alpha"
    beta_url = "https://source.example/beta"
    result = ExtractionCallResult(
        message=ExtractedMessage(
            is_restaurant_message=True,
            ignore_reason=None,
            unresolved_reason=None,
            mentions=[
                ExtractedMention(
                    shop_name="Cafe Alpha",
                    branch_name=None,
                    area="銀座",
                    category="カフェ・喫茶店",
                    source_url=alpha_url,
                    needs_review=False,
                    confidence_reason="alpha source",
                ),
                ExtractedMention(
                    shop_name="Cafe Beta",
                    branch_name=None,
                    area="新宿",
                    category="カフェ・喫茶店",
                    source_url=alpha_url,
                    needs_review=False,
                    confidence_reason="wrong alpha source",
                ),
            ],
        ),
        metrics=metrics(web_search_calls=1),
        source_urls=(alpha_url, beta_url),
    )

    async def discover(_content: str) -> ExtractionCallResult:
        return result

    async def fetch_source(url: str) -> FetchedHtmlDocument:
        assert url == alpha_url
        return FetchedHtmlDocument(
            html="<html><body>Cafe Alpha 銀座</body></html>",
            final_url=alpha_url,
        )

    monkeypatch.setattr(
        identification_pipeline,
        "discover_restaurant_mentions",
        discover,
    )
    monkeypatch.setattr(
        identification_pipeline,
        "fetch_html_document",
        fetch_source,
    )
    message_envelope = envelope().model_copy(
        update={
            "content": "two linked sources",
            "assets": (
                SourceAssetInput(
                    kind="link",
                    url=alpha_url,
                    title="Cafe Alpha 銀座",
                ),
                SourceAssetInput(
                    kind="link",
                    url=beta_url,
                    title="Cafe Beta 新宿",
                ),
            ),
        }
    )
    evidence = identification_pipeline._source_discovery_evidence(message_envelope)
    assert evidence is not None
    db = db_factory()
    try:
        discovered = asyncio.run(
            identification_pipeline._source_discovery_message(
                db,
                MESSAGE_ID,
                evidence,
            )
        )

        assert [mention.shop_name for mention in discovered.mentions] == [
            "Cafe Alpha",
            "Cafe Beta",
        ]
        assert discovered.mentions[0].source_url == alpha_url
        assert discovered.mentions[1].source_url is None
        assert discovered.mentions[1].needs_review is True
        assert "元出典未確認" in discovered.mentions[1].confidence_reason
        assert (
            db.query(ProcessingRun)
            .filter_by(stage="source_discovery_grounding", status="failed")
            .count()
            == 1
        )
    finally:
        db.close()


def test_source_mention_page_cache_is_specific_to_restaurant_name(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url = "https://source.example/post"
    message_envelope = envelope().model_copy(
        update={
            "content": source_url,
            "assets": (SourceAssetInput(kind="link", url=source_url),),
        }
    )
    evidence = identification_pipeline._source_discovery_evidence(message_envelope)
    assert evidence is not None
    fetch_calls = 0

    async def fetch_page(url: str) -> FetchedHtmlDocument:
        nonlocal fetch_calls
        fetch_calls += 1
        assert url == source_url
        return FetchedHtmlDocument(
            html="<html><body>Cafe Alpha 銀座</body></html>",
            final_url=source_url,
        )

    monkeypatch.setattr(
        identification_pipeline,
        "fetch_html_document",
        fetch_page,
    )
    alpha = ExtractedMention(
        shop_name="Cafe Alpha",
        branch_name=None,
        area="銀座",
        category="カフェ・喫茶店",
        source_url=source_url,
        needs_review=False,
        confidence_reason="source",
    )
    beta = alpha.model_copy(update={"shop_name": "Cafe Beta"})
    db = db_factory()
    try:
        assert asyncio.run(
            identification_pipeline._source_mention_is_grounded(
                db,
                MESSAGE_ID,
                alpha,
                evidence,
                (source_url,),
            )
        ) == (True, None)
        grounded, reason = asyncio.run(
            identification_pipeline._source_mention_is_grounded(
                db,
                MESSAGE_ID,
                beta,
                evidence,
                (source_url,),
            )
        )

        assert grounded is False
        assert reason == "元出典ページ上で店舗名と地域を同時に確認できない"
        assert fetch_calls == 2
        assert (
            db.query(LookupCache)
            .filter_by(kind=identification_pipeline.SOURCE_MENTION_CACHE_KIND)
            .count()
            == 1
        )
    finally:
        db.close()


def test_source_mention_rejects_visible_area_when_structured_address_conflicts(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url = "https://source.example/post"
    evidence = identification_pipeline._source_discovery_evidence(
        envelope().model_copy(
            update={
                "assets": (SourceAssetInput(kind="link", url=source_url),),
            }
        )
    )
    assert evidence is not None
    page = """
    <script type="application/ld+json">
    {"@type":"Restaurant","name":"Cafe Alpha", "address":"東京都新宿区新宿1-2-3"}
    </script>
    <body><p>Cafe Alpha</p><footer>店舗一覧: 銀座</footer></body>
    """

    async def fetch_page(_url: str) -> FetchedHtmlDocument:
        return FetchedHtmlDocument(html=page, final_url=source_url)

    monkeypatch.setattr(
        identification_pipeline,
        "fetch_html_document",
        fetch_page,
    )
    mention = ExtractedMention(
        shop_name="Cafe Alpha",
        branch_name=None,
        area="銀座",
        category="カフェ・喫茶店",
        source_url=source_url,
        needs_review=False,
        confidence_reason="source",
    )
    db = db_factory()
    try:
        grounded, reason = asyncio.run(
            identification_pipeline._source_mention_is_grounded(
                db,
                MESSAGE_ID,
                mention,
                evidence,
                (source_url,),
            )
        )

        assert grounded is False
        assert reason == "元出典ページ上で店舗名と地域を同時に確認できない"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("shop_name", "title", "area"),
    (
        ("Cafe Alpha", "Cafe Alphabet 銀座", "銀座"),
        ("銀座ライオン", "銀座ライオン 公式サイト", "銀座"),
        ("Cafe Alpha", "Cafe Alpha 西新宿", "新宿"),
    ),
)
def test_source_grounding_rejects_name_prefix_or_area_inside_shop_name(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    shop_name: str,
    title: str,
    area: str,
) -> None:
    source_url = "https://source.example/post"
    evidence = identification_pipeline._source_discovery_evidence(
        envelope().model_copy(
            update={
                "assets": (
                    SourceAssetInput(
                        kind="link",
                        url=source_url,
                        title=title,
                    ),
                ),
            }
        )
    )
    assert evidence is not None

    async def fetch_page(_url: str) -> FetchedHtmlDocument:
        return FetchedHtmlDocument(
            html=f"<html><body>{title}</body></html>",
            final_url=source_url,
        )

    monkeypatch.setattr(
        identification_pipeline,
        "fetch_html_document",
        fetch_page,
    )
    mention = ExtractedMention(
        shop_name=shop_name,
        branch_name=None,
        area=area,
        category="ビアホール",
        source_url=source_url,
        needs_review=False,
        confidence_reason="source",
    )
    db = db_factory()
    try:
        grounded, reason = asyncio.run(
            identification_pipeline._source_mention_is_grounded(
                db,
                MESSAGE_ID,
                mention,
                evidence,
                (source_url,),
            )
        )

        assert grounded is False
        assert reason == "元出典ページ上で店舗名と地域を同時に確認できない"
    finally:
        db.close()


def test_ungrounded_source_mention_is_persisted_without_wrong_source_url(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha_url = "https://source.example/alpha"
    beta_url = "https://source.example/beta"
    discovery = ExtractionCallResult(
        message=ExtractedMessage(
            is_restaurant_message=True,
            ignore_reason=None,
            unresolved_reason=None,
            mentions=[
                ExtractedMention(
                    shop_name="Cafe Alpha",
                    branch_name=None,
                    area="銀座",
                    category="カフェ・喫茶店",
                    source_url=alpha_url,
                    needs_review=False,
                    confidence_reason="alpha source",
                ),
                ExtractedMention(
                    shop_name="Cafe Beta",
                    branch_name=None,
                    area="新宿",
                    category="カフェ・喫茶店",
                    source_url=alpha_url,
                    needs_review=False,
                    confidence_reason="wrong source",
                ),
            ],
        ),
        metrics=metrics(web_search_calls=1),
        source_urls=(alpha_url, beta_url),
    )

    async def unresolved(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    async def discover(_content: str) -> ExtractionCallResult:
        return discovery

    async def fetch_source(_url: str) -> FetchedHtmlDocument:
        return FetchedHtmlDocument(
            html="<body>Cafe Alpha 銀座</body>",
            final_url=alpha_url,
        )

    async def no_structured(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", unresolved)
    monkeypatch.setattr(identification_pipeline, "discover_restaurant_mentions", discover)
    monkeypatch.setattr(identification_pipeline, "fetch_html_document", fetch_source)
    monkeypatch.setattr(identification_pipeline, "_structured_candidates", no_structured)
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web)
    message_envelope = envelope().model_copy(
        update={
            "content": "two sources",
            "assets": (
                SourceAssetInput(
                    kind="link",
                    url=alpha_url,
                    title="Cafe Alpha 銀座",
                ),
                SourceAssetInput(
                    kind="link",
                    url=beta_url,
                    title="Cafe Beta 新宿",
                ),
            ),
        }
    )
    db = db_factory()
    try:
        result = asyncio.run(
            identification_pipeline._process_message_transaction(
                db,
                message_envelope,
                allow_image_fallback=False,
                allow_source_discovery=True,
            )
        )
        rows = tuple(
            db.query(ShopMention)
            .filter_by(message_id=MESSAGE_ID)
            .order_by(ShopMention.occurrence_index)
            .all()
        )

        assert len(rows) == 2
        assert rows[0].source_url == alpha_url
        assert rows[1].source_url is None
        assert rows[1].review_status == "pending"
        assert result.pending_mention_ids == (rows[0].id, rows[1].id)
    finally:
        db.close()


def test_invalid_source_discovery_cache_is_recorded_and_refetched(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_calls = 0

    async def discover(_content: str) -> ExtractionCallResult:
        nonlocal discovery_calls
        discovery_calls += 1
        return source_discovery_extraction()

    monkeypatch.setattr(identification_pipeline, "discover_restaurant_mentions", discover)
    db = db_factory()
    try:
        evidence = identification_pipeline._source_discovery_evidence(envelope())
        assert evidence is not None
        identification_pipeline._put_cache(
            db,
            "source_discovery",
            evidence.cache_value,
            "not-json",
        )
        db.commit()

        result = asyncio.run(
            identification_pipeline._source_discovery_message(db, MESSAGE_ID, evidence)
        )
        db.commit()
        cached = identification_pipeline._get_cache(
            db,
            "source_discovery",
            evidence.cache_value,
        )

        assert result.mentions[0].shop_name == "abcdefghij"
        assert discovery_calls == 1
        assert cached is not None
        assert cached.payload != "not-json"
        assert (
            db.query(ProcessingRun)
            .filter_by(stage="source_discovery_cache", status="failed")
            .count()
            == 1
        )
    finally:
        db.close()


def test_unresolved_review_can_be_edited_and_approved(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(_content: str) -> ExtractionCallResult:
        return unresolved_extraction()

    monkeypatch.setattr(
        identification_pipeline,
        "extract_restaurant_message",
        fake_extract,
    )
    db = db_factory()
    try:
        message_envelope = envelope().model_copy(
            update={"assets": (SourceAssetInput(kind="link", url=SOURCE_URL),)}
        )
        result = asyncio.run(
            identification_pipeline.process_message(db, message_envelope)
        )
        mention_id = result.pending_mention_ids[0]
        decision = EditAndApproveDecision(
            action="edit_and_approve",
            expected_version=1,
            shop=EditableShop(
                shop_name="手修正した焼肉店",
                area="浅草橋",
                category="焼肉",
                address=None,
                phone=None,
                canonical_url=None,
            ),
        )

        decision_result = apply_review_decision(db, mention_id, decision)
        mention = db.query(ShopMention).filter(ShopMention.id == mention_id).one()

        assert decision_result.review_status == "approved"
        assert mention.shop is not None
        assert mention.shop.shop_name == "手修正した焼肉店"
        assert mention.shop.area == "浅草橋"
        assert mention.shop.category == "焼肉"
    finally:
        db.close()


class FakeAuthor:
    pass


class FakeHistoryMessage:
    def __init__(self, message_id: int, content: str, channel: FakeChannel) -> None:
        self.id = message_id
        self.content = content
        self.channel = channel
        self.author = FakeAuthor()
        self.created_at = datetime(2025, 6, 1, tzinfo=timezone.utc)
        self.embeds: list[object] = []
        self.attachments: list[object] = []


class FakeChannel:
    def __init__(self) -> None:
        self.id = int(CHANNEL_ID)
        self.messages: list[FakeHistoryMessage] = []
        self.sent: list[str] = []

    async def send(self, content: str) -> None:
        self.sent.append(content)

    async def history(
        self,
        *,
        limit: int,
        after: discord.Object | None,
        oldest_first: bool,
    ) -> AsyncIterator[FakeHistoryMessage]:
        assert limit > 0
        assert oldest_first is True
        for message in self.messages:
            if after is None or message.id > after.id:
                yield message


class FakeCommandMessage:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel


class FakeClient:
    def __init__(self) -> None:
        self.user = FakeAuthor()


def test_history_sync_advances_after_unresolved_review_item(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(content: str) -> ExtractionCallResult:
        if SOURCE_URL in content:
            return unresolved_extraction()
        return ignored_extraction()

    async def unresolved_discovery(_content: str) -> ExtractionCallResult:
        return ExtractionCallResult(
            message=ExtractedMessage(
                is_restaurant_message=True,
                ignore_reason=None,
                unresolved_reason="出典から店舗名を特定できない",
                mentions=[],
            ),
            metrics=metrics(web_search_calls=1),
            source_urls=(SOURCE_URL,),
        )

    monkeypatch.setattr(
        identification_pipeline,
        "extract_restaurant_message",
        fake_extract,
    )
    monkeypatch.setattr(
        identification_pipeline,
        "discover_restaurant_mentions",
        unresolved_discovery,
    )
    monkeypatch.setattr(sync_logic, "SessionLocal", db_factory)

    channel = FakeChannel()
    channel.messages = [
        FakeHistoryMessage(int(MESSAGE_ID), SOURCE_URL, channel),
        FakeHistoryMessage(int(NEXT_MESSAGE_ID), "not a restaurant", channel),
    ]
    client = FakeClient()
    command = FakeCommandMessage(channel)

    asyncio.run(
        sync_logic.sync_history(
            cast(discord.Client, client),
            cast(discord.Message, command),
        )
    )

    db = db_factory()
    try:
        state = db.query(SyncState).filter(SyncState.channel_id == CHANNEL_ID).one()
        unresolved = db.query(Message).filter(Message.message_id == MESSAGE_ID).one()
        ignored = db.query(Message).filter(Message.message_id == NEXT_MESSAGE_ID).one()
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert state.last_contiguous_message_id == NEXT_MESSAGE_ID
        assert unresolved.processing_status == "succeeded"
        assert ignored.processing_status == "ignored"
        assert mention.resolution_status == "not_found"
        assert mention.review_status == "pending"
    finally:
        db.close()


def test_history_sync_advances_after_unknown_category_review_item(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_extract(content: str) -> ExtractionCallResult:
        if SOURCE_URL in content:
            return unknown_category_extraction()
        return ignored_extraction()

    async def no_structured_candidates(
        _db: Session,
        _message_id: str,
        _url: str,
    ) -> list[PipelineCandidate]:
        return []

    async def no_web_candidates(
        _db: Session,
        _message_id: str,
        _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(
        identification_pipeline,
        "_structured_candidates",
        no_structured_candidates,
    )
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_web_candidates)
    monkeypatch.setattr(sync_logic, "SessionLocal", db_factory)

    channel = FakeChannel()
    channel.messages = [
        FakeHistoryMessage(int(MESSAGE_ID), SOURCE_URL, channel),
        FakeHistoryMessage(int(NEXT_MESSAGE_ID), "not a restaurant", channel),
    ]
    client = FakeClient()
    command = FakeCommandMessage(channel)

    asyncio.run(
        sync_logic.sync_history(
            cast(discord.Client, client),
            cast(discord.Message, command),
        )
    )

    db = db_factory()
    try:
        state = db.query(SyncState).filter(SyncState.channel_id == CHANNEL_ID).one()
        message = db.query(Message).filter(Message.message_id == MESSAGE_ID).one()
        mention = db.query(ShopMention).filter(ShopMention.message_id == MESSAGE_ID).one()

        assert state.last_contiguous_message_id == NEXT_MESSAGE_ID
        assert message.processing_status == "succeeded"
        assert mention.extracted_category == "メキシコ料理"
        assert mention.review_status == "pending"
        assert mention.difference_type == "new_ambiguous"
        assert mention.metadata_review_status == "pending"
        assert mention.metadata_difference_type == "unknown_category"
        assert mention.shop_id is None
    finally:
        db.close()
