from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from bot.restaurant_extractor import (
    ExtractedMention,
    ExtractedMessage,
    ExtractionCallResult,
    ImageAnalysisResult,
    ImageClues,
    ModelCallMetrics,
)
from db.models import (
    Base,
    CandidateProvenance,
    Message,
    Shop,
    ShopMention,
    SourceAsset,
)
from services import evaluation_service
from services.identification_pipeline import PipelineCandidate
from services.resolution import CandidateIdentity


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


def add_legacy_message(db: Session) -> ShopMention:
    message = Message(message_id="12345678901234567", content="割烹みやび 銀座")
    shop = Shop(shop_name="割烹みやび", area="銀座", category="割烹")
    mention = ShopMention(
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
    db.add(mention)
    db.commit()
    return mention


def extraction(area: str) -> ExtractionCallResult:
    message = ExtractedMessage(
        is_restaurant_message=True,
        ignore_reason=None,
        mentions=[
            ExtractedMention(
                shop_name="割烹みやび",
                branch_name=None,
                area=area,
                category="割烹",
                needs_review=False,
                confidence_reason="本文に店名とエリアがある",
            )
        ],
    )
    metrics = ModelCallMetrics(
        model="test-model",
        input_tokens=10,
        output_tokens=5,
        web_search_calls=0,
        image_count=0,
        latency_ms=1,
        estimated_cost_microusd=1,
    )
    return ExtractionCallResult(message=message, metrics=metrics)


def test_evaluation_keeps_canonical_data_when_there_is_no_difference(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention = add_legacy_message(db)

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return extraction("銀座")

    async def fake_candidates(
        _db: Session,
        _message: Message,
        _extracted: ExtractedMention,
        _assets: tuple[object, ...],
    ) -> list[CandidateIdentity]:
        raise AssertionError("candidate lookup must be skipped for an exact approved match")

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(evaluation_service, "_collect_candidates", fake_candidates)
    result = asyncio.run(
        evaluation_service.evaluate_imported_message(db, mention.message_id)
    )
    db.refresh(mention)
    assert result.differences == 0
    assert mention.review_status == "approved"
    assert mention.shop is not None
    assert mention.shop.area == "銀座"


def test_evaluation_records_difference_without_overwriting_shop(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention = add_legacy_message(db)

    async def fake_extract(_content: str) -> ExtractionCallResult:
        return extraction("月島")

    async def fake_candidates(
        _db: Session,
        _message: Message,
        _extracted: ExtractedMention,
        _assets: tuple[object, ...],
    ) -> list[CandidateIdentity]:
        return []

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(evaluation_service, "_collect_candidates", fake_candidates)
    result = asyncio.run(
        evaluation_service.evaluate_imported_message(db, mention.message_id)
    )
    db.refresh(mention)
    assert result.differences == 1
    assert mention.review_status == "pending"
    assert mention.difference_type == "new_pipeline_difference"
    assert mention.extracted_area == "月島"
    assert mention.shop is not None
    assert mention.shop.area == "銀座"


def test_unverified_candidate_cannot_create_an_evaluation_difference() -> None:
    shop = Shop(
        shop_name="割烹みやび",
        area="銀座",
        category="割烹",
        phone="03-1234-5678",
    )
    candidate = PipelineCandidate(
        name="割烹みやび",
        area="銀座",
        category="日本料理",
        phone="03-1234-5678",
        canonical_url="https://example.com/miyabi",
        provenance=CandidateProvenance.WEB_SEARCH,
        is_verified=False,
        verification_reason="Web search result",
    )

    assert evaluation_service._strong_candidate_for_shop(shop, [candidate]) is None


def test_candidate_category_difference_is_metadata_only() -> None:
    shop = Shop(
        shop_name="割烹みやび",
        area="銀座",
        category="割烹",
        phone="03-1234-5678",
    )
    candidate = PipelineCandidate(
        name="割烹みやび",
        area="銀座",
        category="日本料理",
        phone="03-1234-5678",
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
    )

    assert evaluation_service._identity_candidate_differs(shop, candidate) is False
    assert evaluation_service._metadata_candidate_differs(shop, candidate) is True


def test_candidate_name_matches_shop_branch_stored_separately() -> None:
    shop = Shop(
        shop_name="銀座 鮨はな",
        branch_name="本店",
        area="銀座",
        category="寿司・回転寿司",
    )
    candidate = PipelineCandidate(
        name="銀座・鮨はな 本店",
        area="銀座",
        category="寿司・回転寿司",
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
    )

    assert evaluation_service._identity_candidate_differs(shop, candidate) is False


def test_evaluation_category_change_keeps_identity_approved(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention = add_legacy_message(db)

    async def fake_extract(_content: str) -> ExtractionCallResult:
        result = extraction("銀座")
        changed = result.message.mentions[0].model_copy(update={"category": "焼肉"})
        return ExtractionCallResult(
            message=result.message.model_copy(update={"mentions": [changed]}),
            metrics=result.metrics,
        )

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", fake_extract)

    result = asyncio.run(
        evaluation_service.evaluate_imported_message(db, mention.message_id)
    )
    db.refresh(mention)

    assert result.differences == 1
    assert mention.review_status == "approved"
    assert mention.difference_type is None
    assert mention.metadata_review_status == "pending"
    assert mention.metadata_difference_type == "new_pipeline_metadata_difference"


def test_multiple_evaluation_mentions_do_not_use_shared_canonical_hint(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_url = "https://tabelog.com/tokyo/A1301/A130101/13000001/"
    message = Message(
        message_id="22345678901234567",
        content="浅草橋の焼肉と横浜の寿司",
        processing_status="pending",
    )
    message.assets.append(SourceAsset(kind="link", url=canonical_url))
    db.add(message)
    db.commit()
    seen_hints: list[str | None] = []

    async def fake_extract(_content: str) -> ExtractionCallResult:
        mentions = [
            ExtractedMention(
                shop_name="浅草橋 焼肉はな",
                branch_name=None,
                area="浅草橋",
                category="焼肉",
                needs_review=False,
                confidence_reason="本文に店名とエリアがある",
            ),
            ExtractedMention(
                shop_name="横浜 鮨つき",
                branch_name=None,
                area="横浜",
                category="寿司・回転寿司",
                needs_review=False,
                confidence_reason="本文に店名とエリアがある",
            ),
        ]
        return ExtractionCallResult(
            message=ExtractedMessage(
                is_restaurant_message=True,
                ignore_reason=None,
                unresolved_reason=None,
                mentions=mentions,
            ),
            metrics=extraction("銀座").metrics,
        )

    async def no_candidates(
        _db: Session,
        _message: Message,
        _extracted: ExtractedMention,
        _assets: tuple[object, ...],
    ) -> list[PipelineCandidate]:
        return []

    def no_existing_shop(
        _db: Session,
        _extracted: ExtractedMention,
        automatic_canonical_hint: str | None,
    ) -> tuple[None, None]:
        seen_hints.append(automatic_canonical_hint)
        return None, None

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(evaluation_service, "_collect_candidates", no_candidates)
    monkeypatch.setattr(
        evaluation_service,
        "_find_existing_shop_from_mention",
        no_existing_shop,
    )

    result = asyncio.run(evaluation_service.evaluate_imported_message(db, message.message_id))

    assert seen_hints == [None, None, None, None]
    assert result.new_mentions == 2
    assert db.query(Shop).count() == 0
    assert db.query(ShopMention).filter(ShopMention.shop_id.is_(None)).count() == 2


def test_evaluation_links_new_mention_to_verified_collision(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_url = "https://official.example/miyabi"
    approved_mention = add_legacy_message(db)
    approved_mention.shop.canonical_url = canonical_url
    message = Message(
        message_id="32345678901234567",
        content="割烹みやび本舗 銀座",
        processing_status="pending",
    )
    db.add(message)
    db.commit()
    candidate = PipelineCandidate(
        name="割烹みやび本舗",
        area="銀座",
        category="割烹",
        canonical_url=canonical_url,
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="server-fetched JSON-LD",
    )

    async def fake_extract(_content: str) -> ExtractionCallResult:
        extracted = extraction("銀座")
        mention = extracted.message.mentions[0].model_copy(
            update={"shop_name": "割烹みやび本舗"}
        )
        return ExtractionCallResult(
            message=extracted.message.model_copy(update={"mentions": [mention]}),
            metrics=extracted.metrics,
        )

    async def fake_candidates(
        _db: Session,
        _message: Message,
        _extracted: ExtractedMention,
        _assets: tuple[object, ...],
    ) -> list[PipelineCandidate]:
        return [candidate]

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(evaluation_service, "_collect_candidates", fake_candidates)

    result = asyncio.run(
        evaluation_service.evaluate_imported_message(db, message.message_id)
    )

    created_mention = (
        db.query(ShopMention)
        .filter(ShopMention.message_id == message.message_id)
        .one()
    )
    stored_candidate = created_mention.candidates[0]
    assert result.new_mentions == 1
    assert created_mention.shop_id == approved_mention.shop_id
    assert created_mention.resolution_status == "resolved"
    assert created_mention.review_status == "approved"
    assert created_mention.resolution_method == "automatic"
    assert created_mention.resolution_basis == "verified_collision"
    assert created_mention.reviewed_at is not None
    assert stored_candidate.rank == 1
    assert stored_candidate.is_strong_match is True
    assert db.query(Shop).count() == 1


def test_evaluation_image_path_preserves_verified_collision_candidate(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_url = "https://official.example/miyabi"
    approved_mention = add_legacy_message(db)
    approved_mention.shop.canonical_url = canonical_url
    message = Message(
        message_id="42345678901234567",
        content="割烹みやび本舗 銀座",
        processing_status="pending",
    )
    message.assets.append(
        SourceAsset(
            kind="image",
            url="https://cdn.discordapp.com/attachments/1/2/receipt.jpg",
            mime_type="image/jpeg",
        )
    )
    db.add(message)
    db.commit()
    candidate = PipelineCandidate(
        name="割烹みやび本舗",
        area="銀座",
        category="割烹",
        canonical_url=canonical_url,
        provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True,
        verification_reason="image evidence verified by structured data",
    )

    async def fake_extract(_content: str) -> ExtractionCallResult:
        extracted = extraction("銀座")
        mention = extracted.message.mentions[0].model_copy(
            update={"shop_name": "割烹みやび本舗"}
        )
        return ExtractionCallResult(
            message=extracted.message.model_copy(update={"mentions": [mention]}),
            metrics=extracted.metrics,
        )

    async def no_candidates(
        _db: Session,
        _message: Message,
        _extracted: ExtractedMention,
        _assets: tuple[object, ...],
    ) -> list[PipelineCandidate]:
        return []

    async def fake_image_analysis(
        _mention: ExtractedMention,
        _urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        return ImageAnalysisResult(
            clues=ImageClues(
                subject_kind="restaurant",
                usable=True,
                image_type="receipt",
                visible_shop_names=["割烹みやび本舗"],
                address_clues=[],
                phone_clues=[],
                reason="店名を確認",
            ),
            metrics=extraction("銀座").metrics,
        )

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", fake_extract)
    monkeypatch.setattr(evaluation_service, "_collect_candidates", no_candidates)
    monkeypatch.setattr(evaluation_service, "analyze_restaurant_images", fake_image_analysis)
    monkeypatch.setattr(
        evaluation_service,
        "_candidates_from_image",
        lambda _result, _url: [candidate],
    )

    asyncio.run(evaluation_service.evaluate_imported_message(db, message.message_id))

    created_mention = (
        db.query(ShopMention)
        .filter(ShopMention.message_id == message.message_id)
        .one()
    )
    stored_candidate = created_mention.candidates[0]
    assert created_mention.shop_id == approved_mention.shop_id
    assert created_mention.review_status == "approved"
    assert created_mention.resolution_method == "automatic"
    assert created_mention.resolution_basis == "verified_collision"
    assert stored_candidate.rank == 1
    assert stored_candidate.is_strong_match is True
    assert db.query(Shop).count() == 1
