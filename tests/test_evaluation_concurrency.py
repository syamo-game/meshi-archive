from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import ConnectionPoolEntry

from bot.restaurant_extractor import (
    ExtractedMention, ExtractedMessage, ExtractionCallResult,
    ImageAnalysisResult, ImageClues, ModelCallMetrics,
)
from db.models import (
    Base, CandidateProvenance, LookupCache, Message, ProcessingRun, ResolutionCandidate, ReviewEvent,
    Shop, ShopMention, SourceAsset,
)
from services import evaluation_service, identification_pipeline
from services.identification_pipeline import PipelineCandidate, SourceAssetInput
from services.import_service import apply_csv_update, parse_csv_update
from services.review_service import DeferDecision, MergeDecision, apply_review_decision
from services.resolution import CandidateIdentity
from services.extraction_safety import SubjectKind, requires_manual_evidence_review


MESSAGE_ID = "52345678901234567"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_engine(
        f"sqlite:///{(tmp_path / 'evaluation.sqlite').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(database, "connect")
    def enable_foreign_keys(connection: sqlite3.Connection, _record: ConnectionPoolEntry) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(database)
    with Session(database) as db:
        shop = Shop(id=1, shop_name="割烹みやび", area="銀座", category="割烹", memo="保存済みメモ")
        db.add(Shop(id=2, shop_name="移動先みやび", area="銀座", category="割烹"))
        mention = ShopMention(
            id=1, message=Message(message_id=MESSAGE_ID, content="割烹みやび 銀座"),
            shop=shop, occurrence_index=0, extracted_name="割烹みやび", extracted_area="銀座",
            extracted_category="割烹", resolution_status="resolved", review_status="approved",
            resolution_method="manual", extraction_source="legacy_import",
            confidence_reason="元の根拠", source_url="https://example.com/source",
        )
        db.add(mention)
        db.flush()
        db.add(ResolutionCandidate(mention_id=1, rank=1, name="既存候補", provenance="web_search"))
        db.add(SourceAsset(message_id=MESSAGE_ID, kind="image",
                           url="https://cdn.discordapp.com/attachments/1/2/receipt.jpg"))
        db.commit()
    yield database
    database.dispose()


def _extraction(*, different: bool = False, new_first: bool = False) -> ExtractionCallResult:
    mention = ExtractedMention(
        shop_name="割烹みやび", branch_name=None, area="月島" if different else "銀座",
        category="割烹", needs_review=False, confidence_reason="本文の店舗名",
    )
    mentions = [mention]
    if new_first:
        mentions.insert(0, mention.model_copy(update={"shop_name": "新規の食堂", "area": "新宿"}))
    return ExtractionCallResult(
        message=ExtractedMessage(is_restaurant_message=True, mentions=mentions),
        metrics=ModelCallMetrics(model="test", input_tokens=7, output_tokens=3,
                                 web_search_calls=0, image_count=0, latency_ms=1,
                                 estimated_cost_microusd=2),
    )


def _defer(engine: Engine) -> None:
    with Session(engine) as other:
        apply_review_decision(other, 1, DeferDecision(
            action="defer", expected_version=1, shop_version=1, note="AI待機中の確認",
        ))
        other.commit()


def _merge(engine: Engine) -> None:
    with Session(engine) as other:
        apply_review_decision(other, 1, MergeDecision(
            action="merge", expected_version=1, shop_version=1, target_shop_id=2,
            target_version=1, is_visited=False, memo="保存済みメモ",
        ))
        other.commit()


def _assert_skipped_without_candidate_changes(engine: Engine, *, shop_id: int = 1) -> None:
    with Session(engine) as db:
        mention = db.get(ShopMention, 1)
        assert mention is not None
        assert mention.shop_id == shop_id
        assert mention.extracted_area == "銀座"
        assert mention.confidence_reason == "元の根拠"
        assert mention.source_url == "https://example.com/source"
        assert db.query(ShopMention).count() == 1
        assert db.query(ResolutionCandidate).one().name == "既存候補"
        assert db.query(SourceAsset).count() == 1
        skipped = db.query(ProcessingRun).filter_by(stage="evaluation_pipeline").one()
        assert skipped.status == "ignored"
        assert skipped.error == "concurrent_change"


@pytest.mark.parametrize("change", ["review", "csv", "content", "assets", "mention_added"])
def test_changes_during_extraction_skip_entire_evaluation(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    calls = 0

    async def extract(_content: str) -> ExtractionCallResult:
        nonlocal calls
        calls += 1
        if change == "review":
            _defer(engine)
        elif change == "csv":
            csv = f"_id,shop.version,message_id,memo\n1,1,{MESSAGE_ID},他者の最新メモ\n".encode("utf-8")
            with Session(engine) as other:
                apply_csv_update(other, parse_csv_update(csv, "edit.csv"))
        else:
            with Session(engine) as other:
                if change == "content":
                    other.get(Message, MESSAGE_ID).content = "投稿者が訂正した本文"
                elif change == "assets":
                    other.query(SourceAsset).one().title = "新しい添付の説明"
                else:
                    other.add(ShopMention(message_id=MESSAGE_ID, shop_id=1, occurrence_index=1,
                                           extracted_name="追加された投稿", review_status="pending"))
                other.commit()
        return _extraction()

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", extract)
    with Session(engine) as db:
        # A cached object must not bypass the final database comparison.
        cached = db.get(ShopMention, 1)
        assert cached is not None and cached.version == 1
        result = asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))
    assert calls == 1
    assert result.skipped and result.skip_reason == "concurrent_change"
    assert (result.matched_mentions, result.new_mentions, result.differences) == (0, 0, 0)
    with Session(engine) as db:
        assert db.query(ProcessingRun).filter_by(stage="evaluation_extraction").count() == 1
        assert db.query(ResolutionCandidate).one().name == "既存候補"
        assert db.get(Message, MESSAGE_ID).processed_at is None
        assert db.get(ShopMention, 1).extracted_area == "銀座"
        if change == "review":
            assert (db.get(ShopMention, 1).version, db.get(ShopMention, 1).review_status) == (2, "deferred")
            assert db.query(ReviewEvent).one().note == "AI待機中の確認"
        elif change == "csv":
            assert (db.get(Shop, 1).version, db.get(Shop, 1).memo) == (2, "他者の最新メモ")
            assert db.get(ShopMention, 1).version == 1
        elif change == "mention_added":
            assert db.query(ShopMention).count() == 2


@pytest.mark.parametrize("stage", ["candidates", "image"])
def test_merge_during_later_await_preserves_relation_history_and_all_proposals(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    candidate_calls = 0
    image_calls = 0

    async def extract(_content: str) -> ExtractionCallResult:
        return _extraction(different=True, new_first=stage == "candidates")

    async def candidates(
        _db: Session, _message: Message, _extracted: ExtractedMention,
        _assets: tuple[SourceAssetInput, ...],
    ) -> list[PipelineCandidate]:
        nonlocal candidate_calls
        candidate_calls += 1
        if stage == "candidates" and candidate_calls == 2:
            _merge(engine)
        return []

    async def image(_mention: ExtractedMention, _urls: tuple[str, ...]) -> ImageAnalysisResult:
        nonlocal image_calls
        image_calls += 1
        if stage == "image":
            _merge(engine)
        return ImageAnalysisResult(clues=ImageClues(usable=False, image_type="other", reason="判読不可",
                                                   visible_shop_names=[], address_clues=[], phone_clues=[]),
                                   metrics=_extraction().metrics)

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", extract)
    monkeypatch.setattr(evaluation_service, "_collect_candidates", candidates)
    monkeypatch.setattr(evaluation_service, "analyze_restaurant_images", image)
    with Session(engine) as db:
        result = asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))
    assert result.skipped and result.skip_reason == "concurrent_change"
    assert candidate_calls == (2 if stage == "candidates" else 1)
    assert image_calls == (0 if stage == "candidates" else 1)
    _assert_skipped_without_candidate_changes(engine, shop_id=2)
    with Session(engine) as db:
        assert db.get(Shop, 1) is None
        assert db.get(Shop, 2).memo == "保存済みメモ"
        assert db.get(ShopMention, 1).version == 2
        assert db.query(ReviewEvent).one().action == "merge"
        assert db.query(ProcessingRun).filter_by(stage="evaluation_image").count() == (0 if stage == "candidates" else 1)


@pytest.mark.parametrize("existing_history", [True, False])
def test_human_reviewed_message_is_skipped_before_any_ai(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, existing_history: bool,
) -> None:
    with Session(engine) as db:
        if existing_history:
            db.add(ReviewEvent(mention_id=1, action="approve_current", note="既存の確認"))
        else:
            db.get(ShopMention, 1).extraction_source = "responses_structured"
        db.commit()

    async def unexpected(_content: str) -> ExtractionCallResult:
        raise AssertionError("human decisions must not be re-evaluated")

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", unexpected)
    with Session(engine) as db:
        result = asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))
        assert result.skipped and result.skip_reason == "human_reviewed"
        assert db.get(ShopMention, 1).version == 1
        assert db.get(ShopMention, 1).review_status == "approved"
        assert db.query(ResolutionCandidate).one().name == "既存候補"
        assert db.query(ProcessingRun).count() == 1


def test_ai_failure_does_not_mark_another_evaluation_result_failed(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_after_update(_content: str) -> ExtractionCallResult:
        with Session(engine) as other:
            other.get(Message, MESSAGE_ID).processing_status = "succeeded"
            other.commit()
        raise RuntimeError("synthetic AI failure")

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", fail_after_update)
    with Session(engine) as db:
        result = asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))
        assert result.skipped and result.skip_reason == "concurrent_change"
        assert db.get(Message, MESSAGE_ID).processing_status == "succeeded"
        assert db.query(ProcessingRun).filter_by(stage="evaluation_extraction", status="failed").count() == 1
        assert db.get(ShopMention, 1).version == 1


def test_ai_failure_records_metrics_once_and_leaves_domain_unchanged(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(_content: str) -> ExtractionCallResult:
        raise RuntimeError("synthetic AI failure")

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", fail)
    with Session(engine) as db:
        with pytest.raises(RuntimeError, match="synthetic AI failure"):
            asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))
        assert db.get(Message, MESSAGE_ID).processing_status == "failed"
        assert db.get(ShopMention, 1).version == 1
        assert db.query(ResolutionCandidate).one().name == "既存候補"
        assert db.query(ProcessingRun).filter_by(stage="evaluation_extraction", status="failed").count() == 1
        assert db.query(ProcessingRun).filter_by(stage="evaluation_pipeline", status="failed").count() == 1


def test_message_removed_during_ai_skips_without_dangling_processing_run(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def extract(_content: str) -> ExtractionCallResult:
        with Session(engine) as other:
            other.delete(other.get(Message, MESSAGE_ID))
            other.commit()
        return _extraction()

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", extract)
    with Session(engine) as db:
        result = asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))
        assert result.skipped and result.skip_reason == "message_removed"
        assert db.get(Message, MESSAGE_ID) is None
        assert db.query(ProcessingRun).count() == 0
        assert db.get(Shop, 1).memo == "保存済みメモ"


def test_missing_message_at_start_retains_not_found_error(engine: Engine) -> None:
    with Session(engine) as db:
        with pytest.raises(ValueError, match="Evaluation message not found"):
            asyncio.run(evaluation_service.evaluate_imported_message(db, "99999999999999999"))
        assert db.query(ProcessingRun).count() == 0


def test_broken_cache_retrieval_does_not_block_review_or_duplicate_journal(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/broken-cache"
    with Session(engine) as db:
        db.query(SourceAsset).delete()
        identification_pipeline._put_cache(db, "url_metadata", url, "not-json")
        db.commit()

    async def extract(_content: str) -> ExtractionCallResult:
        return _extraction(different=True)

    async def fetch(_url: str) -> list[CandidateIdentity]:
        _defer(engine)
        return []

    async def candidates(
        db: Session, message: Message, _extracted: ExtractedMention,
        _assets: tuple[SourceAssetInput, ...],
    ) -> list[PipelineCandidate]:
        return await identification_pipeline._structured_candidates(db, message.message_id, url)

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", extract)
    monkeypatch.setattr(evaluation_service, "_collect_candidates", candidates)
    monkeypatch.setattr(identification_pipeline, "fetch_structured_candidates", fetch)
    with Session(engine) as db:
        result = asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))
        assert result.skipped and result.skip_reason == "concurrent_change"
        assert (db.get(ShopMention, 1).version, db.get(ShopMention, 1).review_status) == (2, "deferred")
        assert db.query(ProcessingRun).filter_by(stage="url_metadata_cache", status="failed").count() == 1
        assert db.query(ProcessingRun).filter_by(stage="evaluation_extraction").count() == 1
        assert db.query(ProcessingRun).filter_by(stage="evaluation_pipeline", status="ignored").count() == 1
        assert db.query(LookupCache).one().payload == '{"candidates":[]}'


@pytest.mark.parametrize("subject_kind", ["unknown", "product", "event", "restaurant"])
def test_image_subject_review_blocks_otherwise_verified_collision(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, subject_kind: SubjectKind,
) -> None:
    new_id = "62345678901234567"
    canonical_url = "https://official.example/miyabi"
    with Session(engine) as db:
        shop = db.get(Shop, 1)
        shop.canonical_url = canonical_url
        shop.phone = "0312345678"
        message = Message(message_id=new_id, content="割烹みやび本舗 銀座")
        message.assets.append(SourceAsset(kind="image", url="https://cdn.discordapp.com/attachments/3/4/p.jpg"))
        db.add(message)
        db.commit()
    candidate = PipelineCandidate(
        name="割烹みやび本舗", area="銀座", category="割烹", phone="0312345678",
        canonical_url=canonical_url, provenance=CandidateProvenance.STRUCTURED_DATA,
        is_verified=True, verification_reason="synthetic verified image evidence",
    )

    async def extract(_content: str) -> ExtractionCallResult:
        result = _extraction()
        return ExtractionCallResult(
            result.message.model_copy(update={"mentions": [result.message.mentions[0].model_copy(update={"shop_name": candidate.name})]}),
            result.metrics,
        )

    async def candidates(
        _db: Session, _message: Message, _extracted: ExtractedMention,
        _assets: tuple[SourceAssetInput, ...],
    ) -> list[PipelineCandidate]:
        return []

    async def image(_mention: ExtractedMention, _urls: tuple[str, ...]) -> ImageAnalysisResult:
        return ImageAnalysisResult(
            ImageClues(usable=True, image_type="receipt", subject_kind=subject_kind,
                       visible_shop_names=[candidate.name], address_clues=[], phone_clues=["0312345678"],
                       reason="合成した画像根拠"),
            _extraction().metrics,
        )

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", extract)
    monkeypatch.setattr(evaluation_service, "_collect_candidates", candidates)
    monkeypatch.setattr(evaluation_service, "analyze_restaurant_images", image)
    monkeypatch.setattr(evaluation_service, "_candidates_from_image", lambda _result, _url: [candidate])
    with Session(engine) as db:
        result = asyncio.run(evaluation_service.evaluate_imported_message(db, new_id))
        mention = db.query(ShopMention).filter_by(message_id=new_id).one()
        assert not result.skipped
        assert len(mention.candidates) == 1
        assert db.get(Shop, 1).memo == "保存済みメモ"
        if subject_kind == "restaurant":
            assert mention.shop_id == 1 and mention.review_status == "approved"
        else:
            assert mention.shop_id is None
            assert mention.review_status == ("rejected" if subject_kind == "event" else "pending")
            assert requires_manual_evidence_review(mention.extraction_error)
            assert not mention.candidates[0].is_strong_match


def test_unclear_image_preserves_previous_candidate_and_adds_review_evidence(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def extract(_content: str) -> ExtractionCallResult:
        return _extraction(different=True)

    async def candidates(
        _db: Session, _message: Message, _extracted: ExtractedMention,
        _assets: tuple[SourceAssetInput, ...],
    ) -> list[PipelineCandidate]:
        return []

    async def image(_mention: ExtractedMention, _urls: tuple[str, ...]) -> ImageAnalysisResult:
        return ImageAnalysisResult(
            ImageClues(usable=True, image_type="other", subject_kind="product",
                       visible_shop_names=["割烹みやび"], address_clues=[], phone_clues=["0312345678"],
                       reason="商品名の可能性"),
            _extraction().metrics,
        )

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", extract)
    monkeypatch.setattr(evaluation_service, "_collect_candidates", candidates)
    monkeypatch.setattr(evaluation_service, "analyze_restaurant_images", image)
    with Session(engine) as db:
        result = asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))
        assert not result.skipped
        mention = db.get(ShopMention, 1)
        assert mention.shop_id == 1 and mention.review_status == "pending"
        assert requires_manual_evidence_review(mention.extraction_error)
        candidates = db.query(ResolutionCandidate).filter_by(mention_id=1).order_by(ResolutionCandidate.rank).all()
        assert [(item.rank, item.name) for item in candidates] == [(1, "既存候補"), (2, "割烹みやび")]
        assert candidates[1].phone == "0312345678"
        assert db.get(Shop, 1).memo == "保存済みメモ"


def test_simultaneous_evaluations_apply_once_even_without_a_difference(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = Barrier(2, timeout=10)

    async def extract(_content: str) -> ExtractionCallResult:
        ready.wait()
        return _extraction()

    def run() -> evaluation_service.EvaluationResult:
        with Session(engine) as db:
            return asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", extract)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = workers.submit(run), workers.submit(run)
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert sorted(result.skipped for result in results) == [False, True]
    assert next(result for result in results if result.skipped).skip_reason == "concurrent_change"
    with Session(engine) as db:
        assert db.get(ShopMention, 1).version == 2
        assert db.get(ShopMention, 1).review_status == "approved"
        assert db.query(ShopMention).count() == 1
        assert db.query(ProcessingRun).filter_by(stage="evaluation_extraction").count() == 2
        assert db.query(ProcessingRun).filter_by(stage="evaluation_pipeline", status="ignored").count() == 1
