from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bot.restaurant_extractor import (
    ExtractedMention, ExtractedMessage, ExtractionCallResult, ModelCallMetrics,
    ImageAnalysisResult, ImageClues,
    require_extraction_evidence,
)
from bot.sync_logic import build_message_envelope
from db.models import Base, Message, Shop, ShopMention, SourceAsset
from services import evaluation_service, identification_pipeline
from services.extraction_safety import (
    EvidenceCode, assess_mention_evidence, evidence_review_error,
    input_evidence_assessment, requires_manual_evidence_review,
)
from services.identification_pipeline import MessageEnvelope, SourceAssetInput
from services.mention_reevaluation import auto_resolve_pending_mentions


CURRENT_ID = "1436975355658244097"
PREVIOUS_ID = "1436975355658244096"
CHANNEL_ID = "1432348507410534512"


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def mention(name: str = "銀座鮨はな", **values: str | bool | None) -> ExtractedMention:
    payload: dict[str, str | bool | None] = {
        "shop_name": name, "area": "銀座", "category": "寿司・回転寿司",
        "needs_review": False, "confidence_reason": "人工の抽出結果",
        "subject_kind": "restaurant", "identity_evidence": "explicit",
        "name_evidence": f"「{name}」",
    }
    payload.update(values)
    return ExtractedMention.model_validate(payload)


def add_approved_shop(db: Session, name: str = "銀座鮨はな") -> tuple[Shop, ShopMention]:
    shop = Shop(shop_name=name, area="銀座", category="寿司・回転寿司", memo="保持する記録")
    old_message = Message(message_id=PREVIOUS_ID, content=f"「{name}」", processing_status="succeeded")
    source = ShopMention(
        message=old_message, shop=shop, occurrence_index=0, extracted_name=name,
        extracted_area="銀座", extracted_category="寿司・回転寿司",
        review_status="approved", resolution_status="resolved", metadata_review_status="approved",
        resolution_method="manual", extraction_source="test",
    )
    db.add(source)
    db.commit()
    return shop, source


def extract_result(*mentions: ExtractedMention) -> ExtractionCallResult:
    return ExtractionCallResult(
        ExtractedMessage(is_restaurant_message=True, mentions=list(mentions)),
        ModelCallMetrics(
            model="test", input_tokens=1, output_tokens=1, web_search_calls=0,
            image_count=0, latency_ms=1, estimated_cost_microusd=0,
        ),
    )


def use_extraction(monkeypatch: pytest.MonkeyPatch, *mentions: ExtractedMention) -> None:
    async def extracted(_content: str) -> ExtractionCallResult:
        return extract_result(*mentions)

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", extracted)
    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", extracted)


def envelope(content: str, assets: tuple[SourceAssetInput, ...] = ()) -> MessageEnvelope:
    return MessageEnvelope(
        message_id=CURRENT_ID, channel_id=CHANNEL_ID, content=content,
        created_at=datetime.now(timezone.utc), assets=assets,
    )


@pytest.mark.parametrize(
    ("extracted", "content", "code"),
    [
        (mention("ミナトヨガシテン", identity_evidence="author_only"), "[Embed Author] minatoyogashiten", EvidenceCode.IDENTITY_UNCLEAR),
        (mention("みなと道場", name_evidence="「みなと商店」"), "「みなと商店」へ行った", EvidenceCode.NAME_UNGROUNDED),
        (mention("麺屋88", name_evidence="「麺や八十八」"), "「麺や八十八」へ行った", EvidenceCode.NAME_UNGROUNDED),
        (mention("ゆめ", name_evidence="北町の民宿ゆめ"), "北町の民宿ゆめ", EvidenceCode.NAME_UNGROUNDED),
        (mention(branch_name="銀座店", branch_evidence=None), "「銀座鮨はな」。所在地は銀座", EvidenceCode.BRANCH_UNGROUNDED),
        (mention(name_evidence="[Embed Title] 銀座鮨はな (@food)"), "[Embed Title] 銀座鮨はな (@food)", EvidenceCode.NAME_UNGROUNDED),
        (mention("青空丸"), "舟盛り「青空丸」がおいしい", EvidenceCode.NON_STORE_SUBJECT),
        (mention("合成海鮮食堂", subject_kind="event"), "「合成海鮮食堂」が浦和の期間限定催事へ出店", EvidenceCode.NON_STORE_SUBJECT),
    ],
)
def test_unsupported_names_branches_and_subjects_require_review(
    extracted: ExtractedMention, content: str, code: EvidenceCode,
) -> None:
    result = assess_mention_evidence(extracted, content)
    assert code in result.codes
    assert requires_manual_evidence_review(evidence_review_error(result))


def test_unrelated_product_event_and_closed_words_do_not_block_explicit_store() -> None:
    text = "「銀座鮨はな」で食事。\n別件の商品名は「青空丸」。別の催事は閉店後に終了。"
    result = assess_mention_evidence(mention(), text)
    assert result.requires_review is False


@pytest.mark.parametrize("text", [
    "青空洋菓子店へ行った", "青空洋菓子店で食べた", "店名は青空洋菓子店です",
    "神田の青空洋菓子店でケーキを食べた", "昨日青空洋菓子店へ行った",
    "先日、麺や八十八で食べた",
])
def test_japanese_particles_after_full_store_name_are_valid_evidence(text: str) -> None:
    name = "麺や八十八" if "麺や八十八" in text else "青空洋菓子店"
    assert not assess_mention_evidence(mention(name, name_evidence=text), text).requires_review


@pytest.mark.parametrize("suffix", ["別館", "支店", "はなれ", "東京店", "二号店"])
def test_prefix_of_a_longer_shop_or_branch_is_not_name_evidence(suffix: str) -> None:
    text = f"神田の青空洋菓子店{suffix}へ行った"
    result = assess_mention_evidence(mention("青空洋菓子店", name_evidence=text), text)
    assert EvidenceCode.NAME_UNGROUNDED in result.codes


def test_short_name_inside_a_longer_proper_name_still_requires_review() -> None:
    text = "昨日北町の民宿ゆめで食事した"
    result = assess_mention_evidence(mention("ゆめ", name_evidence=text), text)
    assert EvidenceCode.NAME_UNGROUNDED in result.codes


def test_event_text_preserves_correct_reason_for_older_unclassified_extraction() -> None:
    text = "「合成海鮮食堂」が催事出店"
    result = assess_mention_evidence(mention("合成海鮮食堂", subject_kind=None), text)
    assert EvidenceCode.NON_STORE_SUBJECT in result.codes
    assert any("催事は登録対象外" in reason for reason in result.reasons)
    assert not any("商品への言及" in reason for reason in result.reasons)


@pytest.mark.parametrize("ending", ["続きを読む", "…続き", "...続きはこちら"])
def test_explicit_incomplete_text_markers_are_kept_for_review(ending: str) -> None:
    result = input_evidence_assessment(f"「銀座鮨はな」の紹介\n{ending}")
    assert result.codes == (EvidenceCode.INPUT_INCOMPLETE,)


def test_ellipsis_alone_is_not_proof_of_missing_text() -> None:
    assert not input_evidence_assessment("「銀座鮨はな」…また行きたい").requires_review


def test_api_output_without_safety_labels_cannot_silently_bypass_guard() -> None:
    legacy = mention(identity_evidence=None, subject_kind=None)
    result = require_extraction_evidence(ExtractedMessage(is_restaurant_message=True, mentions=[legacy]))
    assert assess_mention_evidence(result.mentions[0], "「銀座鮨はな」").requires_review


@pytest.mark.parametrize("kind", ["product", "event"])
def test_non_store_mention_does_not_link_even_to_an_exact_approved_name(
    db: Session, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    shop, source = add_approved_shop(db)
    use_extraction(monkeypatch, mention(subject_kind=kind))
    raw = "「銀座鮨はな」の紹介。"
    asset = SourceAssetInput(kind="attachment", url="https://example.com/original.pdf", title="原本")
    result = asyncio.run(identification_pipeline.process_message(db, envelope(raw, (asset,))))
    pending = db.query(ShopMention).filter_by(message_id=CURRENT_ID).one()
    assert pending is not None and pending.shop_id is None
    expected_status = "rejected" if kind == "event" else "pending"
    assert pending.review_status == expected_status
    assert pending.difference_type == ("event_excluded" if kind == "event" else "evidence_review")
    assert len(result.pending_mention_ids) == (0 if kind == "event" else 1)
    assert requires_manual_evidence_review(pending.extraction_error)
    assert db.query(Shop).count() == 1 and shop.memo == "保持する記録"
    assert db.get(Message, CURRENT_ID).content == raw
    assert db.query(SourceAsset).filter_by(message_id=CURRENT_ID).one().title == "原本"
    auto_resolve_pending_mentions(db, source_mention=source, target_shop=shop)
    db.flush()
    assert pending.review_status == expected_status and pending.shop_id is None


@pytest.mark.parametrize("status", ["closed", "event_ended"])
def test_operating_status_does_not_invalidate_supported_identity(
    db: Session, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    shop, _ = add_approved_shop(db)
    use_extraction(monkeypatch, mention(operating_status=status, operating_status_evidence="営業終了のお知らせ"))
    result = asyncio.run(identification_pipeline.process_message(
        db, envelope("店名「銀座鮨はな」。所在地は銀座。営業終了のお知らせ。")
    ))
    assert result.shop_ids == (shop.id,)
    saved = db.query(ShopMention).filter_by(message_id=CURRENT_ID).one()
    assert saved.review_status == "approved" and saved.extraction_error is None
    assert "営業状態の記述（店舗の同定とは別）" in saved.confidence_reason


def test_truncated_message_stays_pending_despite_exact_existing_identity(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_approved_shop(db)
    use_extraction(monkeypatch, mention())
    raw = "「銀座鮨はな」の紹介\n続きを読む"
    result = asyncio.run(identification_pipeline.process_message(db, envelope(raw)))
    assert not result.shop_ids
    saved = db.get(ShopMention, result.pending_mention_ids[0])
    assert saved is not None and "input_incomplete" in saved.extraction_error
    assert db.get(Message, CURRENT_ID).content == raw


def test_discovery_cannot_ground_a_name_in_a_truncated_metadata_excerpt(db: Session) -> None:
    source_url = "https://example.com/post"
    document = SourceAssetInput(
        kind="embed", url=source_url, title="店舗紹介",
        description="「銀座鮨はな」 銀座。" + "あ" * 8_000,
    )
    evidence = identification_pipeline._source_discovery_evidence(envelope(source_url, (document,)))
    assert evidence is not None and evidence.input_truncated
    grounded, reason = asyncio.run(identification_pipeline._source_mention_is_grounded(
        db, CURRENT_ID, mention(source_url=source_url), evidence, (source_url,),
    ))
    assert not grounded and "省略" in reason
    assert document.description.endswith("あ" * 8_000)


def test_bot_keeps_all_asset_metadata_and_marks_analysis_overflow(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachments = [
        SimpleNamespace(filename=f"evidence-{index}.pdf", content_type="application/pdf", url=f"https://example.com/{index}.pdf")
        for index in range(51)
    ]
    message = SimpleNamespace(
        id=int(CURRENT_ID), channel=SimpleNamespace(id=int(CHANNEL_ID)),
        content="「銀座鮨はな」", created_at=datetime.now(timezone.utc), embeds=[], attachments=attachments,
    )
    incoming = build_message_envelope(message)
    assert len(incoming.assets) == 51 and incoming.omitted_asset_count == 1
    use_extraction(monkeypatch, mention())
    result = asyncio.run(identification_pipeline.process_message(db, incoming))
    assert len(result.pending_mention_ids) == 1 and not result.shop_ids
    assert db.query(SourceAsset).filter_by(message_id=CURRENT_ID).count() == 51
    assert "evidence-50.pdf" in db.get(Message, CURRENT_ID).content


def test_bot_labels_embed_author_separately_from_description() -> None:
    embed = discord.Embed(url="https://example.com/post", title="紹介", description="「銀座鮨はな」へ")
    embed.set_author(name="投稿者の名前")
    message = SimpleNamespace(
        id=int(CURRENT_ID), channel=SimpleNamespace(id=int(CHANNEL_ID)), content="",
        created_at=datetime.now(timezone.utc), embeds=[embed], attachments=[],
    )
    incoming = build_message_envelope(message)
    assert "[Embed Author] 投稿者の名前" in incoming.content
    assert "[Embed Description] 「銀座鮨はな」へ" in incoming.content


def test_incomplete_embed_cannot_reuse_a_previously_approved_source() -> None:
    url = "https://x.com/synthetic_test/status/0000000000000000001"
    assert identification_pipeline.identify_source_url(url) is not None
    incoming = envelope(f"[Embed URL] {url}\n[Embed Description] 続きを読む", (SourceAssetInput(kind="link", url=url),))
    assert identification_pipeline._source_identity_for_reuse(incoming) is None


def test_direct_envelope_asset_overflow_cannot_reuse_approved_source() -> None:
    url = "https://x.com/synthetic_test/status/0000000000000000001"
    assert identification_pipeline.identify_source_url(url) is not None
    incoming = envelope(url, tuple(SourceAssetInput(kind="link", url=url) for _ in range(51)))
    assert incoming.omitted_asset_count == 0
    assert identification_pipeline._source_identity_for_reuse(incoming) is None


@pytest.mark.parametrize(("kind", "content", "reason"), [
    ("event", "店舗名は画像参照。", "画像に文字があります"),
    ("product", "店舗名は画像参照。", "画像に文字があります"),
    ("unknown", "店舗名は画像参照。", "画像に文字があります"),
    ("restaurant", "期間限定催事のチラシ。店舗名は画像参照。", "期間限定催事の紹介で常設店舗を特定できない"),
    ("restaurant", "商品の紹介。店舗名は画像参照。", "画像の対象が店舗か不明"),
])
def test_image_only_non_store_or_unknown_subject_stays_pending_with_matching_phone(
    db: Session, monkeypatch: pytest.MonkeyPatch, kind: str, content: str, reason: str,
) -> None:
    shop, source = add_approved_shop(db)
    shop.phone = "0312345678"
    db.commit()

    async def extracted(_content: str) -> ExtractionCallResult:
        result = extract_result()
        return ExtractionCallResult(result.message.model_copy(update={"unresolved_reason": reason}), result.metrics)

    async def image(_mention: ExtractedMention | None, _urls: tuple[str, ...]) -> ImageAnalysisResult:
        return ImageAnalysisResult(ImageClues.model_validate({
            "usable": True, "image_type": "flyer", "visible_shop_names": [shop.shop_name],
            "address_clues": [], "phone_clues": [shop.phone], "reason": "店名と電話番号を確認",
            "subject_kind": kind,
        }), extract_result().metrics)

    monkeypatch.setattr(identification_pipeline, "extract_restaurant_message", extracted)
    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image)
    asset = SourceAssetInput(kind="image", url="https://cdn.discordapp.com/attachments/1/2/flyer.jpg")
    result = asyncio.run(identification_pipeline.process_message(db, envelope(content, (asset,))))
    excluded = kind == "event" or "催事" in content
    assert not result.shop_ids and len(result.pending_mention_ids) == (0 if excluded else 1)
    pending = db.query(ShopMention).filter_by(message_id=CURRENT_ID).one()
    assert pending is not None and requires_manual_evidence_review(pending.extraction_error)
    expected_status = "rejected" if excluded else "pending"
    assert pending.shop_id is None and pending.review_status == expected_status
    assert db.get(Message, CURRENT_ID).content == content
    assert db.query(SourceAsset).filter_by(message_id=CURRENT_ID).count() == 1
    auto_resolve_pending_mentions(db, source_mention=source, target_shop=shop)
    db.flush()
    assert pending.shop_id is None and pending.review_status == expected_status


def test_image_only_closed_store_can_still_be_identified(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shop, _ = add_approved_shop(db)
    shop.phone = "0312345678"
    db.commit()
    use_extraction(monkeypatch)

    async def image(_mention: ExtractedMention | None, _urls: tuple[str, ...]) -> ImageAnalysisResult:
        return ImageAnalysisResult(ImageClues(
            usable=True, image_type="sign", visible_shop_names=[shop.shop_name],
            address_clues=[], phone_clues=[shop.phone], reason="看板に店名と電話番号を確認",
            subject_kind="restaurant", operating_status="closed", operating_status_evidence="閉店のお知らせ",
        ), extract_result().metrics)

    monkeypatch.setattr(identification_pipeline, "analyze_restaurant_images", image)
    asset = SourceAssetInput(kind="image", url="https://cdn.discordapp.com/attachments/1/2/closed.jpg")
    result = asyncio.run(identification_pipeline.process_message(db, envelope("閉店のお知らせ。店名は画像参照。", (asset,))))
    assert result.shop_ids == (shop.id,)
    saved = db.query(ShopMention).filter_by(message_id=CURRENT_ID).one()
    assert saved.review_status == "approved" and saved.extraction_error is None
    assert "営業状態の記述（店舗の同定とは別）: 閉店" in saved.confidence_reason


def test_legacy_evaluation_keeps_store_records_when_new_evidence_requires_review(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shop, old = add_approved_shop(db)
    old.extraction_source = "legacy_import"
    db.commit()
    use_extraction(monkeypatch, mention(subject_kind="product"))
    result = asyncio.run(evaluation_service.evaluate_imported_message(db, PREVIOUS_ID))
    assert result.differences == 1
    assert old.shop_id == shop.id and shop.memo == "保持する記録"
    assert old.review_status == "pending" and requires_manual_evidence_review(old.extraction_error)
    assert db.get(Message, PREVIOUS_ID).content == "「銀座鮨はな」"
