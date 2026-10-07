"""Restaurant locations, event venues, and web pages here are synthetic fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from bot.restaurant_extractor import (
    CandidateSearchResult, EventOrigin, ExtractedMention, ExtractedMessage,
    ExtractionCallResult, FetchedHtmlDocument, ImageAnalysisResult, ImageClues, ModelCallMetrics,
)
from db.models import (
    Base, Message, ResolutionCandidate, ReviewEvent, Shop, ShopMention, SourceAsset,
)
from services import evaluation_service, identification_pipeline
from services.identification_pipeline import MessageEnvelope, SourceAssetInput
from services.mention_reevaluation import auto_resolve_pending_mentions
from services.resolution import CandidateIdentity
from web.routers.review import review_queue


MESSAGE_ID = "7436975355658244097"
PRIOR_ID = "7436975355658244096"
CHANNEL_ID = "7432348507410534512"
OFFICIAL_URL = "https://official.example/yoshiya/honten"
POST_URL = "https://example.com/posts/event"
POSTER_URL = "https://cdn.discordapp.com/attachments/1/2/event.jpg"
ORIGIN_ADDRESS = "東京都千代田区神田鍛冶町1-2-3"
ORIGIN_PHONE = "0312345678"
RELATION = "催事の出店元は「合成海鮮食堂 本店」です。"
PERMANENT = "常設店舗「合成海鮮食堂 本店」は神田にあります。"


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(database)
    try:
        yield database
    finally:
        database.dispose()


def _metrics() -> ModelCallMetrics:
    return ModelCallMetrics(
        model="synthetic-event-extraction", input_tokens=10, output_tokens=5,
        web_search_calls=0, image_count=0, latency_ms=1, estimated_cost_microusd=0,
    )


def _result(*mentions: ExtractedMention) -> ExtractionCallResult:
    return ExtractionCallResult(
        ExtractedMessage(is_restaurant_message=True, mentions=list(mentions)), _metrics(),
    )


def _origin(*, unique: bool = True) -> EventOrigin:
    return EventOrigin(
        shop_name="合成海鮮食堂", branch_name="本店", area="神田", is_unique=unique,
        relation_evidence=RELATION, permanent_evidence=PERMANENT,
        permanent_source_url=OFFICIAL_URL,
    )


def _event(venue: str, origin: EventOrigin | None = None) -> ExtractedMention:
    return ExtractedMention(
        shop_name="合成海鮮食堂", branch_name=f"{venue}物産展会場", area=venue, category="海鮮・刺身",
        needs_review=False, confidence_reason="人工の期間限定催事の投稿",
        subject_kind="event", identity_evidence="explicit", name_evidence="「合成海鮮食堂」",
        branch_evidence=f"会場は{venue}物産展会場です。", event_origin=origin,
    )


def _content(venue: str, *, with_origin: bool = False) -> str:
    text = (
        f"「合成海鮮食堂」が{venue}の期間限定催事へ出店。会場は{venue}物産展会場です。"
        f"会場案内の電話番号は05000009999です。 {POST_URL}"
    )
    return f"{text}\n{RELATION}\n{PERMANENT}" if with_origin else text


def _envelope(content: str, *, message_id: str = MESSAGE_ID, image_only: bool = False) -> MessageEnvelope:
    assets = (SourceAssetInput(kind="image", url=POSTER_URL, title="期間限定催事の原添付"),)
    if not image_only:
        assets += (SourceAssetInput(kind="link", url=OFFICIAL_URL, title="常設店舗の公式情報"),)
    return MessageEnvelope(
        message_id=message_id, channel_id=CHANNEL_ID, content=content,
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc), assets=assets,
    )


def _use_extraction(monkeypatch: pytest.MonkeyPatch, *mentions: ExtractedMention) -> None:
    async def extracted(_content: str) -> ExtractionCallResult:
        return _result(*mentions)

    async def structured(url: str) -> list[CandidateIdentity]:
        assert url == OFFICIAL_URL
        return [CandidateIdentity(
            name="合成海鮮食堂 本店", area="神田", category="海鮮・刺身",
            address=ORIGIN_ADDRESS, phone=ORIGIN_PHONE,
            canonical_url=OFFICIAL_URL, evidence_url=OFFICIAL_URL,
        )]

    async def unexpected_search(_mention: ExtractedMention) -> CandidateSearchResult:
        raise AssertionError("Synthetic source evidence should resolve the origin or exclude the event")

    async def unexpected_image(
        _mention: ExtractedMention | None, _urls: tuple[str, ...],
    ) -> ImageAnalysisResult:
        raise AssertionError("Text event fixtures do not require image inference")

    async def official_html(url: str) -> FetchedHtmlDocument:
        assert url == OFFICIAL_URL
        return FetchedHtmlDocument(
            html=f"<html><body><main><h1>合成海鮮食堂 本店</h1><p>{PERMANENT}</p>"
                 f"<p>{ORIGIN_ADDRESS}</p><p>{ORIGIN_PHONE}</p></main></body></html>",
            final_url=OFFICIAL_URL,
        )

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", extracted)
    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", extracted)
    monkeypatch.setattr(identification_pipeline, "fetch_structured_candidates", structured)
    monkeypatch.setattr(identification_pipeline, "search_restaurant_candidates", unexpected_search)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", unexpected_image)
    monkeypatch.setattr(evaluation_service, "analyze_restaurant_images", unexpected_image)
    monkeypatch.setattr(identification_pipeline, "fetch_html_document", official_html)


def _add_permanent_shop(
    db: Session, *, name: str = "合成海鮮食堂", branch: str | None = "本店",
    message_id: str = PRIOR_ID, with_history: bool = True,
) -> tuple[Shop, ShopMention]:
    shop = Shop(
        shop_name=name, branch_name=branch, area="神田", category="海鮮・刺身",
        address=ORIGIN_ADDRESS, phone=ORIGIN_PHONE, canonical_url=OFFICIAL_URL,
        memo="利用者が保存した訪問メモ", is_visited=True,
        visited_at=datetime(2025, 1, 2, tzinfo=timezone.utc), rating=4, version=7,
    )
    source = ShopMention(
        shop=shop, message=Message(message_id=message_id, content=f"常設店舗「{name} {branch or ''}」で食事。",
                                   processing_status="succeeded"),
        occurrence_index=0, extracted_name=name, extracted_branch_name=branch,
        extracted_area="神田", extracted_category="海鮮・刺身", source_url=POST_URL,
        review_status="approved", metadata_review_status="approved", resolution_status="resolved",
        resolution_method="manual", extraction_source="legacy_import", version=5,
        reviewed_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
    )
    db.add(source)
    db.flush()
    db.add(SourceAsset(message_id=message_id, kind="attachment", url="https://example.com/prior.pdf"))
    db.add(ResolutionCandidate(mention_id=source.id, rank=1, name=name, provenance="structured_data"))
    if with_history:
        db.add(ReviewEvent(mention_id=source.id, action="approve_current", note="既存の手動確認履歴"))
    db.commit()
    return shop, source


@dataclass(frozen=True)
class _ProtectedState:
    shop: tuple[object, ...]
    mention: tuple[object, ...]
    message: tuple[object, ...]
    assets: tuple[tuple[object, ...], ...]
    history: tuple[tuple[object, ...], ...]
    candidates: tuple[tuple[object, ...], ...]


def _protected_state(db: Session, shop_id: int, mention_id: int, message_id: str) -> _ProtectedState:
    return _ProtectedState(
        shop=tuple(db.execute(select(Shop.__table__).where(Shop.id == shop_id)).one()),
        mention=tuple(db.execute(select(ShopMention.__table__).where(ShopMention.id == mention_id)).one()),
        message=tuple(db.execute(select(Message.__table__).where(Message.message_id == message_id)).one()),
        assets=tuple(tuple(row) for row in db.execute(
            select(SourceAsset.__table__).where(SourceAsset.message_id == message_id).order_by(SourceAsset.id)
        )),
        history=tuple(tuple(row) for row in db.execute(
            select(ReviewEvent.__table__).where(ReviewEvent.mention_id == mention_id).order_by(ReviewEvent.id)
        )),
        candidates=tuple(tuple(row) for row in db.execute(
            select(ResolutionCandidate.__table__).where(ResolutionCandidate.mention_id == mention_id).order_by(ResolutionCandidate.id)
        )),
    )


def _assert_no_open_queue_item(db: Session, mention_id: int) -> None:
    request = Request({"type": "http", "session": {"admin_authenticated": True}, "headers": []})
    for scope in ("identity", "metadata"):
        for status in ("pending", "deferred"):
            queue = review_queue(
                request, Response(), scope=scope, status=status, cursor=None, limit=50, db=db,
            )
            assert mention_id not in {item.id for item in queue.items}


def _assert_excluded(db: Session, message_id: str, raw: str) -> ShopMention:
    saved = db.query(ShopMention).filter_by(message_id=message_id).one()
    assert saved.shop_id is None
    assert saved.review_status == "rejected" and saved.resolution_status == "invalid"
    assert saved.difference_type == "event_excluded"
    assert saved.extraction_error is not None and "event_excluded" in saved.extraction_error
    assert db.get(Message, message_id).content == raw
    assert db.query(SourceAsset).filter_by(message_id=message_id, url=POSTER_URL).count() == 1
    _assert_no_open_queue_item(db, saved.id)
    return saved


@pytest.mark.parametrize("venue", ["浦和", "京都"])
def test_verified_event_origin_creates_permanent_shop_without_venue_branch(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, venue: str,
) -> None:
    _use_extraction(monkeypatch, _event(venue, _origin()))
    raw = _content(venue, with_origin=True)
    with Session(engine) as db:
        result = asyncio.run(identification_pipeline.process_message(db, _envelope(raw)))
        assert len(result.shop_ids) == 1 and not result.pending_mention_ids
    with Session(engine) as verified:
        shop = verified.query(Shop).one()
        saved = verified.query(ShopMention).one()
        assert shop.shop_name == "合成海鮮食堂" and shop.branch_name == "本店"
        assert shop.area == "神田" and shop.address == ORIGIN_ADDRESS and shop.phone == ORIGIN_PHONE
        assert shop.canonical_url == OFFICIAL_URL
        assert shop.area != venue and shop.branch_name != f"{venue}物産展会場"
        assert saved.shop_id == shop.id and saved.review_status == "approved"
        assert verified.get(Message, MESSAGE_ID).content == raw
        assert verified.query(SourceAsset).filter_by(message_id=MESSAGE_ID, url=POSTER_URL).count() == 1


def test_ambiguous_branch_origin_does_not_choose_existing_head_office(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(engine) as db:
        first, source = _add_permanent_shop(db)
        second, second_source = _add_permanent_shop(db, branch="西口店", message_id="7436975355658244095")
        before = (_protected_state(db, first.id, source.id, PRIOR_ID),
                  _protected_state(db, second.id, second_source.id, second_source.message_id))
        _use_extraction(monkeypatch, _event("浦和", _origin(unique=False)))
        raw = _content("浦和", with_origin=True) + "ただし出店元が本店か西口店かは未確認です。"
        result = asyncio.run(identification_pipeline.process_message(db, _envelope(raw)))
        assert not result.shop_ids and not result.pending_mention_ids
        _assert_excluded(db, MESSAGE_ID, raw)
        assert db.query(Shop).count() == 2
        assert before == (_protected_state(db, first.id, source.id, PRIOR_ID),
                          _protected_state(db, second.id, second_source.id, second_source.message_id))


def test_event_without_permanent_origin_is_excluded_with_original_evidence(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_extraction(monkeypatch, _event("京都"))
    raw = _content("京都") + "常設店舗はなく、今回だけの催事企画です。"
    with Session(engine) as db:
        result = asyncio.run(identification_pipeline.process_message(db, _envelope(raw)))
        assert not result.shop_ids and not result.pending_mention_ids
        _assert_excluded(db, MESSAGE_ID, raw)
        assert db.query(Shop).count() == 0


@pytest.mark.parametrize("venue", ["浦和", "京都"])
def test_known_permanent_origin_links_without_changing_saved_user_records(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, venue: str,
) -> None:
    with Session(engine) as db:
        shop, source = _add_permanent_shop(db)
        before = _protected_state(db, shop.id, source.id, PRIOR_ID)
        origin = _origin()
        if venue == "浦和":
            origin = origin.model_copy(update={"permanent_source_url": None})
        _use_extraction(monkeypatch, _event(venue, origin))
        raw = _content(venue, with_origin=True)
        result = asyncio.run(identification_pipeline.process_message(db, _envelope(raw)))
        assert result.shop_ids == (shop.id,) and not result.pending_mention_ids
        assert db.query(Shop).count() == 1
        assert _protected_state(db, shop.id, source.id, PRIOR_ID) == before
        saved = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()
        assert saved.shop_id == shop.id and saved.review_status == "approved"
        assert db.get(Message, MESSAGE_ID).content == raw


def test_mixed_message_keeps_explicit_permanent_restaurant_and_excludes_unresolved_event(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(engine) as db:
        shop, source = _add_permanent_shop(db, name="神田食堂", branch=None)
        before = _protected_state(db, shop.id, source.id, PRIOR_ID)
        restaurant = ExtractedMention(
            shop_name="神田食堂", area="神田", category="海鮮・刺身", needs_review=False,
            confidence_reason="本文で常設店に言及", subject_kind="restaurant",
            identity_evidence="explicit", name_evidence="「神田食堂」",
        )
        _use_extraction(monkeypatch, restaurant, _event("浦和"))
        raw = "常設店舗「神田食堂」で食事。所在地は神田です。\n" + _content("浦和")
        result = asyncio.run(identification_pipeline.process_message(db, _envelope(raw)))
        assert result.shop_ids == (shop.id,) and not result.pending_mention_ids
        assert db.query(Shop).count() == 1
        rows = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).order_by(ShopMention.occurrence_index).all()
        assert len(rows) == 2
        assert (rows[0].shop_id, rows[0].review_status) == (shop.id, "approved")
        assert (rows[1].shop_id, rows[1].review_status, rows[1].difference_type) == (None, "rejected", "event_excluded")
        _assert_no_open_queue_item(db, rows[1].id)
        assert _protected_state(db, shop.id, source.id, PRIOR_ID) == before


def test_image_event_does_not_link_from_matching_name_and_phone_alone(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(engine) as db:
        shop, source = _add_permanent_shop(db)
        before = _protected_state(db, shop.id, source.id, PRIOR_ID)
        _use_extraction(monkeypatch)

        async def image(_mention: ExtractedMention | None, _urls: tuple[str, ...]) -> ImageAnalysisResult:
            return ImageAnalysisResult(
                ImageClues(usable=True, image_type="flyer", subject_kind="event",
                           visible_shop_names=["合成海鮮食堂 本店"], address_clues=[], phone_clues=[ORIGIN_PHONE],
                           reason="催事のチラシ。常設の出店元は特定できない"), _metrics(),
            )

        monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image)
        raw = "浦和の期間限定物産展のチラシ。店名は画像にあります。"
        result = asyncio.run(identification_pipeline.process_message(db, _envelope(raw, image_only=True)))
        assert not result.shop_ids and not result.pending_mention_ids
        _assert_excluded(db, MESSAGE_ID, raw)
        assert _protected_state(db, shop.id, source.id, PRIOR_ID) == before


def test_excluded_event_is_not_revived_by_retries_or_automatic_reevaluation(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(engine) as db:
        shop, source = _add_permanent_shop(db)
        _use_extraction(monkeypatch, _event("浦和"))
        raw = _content("浦和")
        incoming = _envelope(raw)
        asyncio.run(identification_pipeline.process_message(db, incoming))
        excluded = _assert_excluded(db, MESSAGE_ID, raw)
        saved_id, version = excluded.id, excluded.version
        repeated = asyncio.run(identification_pipeline.process_message(db, incoming))
        assert not repeated.shop_ids and not repeated.pending_mention_ids
        resolution = auto_resolve_pending_mentions(db, source_mention=source, target_shop=shop)
        db.commit()
        assert not resolution.mention_ids
        guessed_store = ExtractedMention(
            shop_name="合成海鮮食堂", branch_name="本店", area="神田", category="海鮮・刺身",
            needs_review=False, confidence_reason="催事を誤って常設店に解釈する人工応答",
            subject_kind="restaurant", identity_evidence="explicit", name_evidence="「合成海鮮食堂」",
        )
        _use_extraction(monkeypatch, guessed_store)
        evaluated = asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))
        assert evaluated.skipped
        excluded = _assert_excluded(db, MESSAGE_ID, raw)
        assert (excluded.id, excluded.version) == (saved_id, version)
        assert db.query(Shop).count() == 1


@pytest.mark.parametrize("with_history", [True, False])
def test_event_reassessment_never_revokes_an_existing_approved_store(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, with_history: bool,
) -> None:
    with Session(engine) as db:
        shop, source = _add_permanent_shop(db, with_history=with_history)
        source.message.content = _content("京都")
        db.commit()
        before = _protected_state(db, shop.id, source.id, PRIOR_ID)
        _use_extraction(monkeypatch, _event("京都"))
        result = asyncio.run(evaluation_service.evaluate_imported_message(db, PRIOR_ID))
        assert result.skipped
        assert result.skip_reason in {"human_reviewed", "event_reassessment_preserved"}
        assert _protected_state(db, shop.id, source.id, PRIOR_ID) == before


@pytest.mark.parametrize("entry", ["process", "evaluate"])
def test_two_events_for_one_new_origin_create_one_permanent_shop(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, entry: str,
) -> None:
    _use_extraction(monkeypatch, _event("浦和", _origin()), _event("京都", _origin()))
    raw = _content("浦和", with_origin=True) + "\n" + _content("京都", with_origin=True)
    incoming = _envelope(raw)
    with Session(engine) as db:
        if entry == "process":
            result = asyncio.run(identification_pipeline.process_message(db, incoming))
            assert len(set(result.shop_ids)) == 1 and not result.pending_mention_ids
        else:
            message = Message(message_id=MESSAGE_ID, content=raw)
            message.assets = [SourceAsset(**asset.model_dump()) for asset in incoming.assets]
            db.add(message)
            db.commit()
            evaluated = asyncio.run(evaluation_service.evaluate_imported_message(db, MESSAGE_ID))
            assert not evaluated.skipped and evaluated.new_mentions == 2
        shop = db.query(Shop).one()
        rows = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).all()
        assert len(rows) == 2
        assert {row.shop_id for row in rows} == {shop.id}
        assert {row.review_status for row in rows} == {"approved"}
        assert (shop.shop_name, shop.branch_name, shop.area) == ("合成海鮮食堂", "本店", "神田")
        assert db.get(Message, MESSAGE_ID).content == raw
