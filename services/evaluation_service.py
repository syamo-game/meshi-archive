from __future__ import annotations

import logging
from contextvars import Token
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from bot.restaurant_extractor import (
    ExtractedMention,
    ExtractedMessage,
    ImageAnalysisResult,
    analyze_restaurant_images,
    extract_restaurant_message,
)
from db.models import (
    AssetKind,
    Message,
    MetadataReviewStatus,
    ProcessingRun,
    ProcessingStatus,
    ResolutionCandidate,
    ResolutionBasis,
    ResolutionMethod,
    ResolutionStatus,
    ReviewStatus,
    ReviewEvent,
    Shop,
    ShopMention,
    SourceAsset,
    utc_now,
)
from services.identification_pipeline import (
    PipelineCandidate,
    SourceAssetInput,
    _ACTIVE_OPERATIONAL_JOURNAL,
    _OperationalJournal,
    _EventOriginResolution,
    _event_origin_mention_row,
    _prepare_event_origin,
    _candidate_for_new_shop,
    _candidate_verification_urls,
    _candidates_from_image,
    _dedupe_candidates,
    _find_existing_shop,
    _find_existing_shop_from_mention,
    _guard_new_shop_creation_or_link,
    _is_discord_image,
    _metadata_review_state,
    _name_with_branch,
    _record_metrics,
    _record_failure,
    _persist_operational_journal,
    _source_urls_from_assets,
    _store_candidates,
    _structured_candidates,
    _web_candidates,
)
from services.shop_creation_lock import lock_new_shop_creation
from services.resolution import (
    CandidateIdentity,
    branches_conflict,
    evaluate_identity,
    name_similarity,
    normalize_address,
    normalize_area,
    normalize_phone,
)
from services.extraction_safety import (
    EVENT_EXCLUDED,
    EvidenceAssessment,
    assess_mention_evidence,
    evidence_review_error,
    image_evidence_assessment,
    input_evidence_assessment,
    is_event_excluded_registration,
    operating_status_note,
    requires_manual_evidence_review,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluationResult:
    message_id: str
    matched_mentions: int
    new_mentions: int
    differences: int
    skipped: bool
    skip_reason: str | None = None


class _EvaluationChanged(Exception):
    pass


@dataclass(frozen=True)
class _EvaluationSnapshot:
    content: str | None
    fetch_error: str | None
    source_created_at: datetime | None
    processing_status: str
    processed_at: datetime | None
    assets: tuple[tuple[int, SourceAssetInput], ...]
    mentions: tuple[tuple[int, int, int | None, int], ...]
    shops: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class _PreparedCandidates:
    candidates: tuple[PipelineCandidate, ...] = ()
    image_assessment: EvidenceAssessment = EvidenceAssessment()
    event_origin: _EventOriginResolution | None = None


def _load_evaluation(
    db: Session, message_id: str, *, for_update: bool = False,
) -> tuple[Message, list[ShopMention]]:
    message_query = db.query(Message).filter(Message.message_id == message_id)
    mention_query = (
        db.query(ShopMention)
        .filter(ShopMention.message_id == message_id)
        .order_by(ShopMention.id)
    )
    asset_query = (
        db.query(SourceAsset)
        .filter(SourceAsset.message_id == message_id)
        .order_by(SourceAsset.id)
    )
    if for_update:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(
                text("UPDATE messages SET message_id = message_id WHERE message_id = :id"),
                {"id": message_id},
            )
        message_query = message_query.with_for_update()
        mention_query = mention_query.with_for_update()
        asset_query = asset_query.with_for_update()
    message = message_query.populate_existing().first()
    if message is None:
        raise _EvaluationChanged("message_removed")
    mentions = mention_query.populate_existing().all()
    asset_query.populate_existing().all()
    db.expire(message, ["assets", "mentions"])
    shop_ids = {mention.shop_id for mention in mentions if mention.shop_id is not None}
    shops = db.query(Shop).filter(Shop.id.in_(shop_ids)).order_by(Shop.id)
    if for_update:
        shops = shops.with_for_update()
    shops.populate_existing().all()
    for mention in mentions:
        db.expire(mention, ["shop"])
    return message, mentions


def _evaluation_snapshot(
    message: Message, mentions: list[ShopMention],
) -> _EvaluationSnapshot:
    return _EvaluationSnapshot(
        content=message.content,
        fetch_error=message.fetch_error,
        source_created_at=message.source_created_at,
        processing_status=message.processing_status,
        processed_at=message.processed_at,
        assets=tuple(
            (asset.id, SourceAssetInput(
                kind=asset.kind, url=asset.url, title=asset.title,
                description=asset.description, mime_type=asset.mime_type,
            ))
            for asset in sorted(message.assets, key=lambda asset: asset.id)
        ),
        mentions=tuple(
            (mention.id, mention.version, mention.shop_id, mention.occurrence_index)
            for mention in mentions
        ),
        shops=tuple(sorted({
            (mention.shop.id, mention.shop.version)
            for mention in mentions if mention.shop is not None
        })),
    )


def _human_reviewed(db: Session, mentions: list[ShopMention]) -> bool:
    # Legacy imports inferred manual approval; review history distinguishes actual decisions.
    if any(
        mention.resolution_method == ResolutionMethod.MANUAL.value
        and mention.extraction_source != "legacy_import"
        for mention in mentions
    ):
        return True
    return (
        db.query(ReviewEvent.id)
        .filter(ReviewEvent.mention_id.in_([mention.id for mention in mentions]))
        .first()
    ) is not None


def _lock_evaluation(
    db: Session, message_id: str, expected: _EvaluationSnapshot,
) -> tuple[Message, list[ShopMention]]:
    # External fetches finish before this short transaction acquires row locks.
    lock_new_shop_creation(db)
    message, mentions = _load_evaluation(db, message_id, for_update=True)
    if _evaluation_snapshot(message, mentions) != expected:
        raise _EvaluationChanged("concurrent_change")
    if _human_reviewed(db, mentions):
        raise _EvaluationChanged("human_reviewed")
    return message, mentions


def _persist_evaluation_journal(db: Session) -> None:
    journal = _ACTIVE_OPERATIONAL_JOURNAL.get()
    if journal is None:
        return
    token = _ACTIVE_OPERATIONAL_JOURNAL.set(None)
    try:
        _persist_operational_journal(db, journal)
    finally:
        _ACTIVE_OPERATIONAL_JOURNAL.reset(token)


def _skip_evaluation(db: Session, message_id: str, reason: str) -> EvaluationResult:
    db.rollback()
    if db.get(Message, message_id) is None:
        logger.info("Evaluation skipped after message removal: message_id=%s", message_id)
        return EvaluationResult(message_id, 0, 0, 0, True, "message_removed")
    _persist_evaluation_journal(db)
    db.add(ProcessingRun(
        message_id=message_id, stage="evaluation_pipeline",
        status=ProcessingStatus.IGNORED.value, error=reason,
    ))
    db.commit()
    return EvaluationResult(message_id, 0, 0, 0, True, reason)


def _asset_inputs(message: Message) -> tuple[SourceAssetInput, ...]:
    return tuple(
        SourceAssetInput(
            kind=asset.kind,
            url=asset.url,
            title=asset.title,
            description=asset.description,
            mime_type=asset.mime_type,
        )
        for asset in message.assets
    )


def _match_legacy_mention(
    extracted: ExtractedMention,
    unmatched: list[ShopMention],
) -> ShopMention | None:
    ranked = sorted(
        (
            (name_similarity(extracted.shop_name, mention.shop.shop_name), mention)
            for mention in unmatched
            if mention.shop is not None
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.8:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.05:
        return None
    return ranked[0][1]


async def _collect_candidates(
    db: Session,
    message: Message,
    extracted: ExtractedMention,
    assets: tuple[SourceAssetInput, ...],
) -> list[PipelineCandidate]:
    _source_url, canonical_hint, _canonical_assets_ambiguous = (
        _source_urls_from_assets(assets)
    )
    structured_urls: list[str] = []
    if canonical_hint:
        structured_urls.append(canonical_hint)
    for asset in assets:
        if asset.kind in {AssetKind.LINK.value, AssetKind.EMBED.value}:
            if asset.url not in structured_urls:
                structured_urls.append(asset.url)
    candidates: list[PipelineCandidate] = []
    for url in structured_urls[:10]:
        candidates.extend(await _structured_candidates(db, message.message_id, url))
    existing_shop, _existing_reason = _find_existing_shop(db, extracted, candidates)
    automatic_candidate = (
        None
        if existing_shop
        else _candidate_for_new_shop(
            extracted,
            candidates,
        )
    )
    if existing_shop is None and automatic_candidate is None:
        web_batch = await _web_candidates(db, message.message_id, extracted)
        candidates.extend(web_batch.candidates)
        for verification_url in _candidate_verification_urls(
            extracted,
            web_batch.candidates,
        ):
            candidates.extend(
                await _structured_candidates(db, message.message_id, verification_url)
            )
    return _dedupe_candidates(candidates)


def _identity_candidate_differs(
    shop: Shop,
    candidate: CandidateIdentity | None,
) -> bool:
    if candidate is None:
        return False
    shop_name = _name_with_branch(shop.shop_name, shop.branch_name)
    if candidate.name and (
        name_similarity(shop_name, candidate.name) < 0.9
        or branches_conflict(shop_name, candidate.name)
    ):
        return True
    comparisons = (
        (
            normalize_area(candidate.area) if candidate.area else None,
            normalize_area(shop.area),
        ),
        (normalize_address(candidate.address), normalize_address(shop.address)),
        (normalize_phone(candidate.phone), normalize_phone(shop.phone)),
        (candidate.canonical_url, shop.canonical_url),
        (candidate.external_source, shop.external_source),
        (candidate.external_id, shop.external_id),
    )
    return any(proposed is not None and proposed != current for proposed, current in comparisons)


def _metadata_candidate_differs(
    shop: Shop,
    candidate: CandidateIdentity | None,
) -> bool:
    return bool(
        candidate is not None
        and candidate.category is not None
        and candidate.category != shop.category
    )


def _strong_candidate_for_shop(
    shop: Shop,
    candidates: list[PipelineCandidate],
) -> PipelineCandidate | None:
    existing = CandidateIdentity(
        name=_name_with_branch(shop.shop_name, shop.branch_name),
        area=shop.area,
        category=shop.category,
        address=shop.address,
        phone=shop.phone,
        canonical_url=shop.canonical_url,
        external_source=shop.external_source,
        external_id=shop.external_id,
        evidence_url=shop.canonical_url,
    )
    matching = [
        candidate
        for candidate in candidates
        if candidate.is_verified and evaluate_identity(existing, candidate).is_strong_match
    ]
    if not matching:
        return None
    return max(
        matching,
        key=lambda candidate: sum(
            value is not None
            for value in (
                candidate.external_id,
                candidate.phone,
                candidate.address,
                candidate.canonical_url,
            )
        ),
    )


def _identity_extraction_differs(shop: Shop, extracted: ExtractedMention) -> bool:
    shop_name = _name_with_branch(shop.shop_name, shop.branch_name)
    extracted_name = _name_with_branch(extracted.shop_name, extracted.branch_name)
    if (
        name_similarity(shop_name, extracted_name) < 0.9
        or branches_conflict(shop_name, extracted_name)
    ):
        return True
    return bool(
        extracted.area
        and normalize_area(extracted.area) != normalize_area(shop.area)
    )


def _metadata_extraction_differs(shop: Shop, extracted: ExtractedMention) -> bool:
    return bool(extracted.category and extracted.category != shop.category)


def _mark_difference(mention: ShopMention, difference_type: str) -> None:
    mention.review_status = ReviewStatus.PENDING.value
    if mention.difference_type not in {"legacy_review", "source_unavailable"}:
        mention.difference_type = difference_type
    mention.version += 1


def _mark_metadata_difference(mention: ShopMention, difference_type: str) -> None:
    mention.metadata_review_status = "pending"
    mention.metadata_difference_type = difference_type
    mention.version += 1


async def _prepare_candidates(
    db: Session, message: Message, extracted_message: ExtractedMessage,
    assets: tuple[SourceAssetInput, ...], mentions: list[ShopMention],
) -> tuple[_PreparedCandidates, ...]:
    prepared: list[_PreparedCandidates] = []
    unmatched = list(mentions)
    input_assessment = input_evidence_assessment(
        message.content or "", omitted_asset_count=max(0, len(assets) - 50),
    )
    _source_url, canonical_hint, canonical_assets_ambiguous = _source_urls_from_assets(assets)
    image_urls = tuple(asset.url for asset in assets if _is_discord_image(asset))
    image_result: ImageAnalysisResult | None = None
    for extracted in extracted_message.mentions:
        mention = _match_legacy_mention(extracted, unmatched)
        if mention is not None:
            unmatched.remove(mention)
        safety = assess_mention_evidence(
            extracted, message.content or "", input_assessment=input_assessment,
        )
        if safety.is_event_excluded:
            origin = await _prepare_event_origin(
                db, message.message_id, extracted, message.content or "",
                assets=assets, input_assessment=input_assessment,
            )
            prepared.append(_PreparedCandidates(event_origin=origin))
            continue
        if safety.requires_review or (
            mention is not None
            and mention.review_status != ReviewStatus.APPROVED.value
            and requires_manual_evidence_review(mention.extraction_error)
        ):
            prepared.append(_PreparedCandidates())
            continue
        if canonical_assets_ambiguous:
            existing_shop, blocked = None, None
        else:
            existing_shop, blocked = _find_existing_shop_from_mention(
                db, extracted, canonical_hint if len(extracted_message.mentions) == 1 else None,
            )
        candidates: list[PipelineCandidate] = []
        automatic_candidate: PipelineCandidate | None = None
        image_assessment = EvidenceAssessment()
        if existing_shop is None and blocked is None:
            candidates = await _collect_candidates(db, message, extracted, assets)
            if canonical_assets_ambiguous:
                existing_shop, blocked = None, None
            else:
                existing_shop, blocked = _find_existing_shop(db, extracted, candidates)
            if not (canonical_assets_ambiguous or existing_shop or blocked):
                automatic_candidate = _candidate_for_new_shop(extracted, candidates)
        if (
            existing_shop is None and automatic_candidate is None and blocked is None
            and image_urls and not canonical_assets_ambiguous
            and len(extracted_message.mentions) == 1
        ):
            if image_result is None:
                try:
                    image_result = await analyze_restaurant_images(extracted, image_urls)
                except Exception as exc:
                    _record_failure(db, message.message_id, "evaluation_image", exc)
                    raise
                _record_metrics(db, message.message_id, "evaluation_image", image_result.metrics)
            image_assessment = image_evidence_assessment(
                image_result.clues.subject_kind,
                input_assessment=input_assessment,
            )
            if image_assessment.is_event_excluded:
                event = extracted.model_copy(update={
                    "subject_kind": "event", "event_origin": image_result.clues.event_origin,
                })
                origin = await _prepare_event_origin(
                    db, message.message_id, event, message.content or "",
                    assets=assets, input_assessment=input_assessment,
                )
                image_candidates = _candidates_from_image(image_result, image_urls[0])
                origin = replace(
                    origin, extracted=origin.extracted or event,
                    candidates=tuple(_dedupe_candidates([*origin.candidates, *image_candidates])),
                )
                prepared.append(_PreparedCandidates(image_assessment=image_assessment, event_origin=origin))
                continue
            candidates.extend(_candidates_from_image(image_result, image_urls[0]))
        prepared.append(_PreparedCandidates(
            tuple(_dedupe_candidates(candidates)), image_assessment,
        ))
    return tuple(prepared)


def _append_review_candidates(
    db: Session, mention: ShopMention, extracted: ExtractedMention,
    candidates: tuple[PipelineCandidate, ...],
) -> None:
    existing_rank = (
        db.query(ResolutionCandidate.rank)
        .filter(ResolutionCandidate.mention_id == mention.id)
        .order_by(ResolutionCandidate.rank.desc())
        .first()
    )
    rank_offset = existing_rank[0] if existing_rank is not None else 0
    _store_candidates(db, mention, extracted, list(candidates), None)
    for item in db.new:
        if isinstance(item, ResolutionCandidate) and item.mention is mention:
            item.rank += rank_offset


async def _evaluate_imported_message_transaction(
    db: Session,
    message_id: str,
) -> EvaluationResult:
    try:
        message, mentions = _load_evaluation(db, message_id)
    except _EvaluationChanged as exc:
        raise ValueError(f"Evaluation message not found: message_id={message_id}") from exc
    expected = _evaluation_snapshot(message, mentions)
    if mentions and all(is_event_excluded_registration(mention) for mention in mentions):
        return _skip_evaluation(db, message_id, EVENT_EXCLUDED)
    if _human_reviewed(db, mentions):
        return _skip_evaluation(db, message_id, "human_reviewed")
    if message.fetch_error or not message.content:
        db.rollback()
        try:
            message, mentions = _lock_evaluation(db, message_id, expected)
        except _EvaluationChanged as exc:
            return _skip_evaluation(db, message_id, str(exc))
        for mention in mentions:
            _mark_difference(mention, "source_unavailable")
        db.commit()
        return EvaluationResult(message_id, 0, 0, len(mentions), True, "source_unavailable")

    assets = _asset_inputs(message)
    input_assessment = input_evidence_assessment(
        message.content, omitted_asset_count=max(0, len(assets) - 50)
    )
    source_url, canonical_hint, canonical_assets_ambiguous = (
        _source_urls_from_assets(assets)
    )
    unmatched = list(mentions)
    matched_count = 0
    new_count = 0
    difference_count = 0
    try:
        try:
            extraction = await extract_restaurant_message(message.content)
        except Exception as exc:
            _record_failure(db, message.message_id, "evaluation_extraction", exc)
            raise
        with db.no_autoflush:
            _record_metrics(db, message.message_id, "evaluation_extraction", extraction.metrics)
            event_context = image_evidence_assessment(
                "unknown", content=message.content,
                unresolved_reason=extraction.message.unresolved_reason or extraction.message.ignore_reason or "",
            ).is_event_excluded
            extracted_message = extraction.message
            if event_context and not extracted_message.mentions:
                extracted_message = ExtractedMessage(is_restaurant_message=True, mentions=[ExtractedMention(
                    shop_name="催事（出店元未特定）", needs_review=True, subject_kind="event",
                    confidence_reason=extracted_message.unresolved_reason or extracted_message.ignore_reason or "催事の紹介",
                )])
            if any(
                assess_mention_evidence(item, message.content).is_event_excluded
                for item in extracted_message.mentions
            ) and any(item.shop_id is not None or item.review_status == ReviewStatus.APPROVED.value for item in mentions):
                return _skip_evaluation(db, message_id, "event_reassessment_preserved")
            prepared_candidates = (
                await _prepare_candidates(db, message, extracted_message, assets, unmatched)
                if extracted_message.is_restaurant_message else ()
            )
            if any(item.event_origin is not None for item in prepared_candidates) and any(
                item.shop_id is not None or item.review_status == ReviewStatus.APPROVED.value for item in mentions
            ):
                return _skip_evaluation(db, message_id, "event_reassessment_preserved")
        # Release the evidence-read transaction before locking the current records.
        db.rollback()
        message, unmatched = _lock_evaluation(db, message_id, expected)
        if not extracted_message.is_restaurant_message:
            for mention in unmatched:
                if is_event_excluded_registration(mention):
                    continue
                _mark_difference(mention, "new_pipeline_not_restaurant")
                if input_assessment.requires_review:
                    mention.extraction_error = evidence_review_error(input_assessment)
                    mention.confidence_reason = " / ".join(input_assessment.reasons)
            message.processing_status = ProcessingStatus.SUCCEEDED.value
            message.processed_at = utc_now()
            _persist_evaluation_journal(db)
            db.commit()
            return EvaluationResult(message_id, 0, 0, len(unmatched), False)

        next_occurrence = max((mention.occurrence_index for mention in unmatched), default=-1) + 1
        for extracted, prepared in zip(extracted_message.mentions, prepared_candidates, strict=True):
            prior_event = next((
                item for item in unmatched
                if item.shop_id is None and item.extracted_name == extracted.shop_name
                and item.extracted_branch_name == extracted.branch_name
            ), None)
            if prior_event is not None and is_event_excluded_registration(prior_event):
                unmatched.remove(prior_event)
                continue
            if prepared.event_origin is not None:
                if prior_event is not None:
                    unmatched.remove(prior_event)
                    matched_count += 1
                row = _event_origin_mention_row(
                    db, message, extracted, prepared.event_origin,
                    occurrence=prior_event.occurrence_index if prior_event is not None else next_occurrence,
                    source_url=source_url, extraction_source="responses_structured", previous=prior_event,
                )
                if prior_event is None:
                    next_occurrence += 1
                    new_count += 1
                if row.shop_id is not None:
                    difference_count += 1
                continue
            note = operating_status_note(extracted)
            if note:
                extracted = extracted.model_copy(
                    update={"confidence_reason": f"{extracted.confidence_reason} / {note}"}
                )
            mention_canonical_hint = (
                canonical_hint if len(extraction.message.mentions) == 1 else None
            )
            mention = _match_legacy_mention(extracted, unmatched)
            if mention is not None:
                unmatched.remove(mention)
                matched_count += 1
            else:
                mention = ShopMention(
                    message=message,
                    occurrence_index=next_occurrence,
                    extracted_name=extracted.shop_name,
                    extracted_branch_name=extracted.branch_name,
                    extracted_area=extracted.area,
                    extracted_category=extracted.category,
                    source_url=source_url,
                    resolution_status="ambiguous",
                    review_status=ReviewStatus.PENDING.value,
                    difference_type="new_pipeline_mention",
                    extraction_source="responses_structured",
                    confidence_reason=extracted.confidence_reason,
                )
                next_occurrence += 1
                db.add(mention)
                db.flush()
                new_count += 1
                difference_count += 1

            safety_assessment = assess_mention_evidence(
                extracted, message.content, input_assessment=input_assessment
            )
            if prepared.image_assessment.requires_review:
                safety_assessment = EvidenceAssessment(
                    tuple(dict.fromkeys((*safety_assessment.codes, *prepared.image_assessment.codes))),
                    tuple(dict.fromkeys((*safety_assessment.reasons, *prepared.image_assessment.reasons))),
                )
            if safety_assessment.requires_review or (
                mention.review_status != ReviewStatus.APPROVED.value
                and requires_manual_evidence_review(mention.extraction_error)
            ):
                _mark_difference(mention, "evidence_review")
                mention.resolution_status = ResolutionStatus.AMBIGUOUS.value
                mention.extraction_error = evidence_review_error(safety_assessment) or mention.extraction_error
                mention.confidence_reason = " / ".join(
                    (extracted.confidence_reason, *safety_assessment.reasons)
                )
                if prepared.candidates:
                    _append_review_candidates(db, mention, extracted, prepared.candidates)
                difference_count += 1
                continue

            if canonical_assets_ambiguous:
                existing_shop = None
                _existing_reason = None
            else:
                existing_shop, _existing_reason = _find_existing_shop_from_mention(
                    db,
                    extracted,
                    mention_canonical_hint,
                )
            creation_blocked_reason = (
                _existing_reason if existing_shop is None else None
            )
            candidates = list(prepared.candidates)
            automatic_candidate: PipelineCandidate | None = None
            collision_candidate: PipelineCandidate | None = None
            if existing_shop is None and creation_blocked_reason is None:
                if canonical_assets_ambiguous:
                    existing_shop = None
                    candidate_reason = None
                else:
                    existing_shop, candidate_reason = _find_existing_shop(
                        db,
                        extracted,
                        candidates,
                    )
                creation_blocked_reason = (
                    candidate_reason if existing_shop is None else None
                )
                automatic_candidate = (
                    None
                    if canonical_assets_ambiguous
                    or existing_shop
                    or creation_blocked_reason
                    else _candidate_for_new_shop(
                        extracted,
                        candidates,
                    )
                )
                candidate_before_collision = automatic_candidate
                (
                    automatic_candidate,
                    collision_shop,
                    collision_reason,
                ) = _guard_new_shop_creation_or_link(
                    db,
                    extracted,
                    automatic_candidate,
                    creation_blocked_reason,
                )
                if collision_shop is not None:
                    existing_shop = collision_shop
                    collision_candidate = candidate_before_collision
                    creation_blocked_reason = None
                else:
                    creation_blocked_reason = collision_reason
            if existing_shop is not None:
                selected_version = existing_shop.version
                selected = (
                    db.query(Shop)
                    .filter(Shop.id == existing_shop.id)
                    .with_for_update()
                    .populate_existing()
                    .first()
                )
                if selected is None or selected.version != selected_version:
                    raise _EvaluationChanged("concurrent_change")
                if collision_candidate is not None:
                    db.expire(selected, ["mentions"])
                    if not any(
                        item.review_status == ReviewStatus.APPROVED.value
                        for item in selected.mentions
                    ):
                        raise _EvaluationChanged("concurrent_change")

            mention.extracted_name = extracted.shop_name
            mention.extracted_branch_name = extracted.branch_name
            mention.extracted_area = extracted.area
            mention.extracted_category = extracted.category
            mention.source_url = source_url or mention.source_url
            mention.extraction_source = "responses_structured"
            mention.confidence_reason = extracted.confidence_reason
            db.query(ResolutionCandidate).filter(
                ResolutionCandidate.mention_id == mention.id
            ).delete(synchronize_session=False)
            selected_candidate = automatic_candidate or collision_candidate
            _store_candidates(db, mention, extracted, candidates, selected_candidate)

            linked_collision_to_new_mention = False
            if (
                mention.shop is None
                and existing_shop is not None
                and collision_candidate is not None
            ):
                mention.shop = existing_shop
                mention.resolution_status = ResolutionStatus.RESOLVED.value
                mention.review_status = ReviewStatus.APPROVED.value
                mention.resolution_method = ResolutionMethod.AUTOMATIC.value
                mention.resolution_basis = ResolutionBasis.VERIFIED_COLLISION.value
                mention.reviewed_at = utc_now()
                metadata_status, metadata_difference = _metadata_review_state(
                    existing_shop.area,
                    existing_shop.category,
                )
                mention.metadata_review_status = metadata_status.value
                mention.metadata_difference_type = metadata_difference
                mention.metadata_reviewed_at = (
                    utc_now()
                    if metadata_status == MetadataReviewStatus.APPROVED
                    else None
                )
                linked_collision_to_new_mention = True

            if mention.shop is not None and not linked_collision_to_new_mention:
                differs = _identity_extraction_differs(mention.shop, extracted)
                if existing_shop is not None and existing_shop.id != mention.shop.id:
                    differs = True
                proposed_candidate = automatic_candidate or _strong_candidate_for_shop(
                    mention.shop, candidates
                )
                if _identity_candidate_differs(mention.shop, proposed_candidate):
                    differs = True
                if differs:
                    _mark_difference(mention, "new_pipeline_difference")
                    difference_count += 1
                metadata_status, metadata_difference = _metadata_review_state(
                    mention.shop.area,
                    mention.shop.category,
                )
                if (
                    _metadata_extraction_differs(mention.shop, extracted)
                    or _metadata_candidate_differs(mention.shop, proposed_candidate)
                    or metadata_status == MetadataReviewStatus.PENDING
                ):
                    _mark_metadata_difference(
                        mention,
                        metadata_difference or "new_pipeline_metadata_difference",
                    )
                    if not differs:
                        difference_count += 1

        for mention in unmatched:
            if is_event_excluded_registration(mention):
                continue
            _mark_difference(mention, "new_pipeline_missing_mention")
            difference_count += 1

        for mention_id, version, _shop_id, _occurrence in expected.mentions:
            mention = db.get(ShopMention, mention_id)
            if mention is not None and mention.version == version and not is_event_excluded_registration(mention):
                mention.version += 1
        message.processing_status = ProcessingStatus.SUCCEEDED.value
        message.processed_at = utc_now()
        _persist_evaluation_journal(db)
        db.commit()
        return EvaluationResult(
            message_id,
            matched_count,
            new_count,
            difference_count,
            False,
        )
    except _EvaluationChanged as exc:
        return _skip_evaluation(db, message_id, str(exc))
    except Exception as exc:
        db.rollback()
        try:
            failed, _mentions = _lock_evaluation(db, message_id, expected)
        except _EvaluationChanged as conflict:
            return _skip_evaluation(db, message_id, str(conflict))
        failed.processing_status = ProcessingStatus.FAILED.value
        db.add(
            ProcessingRun(
                message_id=message_id,
                stage="evaluation_pipeline",
                status=ProcessingStatus.FAILED.value,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        db.commit()
        raise


async def evaluate_imported_message(db: Session, message_id: str) -> EvaluationResult:
    journal = _OperationalJournal(defer_writes=True)
    token: Token[_OperationalJournal | None] = _ACTIVE_OPERATIONAL_JOURNAL.set(journal)
    journal_is_active = True
    try:
        return await _evaluate_imported_message_transaction(db, message_id)
    except Exception:
        _ACTIVE_OPERATIONAL_JOURNAL.reset(token)
        journal_is_active = False
        db.rollback()
        try:
            _persist_operational_journal(db, journal)
            db.commit()
        except Exception as persistence_error:
            db.rollback()
            logger.exception(
                "Failed to preserve evaluation operational records: message_id=%s error=%s",
                message_id,
                persistence_error,
            )
        raise
    finally:
        if journal_is_active:
            _ACTIVE_OPERATIONAL_JOURNAL.reset(token)
