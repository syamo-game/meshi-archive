from __future__ import annotations

import logging
from contextvars import Token
from dataclasses import dataclass

from sqlalchemy.orm import Session

from bot.restaurant_extractor import (
    ExtractedMention,
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
    Shop,
    ShopMention,
    utc_now,
)
from services.identification_pipeline import (
    PipelineCandidate,
    SourceAssetInput,
    _ACTIVE_OPERATIONAL_JOURNAL,
    _OperationalJournal,
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
from services.resolution import (
    CandidateIdentity,
    branches_conflict,
    evaluate_identity,
    name_similarity,
    normalize_address,
    normalize_area,
    normalize_phone,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluationResult:
    message_id: str
    matched_mentions: int
    new_mentions: int
    differences: int
    skipped: bool


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


async def _evaluate_imported_message_transaction(
    db: Session,
    message_id: str,
) -> EvaluationResult:
    message = db.query(Message).filter(Message.message_id == message_id).first()
    if message is None:
        raise ValueError(f"Evaluation message not found: message_id={message_id}")
    if message.fetch_error or not message.content:
        for mention in message.mentions:
            _mark_difference(mention, "source_unavailable")
        db.commit()
        return EvaluationResult(message_id, 0, 0, len(message.mentions), True)

    message.processing_status = ProcessingStatus.PROCESSING.value
    assets = _asset_inputs(message)
    source_url, canonical_hint, canonical_assets_ambiguous = (
        _source_urls_from_assets(assets)
    )
    image_urls = tuple(asset.url for asset in assets if _is_discord_image(asset))
    image_result: ImageAnalysisResult | None = None
    unmatched = list(message.mentions)
    matched_count = 0
    new_count = 0
    difference_count = 0
    try:
        try:
            extraction = await extract_restaurant_message(message.content)
        except Exception as exc:
            _record_failure(db, message.message_id, "evaluation_extraction", exc)
            raise
        _record_metrics(db, message.message_id, "evaluation_extraction", extraction.metrics)
        if not extraction.message.is_restaurant_message:
            for mention in unmatched:
                _mark_difference(mention, "new_pipeline_not_restaurant")
            message.processing_status = ProcessingStatus.SUCCEEDED.value
            message.processed_at = utc_now()
            db.commit()
            return EvaluationResult(message_id, 0, 0, len(unmatched), False)

        next_occurrence = max((mention.occurrence_index for mention in unmatched), default=-1) + 1
        for extracted in extraction.message.mentions:
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
            candidates: list[PipelineCandidate] = []
            automatic_candidate: PipelineCandidate | None = None
            collision_candidate: PipelineCandidate | None = None
            if existing_shop is None and creation_blocked_reason is None:
                candidates = await _collect_candidates(
                    db,
                    message,
                    extracted,
                    assets,
                )
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
            if (
                existing_shop is None
                and automatic_candidate is None
                and creation_blocked_reason is None
                and image_urls
                and not canonical_assets_ambiguous
            ):
                if image_result is None:
                    try:
                        image_result = await analyze_restaurant_images(extracted, image_urls)
                    except Exception as exc:
                        _record_failure(db, message.message_id, "evaluation_image", exc)
                        raise
                    _record_metrics(
                        db,
                        message.message_id,
                        "evaluation_image",
                        image_result.metrics,
                    )
                candidates.extend(_candidates_from_image(image_result, image_urls[0]))
                candidates = _dedupe_candidates(candidates)
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
                    if existing_shop or creation_blocked_reason
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
            _mark_difference(mention, "new_pipeline_missing_mention")
            difference_count += 1

        message.processing_status = ProcessingStatus.SUCCEEDED.value
        message.processed_at = utc_now()
        db.commit()
        return EvaluationResult(
            message_id,
            matched_count,
            new_count,
            difference_count,
            False,
        )
    except Exception as exc:
        db.rollback()
        failed = db.query(Message).filter(Message.message_id == message_id).one()
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
    journal = _OperationalJournal()
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
