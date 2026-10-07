from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bot.restaurant_extractor import (
    CandidateSearchResult, ExtractedMention, ExtractedMessage, ExtractionCallResult, ModelCallMetrics,
    is_known_category as extractor_is_known_category,
)
from db.models import Base, Message, ResolutionCandidate, Shop, ShopMention
from services import identification_pipeline
from services.category_normalization import (
    CATEGORY_VALUES, canonicalize_category, is_known_category,
)
from services.identification_pipeline import CandidateBatch, MessageEnvelope, SourceAssetInput
from services.resolution import CandidateIdentity


@pytest.mark.parametrize("raw", ["ジェラート", "ジェラート専門店", "アイスクリーム", "ソフトクリーム"])
def test_explicit_dessert_aliases_map_to_the_existing_category(raw: str) -> None:
    assert canonicalize_category(raw) == "スイーツ・洋菓子"
    assert canonicalize_category(f" {raw} ") == "スイーツ・洋菓子"
    assert is_known_category(raw) is False
    assert extractor_is_known_category(raw) is False


def test_existing_canonical_categories_and_strict_validation_are_unchanged() -> None:
    for category in CATEGORY_VALUES:
        assert canonicalize_category(category) == category
        assert is_known_category(category) is True
        assert extractor_is_known_category(category) is True
    assert is_known_category(" スイーツ・洋菓子 ") is False
    assert extractor_is_known_category(" スイーツ・洋菓子 ") is False


@pytest.mark.parametrize("raw", [
    None, "", " ", "不明", "メキシコ料理", "ジェラート・カフェ",
    "ジェラート／居酒屋", "ジェラートなど", "ジェラートかもしれない", "自家製ジェラート",
])
def test_unknown_and_mixed_categories_are_not_guessed_or_mapped_to_other(raw: str | None) -> None:
    assert canonicalize_category(raw) is None
    assert is_known_category(raw) is False


_MESSAGE_ID = "1436975355658244097"
_SOURCE_URL = "https://catalog.example/gelato"
_SHOP_NAME = "神田ジェラート店"


def _metrics() -> ModelCallMetrics:
    return ModelCallMetrics(
        model="synthetic-extraction", input_tokens=10, output_tokens=10,
        web_search_calls=0, image_count=0, latency_ms=1, estimated_cost_microusd=0,
    )


@pytest.mark.parametrize(
    "extracted_category,candidate_category,expected_category,expected_status,expected_reason",
    [
        ("ジェラート", None, "スイーツ・洋菓子", "approved", None),
        ("ジェラート", "アイスクリーム", "スイーツ・洋菓子", "approved", None),
        ("スイーツ・洋菓子", "ジェラート専門店", "スイーツ・洋菓子", "approved", None),
        ("ジェラート", "未知の業態", "未知の業態", "pending", "unknown_category"),
        ("ジェラート・カフェ", None, "ジェラート・カフェ", "pending", "unknown_category"),
        (None, None, None, "pending", "missing_category"),
    ],
)
def test_full_pipeline_normalizes_shop_category_and_preserves_raw_evidence(
    monkeypatch: pytest.MonkeyPatch,
    extracted_category: str | None,
    candidate_category: str | None,
    expected_category: str | None,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    async def extracted_response(_content: str) -> ExtractionCallResult:
        return ExtractionCallResult(
            message=ExtractedMessage(
                is_restaurant_message=True,
                mentions=[ExtractedMention(
                    shop_name=_SHOP_NAME, area="神田", category=extracted_category,
                    needs_review=False, confidence_reason="合成投稿に店名・所在地・業態が記載されている",
                )],
            ),
            metrics=_metrics(),
        )

    async def structured_response(url: str) -> list[CandidateIdentity]:
        assert url == _SOURCE_URL
        return [CandidateIdentity(
            name=_SHOP_NAME, area="神田", category=candidate_category,
            address="東京都千代田区神田鍛冶町1-2-3",
            canonical_url=_SOURCE_URL, evidence_url=_SOURCE_URL,
        )]

    async def unexpected_search(_mention: ExtractedMention) -> CandidateSearchResult:
        raise AssertionError("The supplied structured evidence should identify the synthetic shop")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", extracted_response)
    monkeypatch.setattr(identification_pipeline, "fetch_structured_candidates", structured_response)
    monkeypatch.setattr(identification_pipeline, "search_restaurant_candidates", unexpected_search)
    envelope = MessageEnvelope(
        message_id=_MESSAGE_ID, channel_id="1432348507410534512",
        content=f"神田ジェラート店で食べた。所在地は神田。 {_SOURCE_URL}",
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        assets=(SourceAssetInput(kind="link", url=_SOURCE_URL),),
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            result = asyncio.run(identification_pipeline.process_message(db, envelope))
            assert len(result.shop_ids) == 1
        with Session(engine) as verified:
            shop = verified.query(Shop).one()
            mention = verified.query(ShopMention).one()
            candidate = verified.query(ResolutionCandidate).one()
            assert verified.get(Message, _MESSAGE_ID).processing_status == "succeeded"
            assert shop.category == expected_category
            assert mention.shop_id == shop.id
            assert mention.review_status == "approved"
            assert mention.metadata_review_status == expected_status
            assert mention.metadata_difference_type == expected_reason
            assert mention.extracted_category == extracted_category
            assert candidate.category == candidate_category
    finally:
        engine.dispose()


def test_recognized_category_does_not_create_an_unidentified_shop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def extracted_response(_content: str) -> ExtractionCallResult:
        return ExtractionCallResult(
            message=ExtractedMessage(
                is_restaurant_message=True,
                mentions=[ExtractedMention(
                    shop_name=_SHOP_NAME, area="神田", category="ジェラート",
                    needs_review=True, confidence_reason="合成投稿だけでは所在地と実在店舗を確認できない",
                )],
            ),
            metrics=_metrics(),
        )

    async def no_candidates(
        _db: Session, _message_id: str, _mention: ExtractedMention,
    ) -> CandidateBatch:
        return CandidateBatch(())

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", extracted_response)
    monkeypatch.setattr(identification_pipeline, "_web_candidates", no_candidates)
    envelope = MessageEnvelope(
        message_id=_MESSAGE_ID, channel_id="1432348507410534512",
        content="神田ジェラート店というお店らしい。",
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            result = asyncio.run(identification_pipeline.process_message(db, envelope))
        with Session(engine) as verified:
            mention = verified.query(ShopMention).one()
            assert result.shop_ids == ()
            assert verified.query(Shop).count() == 0
            assert mention.shop_id is None
            assert mention.review_status == "pending"
            assert mention.extracted_category == "ジェラート"
    finally:
        engine.dispose()


def test_new_extraction_does_not_reclassify_an_existing_user_reviewed_shop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def extracted_response(_content: str) -> ExtractionCallResult:
        return ExtractionCallResult(
            message=ExtractedMessage(
                is_restaurant_message=True,
                mentions=[ExtractedMention(
                    shop_name=_SHOP_NAME, area="神田", category="ジェラート",
                    needs_review=False, confidence_reason="合成投稿に店名と所在地が記載されている",
                )],
            ),
            metrics=_metrics(),
        )

    async def unexpected_search(_mention: ExtractedMention) -> CandidateSearchResult:
        raise AssertionError("The existing approved shop should be reused")

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", extracted_response)
    monkeypatch.setattr(identification_pipeline, "search_restaurant_candidates", unexpected_search)
    envelope = MessageEnvelope(
        message_id=_MESSAGE_ID, channel_id="1432348507410534512",
        content="神田ジェラート店へ。場所は神田。",
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            shop = Shop(shop_name=_SHOP_NAME, area="神田", category="甘味処", memo="利用者の補足", version=7)
            prior = Message(message_id="12345678901234567", content="利用者が確認した先行投稿")
            db.add_all([shop, prior])
            db.flush()
            db.add(ShopMention(
                shop=shop, message=prior, occurrence_index=0, extracted_name=_SHOP_NAME,
                extracted_area="神田", extracted_category="和菓子と甘味",
                review_status="approved", metadata_review_status="approved",
                resolution_status="resolved", resolution_method="manual",
            ))
            db.commit()
            asyncio.run(identification_pipeline.process_message(db, envelope))
        with Session(engine) as verified:
            stored_shop = verified.query(Shop).one()
            new_mention = verified.query(ShopMention).filter(ShopMention.message_id == _MESSAGE_ID).one()
            assert (stored_shop.category, stored_shop.memo, stored_shop.version) == ("甘味処", "利用者の補足", 7)
            assert new_mention.shop_id == stored_shop.id
            assert new_mention.extracted_category == "ジェラート"
            assert new_mention.metadata_review_status == "approved"
    finally:
        engine.dispose()
