"""All restaurants, URLs, addresses, and official pages are synthetic fixtures."""

from __future__ import annotations

import asyncio
import html
import json
from collections.abc import Generator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bot.restaurant_extractor import (
    EventOrigin, ExtractedMention, ExtractedMessage, ExtractionCallResult,
    ExtractionError, FetchedHtmlDocument, ModelCallMetrics, extract_json_ld_candidates,
)
from db.models import Base, Message, Shop, ShopMention, SourceAsset
from services import identification_pipeline as pipeline
from services.identification_pipeline import MessageEnvelope, SourceAssetInput


MESSAGE_ID = "8436975355658244097"
PRIOR_ID = "8436975355658244096"
CHANNEL_ID = "8432348507410534512"
OFFICIAL_URL = "https://official.example/verification-shop/honten"
ADDRESS = "東京都千代田区神田鍛冶町1-2-3"
PHONE = "0312345678"


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _origin(branch: str = "本店", *, relation: str | None = None) -> EventOrigin:
    return EventOrigin(
        shop_name="検証亭", branch_name=branch, area="神田", is_unique=True,
        relation_evidence=relation or f"催事の出店元は「検証亭 {branch}」です。",
        permanent_evidence=f"常設店舗「検証亭 {branch}」は神田にあります。",
        permanent_source_url=OFFICIAL_URL,
    )


def _event(origin: EventOrigin | None) -> ExtractedMention:
    return ExtractedMention(
        shop_name="検証亭", branch_name="浦和催事会場", area="浦和", category="定食・食堂",
        subject_kind="event", identity_evidence="explicit", needs_review=False,
        name_evidence="検証亭", branch_evidence="浦和催事会場",
        confidence_reason="人工の催事抽出", event_origin=origin,
    )


def _content(origin: EventOrigin) -> str:
    return (
        "検証亭が浦和催事会場へ期間限定出店。会場電話は05000009999です。\n"
        f"{origin.relation_evidence}\n常設店舗の案内: {OFFICIAL_URL}"
    )


def _page(origin: EventOrigin, *, relation: str = "") -> FetchedHtmlDocument:
    structured = json.dumps({
        "@context": "https://schema.org", "@type": "Restaurant",
        "name": f"{origin.shop_name} {origin.branch_name}", "address": ADDRESS,
        "telephone": PHONE, "url": OFFICIAL_URL, "servesCuisine": "定食・食堂",
    }, ensure_ascii=False)
    return FetchedHtmlDocument(
        final_url=OFFICIAL_URL,
        html=(f"<html><body><h1>検証亭 {html.escape(origin.branch_name or '')}</h1>"
              f"<p>{html.escape(origin.permanent_evidence)}</p><p>{html.escape(relation)}</p>"
              f"<p>{ADDRESS}</p><p>{PHONE}</p>"
              f'<script type="application/ld+json">{structured}</script></body></html>'),
    )


def _seed_shop(db: Session, branch: str = "本店") -> Shop:
    shop = Shop(
        shop_name="検証亭", branch_name=branch, area="神田", category="定食・食堂",
        address=ADDRESS, phone=PHONE, canonical_url=OFFICIAL_URL,
        memo="利用者の既存メモ", rating=4, version=7,
    )
    db.add(ShopMention(
        message=Message(message_id=PRIOR_ID, content="以前の店舗記録", processing_status="succeeded"),
        shop=shop, occurrence_index=0, extracted_name="検証亭", extracted_branch_name=branch,
        review_status="approved", metadata_review_status="approved", resolution_status="resolved",
        resolution_method="manual", extraction_source="legacy_import", version=5,
    ))
    db.commit()
    return shop


def _run(
    db: Session, monkeypatch: pytest.MonkeyPatch, mention: ExtractedMention | None, content: str,
    pages: dict[str, FetchedHtmlDocument | Exception],
    *, allow_source_discovery: bool = False,
) -> tuple[pipeline.MessageProcessResult, list[str]]:
    fetched: list[str] = []

    async def extraction(_content: str) -> ExtractionCallResult:
        return ExtractionCallResult(
            ExtractedMessage(is_restaurant_message=True, mentions=[mention] if mention else []),
            ModelCallMetrics(model="synthetic", input_tokens=1, output_tokens=1,
                             web_search_calls=0, image_count=0, latency_ms=1, estimated_cost_microusd=0),
        )

    async def fetch(url: str) -> FetchedHtmlDocument:
        fetched.append(url)
        assert url in pages, f"Unexpected external request: {url}"
        response = pages[url]
        if isinstance(response, Exception):
            raise response
        return response

    async def structured(url: str):
        document = await fetch(url)
        return extract_json_ld_candidates(document.html, document.final_url)

    async def unexpected(*_args: object, **_kwargs: object):
        raise AssertionError("No external AI/search/image calls are permitted")

    monkeypatch.setattr(pipeline, "extract_restaurant_message", extraction)
    monkeypatch.setattr(pipeline, "fetch_html_document", fetch)
    monkeypatch.setattr(pipeline, "fetch_structured_candidates", structured)
    monkeypatch.setattr(pipeline, "search_restaurant_candidates", unexpected)
    monkeypatch.setattr(pipeline, "discover_restaurant_mentions", unexpected)
    monkeypatch.setattr(pipeline, "analyze_restaurant_images", unexpected)
    return asyncio.run(pipeline.process_message(
        db, _incoming(content), allow_source_discovery=allow_source_discovery,
    )), fetched


def _incoming(content: str) -> MessageEnvelope:
    return MessageEnvelope(
        message_id=MESSAGE_ID, channel_id=CHANNEL_ID, content=content,
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        assets=(SourceAssetInput(kind="link", url=OFFICIAL_URL),),
    )


@pytest.mark.parametrize("existing", [False, True])
def test_official_page_only_permanent_evidence_registers_the_origin(
    db: Session, monkeypatch: pytest.MonkeyPatch, existing: bool,
) -> None:
    origin = _origin()
    before = _seed_shop(db).id if existing else None
    content = _content(origin)
    assert origin.permanent_evidence not in content
    result, fetched = _run(db, monkeypatch, _event(origin), content, {OFFICIAL_URL: _page(origin)})
    shop = db.query(Shop).one()
    saved = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()
    assert result.shop_ids == (shop.id,) and not result.pending_mention_ids
    assert saved.shop_id == shop.id and saved.review_status == "approved"
    assert shop.area == "神田" and shop.address == ADDRESS and shop.phone == PHONE
    assert "浦和" not in shop.shop_name and "浦和" not in (shop.branch_name or "")
    assert fetched and set(fetched) == {OFFICIAL_URL}
    assert db.get(Message, MESSAGE_ID).content == content
    assert db.query(SourceAsset).filter_by(message_id=MESSAGE_ID).one().url == OFFICIAL_URL
    if existing:
        assert shop.id == before and shop.version == 7 and shop.memo == "利用者の既存メモ" and shop.rating == 4


@pytest.mark.parametrize("branch", ["本店", "神田店"])
def test_direct_event_announcement_with_venue_phrase_identifies_its_named_branch(
    db: Session, monkeypatch: pytest.MonkeyPatch, branch: str,
) -> None:
    relation = f"検証亭 {branch}が浦和の物産展に期間限定出店します。"
    origin = _origin(branch, relation=relation)
    shop = _seed_shop(db, branch)
    result, fetched = _run(db, monkeypatch, _event(origin), _content(origin), {OFFICIAL_URL: _page(origin)})
    assert result.shop_ids == (shop.id,)
    assert fetched == [OFFICIAL_URL]


@pytest.mark.parametrize("failure", [
    "relation_missing", "permanent_quote_missing", "fetch_failure", "redirect",
    "model_url_only", "same_host_unrelated_page", "negated_origin",
])
def test_unverified_event_origin_is_excluded_and_existing_store_is_untouched(
    db: Session, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    origin = _origin()
    shop = _seed_shop(db)
    content = _content(origin)
    page = _page(origin)
    pages: dict[str, FetchedHtmlDocument | Exception] = {OFFICIAL_URL: page}
    if failure == "relation_missing":
        origin = origin.model_copy(update={"relation_source_url": OFFICIAL_URL})
        content = f"浦和物産展で検証亭を紹介。常設店情報: {OFFICIAL_URL}"
    elif failure == "permanent_quote_missing":
        pages[OFFICIAL_URL] = FetchedHtmlDocument(html="<p>催事会場の案内だけです。</p>", final_url=OFFICIAL_URL)
    elif failure == "fetch_failure":
        pages[OFFICIAL_URL] = ExtractionError("synthetic page retrieval failure")
    elif failure == "redirect":
        pages[OFFICIAL_URL] = FetchedHtmlDocument(html=page.html, final_url="https://other.example/another-branch")
    elif failure in {"model_url_only", "same_host_unrelated_page"}:
        url = ("https://invented.example/assertion" if failure == "model_url_only"
               else "https://official.example/unrelated-author-post")
        origin = origin.model_copy(update={"permanent_source_url": url})
    else:
        relation = "催事の出店元は「検証亭 本店」ではなく、別の店舗です。"
        origin = origin.model_copy(update={"relation_evidence": relation})
        content = _content(origin)
    result, fetched = _run(db, monkeypatch, _event(origin), content, pages)
    saved = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()
    assert not result.shop_ids and not result.pending_mention_ids
    assert saved.shop_id is None and saved.review_status == "rejected"
    assert saved.difference_type == "event_excluded" and "event_excluded" in saved.extraction_error
    assert db.query(Shop).count() == 1 and shop.version == 7
    assert shop.memo == "利用者の既存メモ" and shop.rating == 4
    assert db.query(ShopMention).filter_by(message_id=PRIOR_ID).one().version == 5
    assert db.get(Message, MESSAGE_ID).content == content
    if failure in {"model_url_only", "same_host_unrelated_page"}:
        assert fetched == []
    else:
        assert fetched == [OFFICIAL_URL]


def test_cached_legacy_event_payload_without_origin_cannot_reuse_an_approved_store(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shop = _seed_shop(db)
    origin = _origin()
    content = f"浦和の催事の紹介。元店舗の情報: {OFFICIAL_URL}"
    evidence = pipeline._source_discovery_evidence(_incoming(content))
    assert evidence is not None
    legacy_payload = _event(None).model_copy(update={
        "branch_name": "本店", "area": "神田", "source_url": OFFICIAL_URL,
    }).model_dump(exclude={"event_origin"})
    pipeline._put_cache(db, "source_discovery", evidence.cache_value, json.dumps({
        "message": {"is_restaurant_message": True, "mentions": [legacy_payload]},
        "source_urls": [OFFICIAL_URL],
    }, ensure_ascii=False))
    db.commit()
    result, fetched = _run(
        db, monkeypatch, None, content, {OFFICIAL_URL: _page(origin)}, allow_source_discovery=True,
    )
    saved = db.query(ShopMention).filter_by(message_id=MESSAGE_ID).one()
    assert not result.shop_ids and not result.pending_mention_ids
    assert fetched == [OFFICIAL_URL]
    assert saved.shop_id is None and saved.difference_type == "event_excluded"
    assert db.query(Shop).count() == 1 and shop.version == 7
