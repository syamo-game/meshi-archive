from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from bot.restaurant_extractor import is_known_category
from db.models import (
    MetadataReviewStatus,
    ResolutionCandidate,
    ResolutionMethod,
    ResolutionStatus,
    ReviewEvent,
    ReviewScope,
    ReviewStatus,
    Shop,
    ShopMention,
    ShopRedirect,
    SourceAsset,
    utc_now,
)
from services.import_service import (
    collect_external_identities,
    external_identities_conflict,
    extract_external_identity,
    preferred_external_identity,
)
from services.mention_reevaluation import (
    AutomaticMentionResolution,
    auto_resolve_pending_mentions,
)
from services.merge_history import snapshot_merge
from services.extraction_safety import is_event_excluded_registration
from services.resolution import (
    normalize_address,
    normalize_name,
    normalize_phone,
    normalize_url_identity,
)
from services.shop_creation_lock import (
    find_shop_creation_collision,
    lock_new_shop_creation,
)
from web.area_groups import CANONICAL_AREAS, area_display_label, canonicalize_area, is_known_area


MANUAL_EXCLUDED = "manual_excluded"
_EXTRACTION_NOT_FOUND = "extraction_not_found"
_UNRESOLVED_SHOP_LABEL = "（店舗名未特定）"


class ReviewConflictError(RuntimeError):
    def __init__(self, message: str, *, code: str = "decision_conflict") -> None:
        super().__init__(message)
        self.code = code


class ReviewNotFoundError(RuntimeError):
    pass


class ReviewInvalidDecisionError(ValueError):
    pass


class BaseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["identity", "metadata"] = ReviewScope.IDENTITY.value
    expected_version: int = Field(ge=1)
    shop_version: int | None = Field(default=None, ge=1)


class ApproveCurrentDecision(BaseDecision):
    action: Literal["approve_current"]


class ShopMutatingDecision(BaseDecision):
    pass


class ApproveCandidateDecision(ShopMutatingDecision):
    action: Literal["approve_candidate"]
    candidate_id: int = Field(gt=0)


class EditableShop(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    shop_name: str = Field(min_length=1, max_length=500)
    branch_name: str | None = Field(default=None, max_length=255)
    area: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=2_000)
    phone: str | None = Field(default=None, max_length=64)
    canonical_url: HttpUrl | None = None

    @field_validator("area")
    @classmethod
    def validate_area(cls, value: str) -> str:
        canonical = canonicalize_area(value)
        if canonical is None:
            candidates = [
                area for area in sorted(CANONICAL_AREAS)
                if area_display_label(area) in value
            ][:5]
            hint = "、".join(candidates) if candidates else "登録済みの市区町村名・駅名"
            raise ValueError(
                f"エリア「{value}」は登録できる地名にありません。候補: {hint}。"
                "所在地に合う地名を選んでください。"
            )
        return canonical


class _CandidateShopSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    shop_name: str
    branch_name: str | None
    area: str | None
    category: str | None
    address: str | None
    phone: str | None
    canonical_url: str | None
    evidence_url: str | None
    external_source: str | None
    external_id: str | None


@dataclass(frozen=True)
class PreparedReviewPhoto:
    asset_id: int
    message_id: str
    source_url: str
    image_key: str


class EditAndApproveDecision(ShopMutatingDecision):
    action: Literal["edit_and_approve"]
    shop: EditableShop
    photo_asset_id: int | None = Field(default=None, gt=0)
    confirm_metadata: bool = False
    suggestion_evidence_urls: list[HttpUrl] = Field(default_factory=list, max_length=5)
    suggestion_reason: str | None = Field(default=None, max_length=2_000)
    supplement: str | None = Field(default=None, max_length=4_000)


class ExcludeDecision(ShopMutatingDecision):
    action: Literal["exclude"]
    reason: str | None = Field(default=None, max_length=2_000)


class MergeDecision(ShopMutatingDecision):
    action: Literal["merge"]
    target_shop_id: int = Field(gt=0)
    target_version: int = Field(ge=1)
    is_visited: bool
    visited_at: datetime | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    memo: str | None = Field(default=None, max_length=20_000)

    @field_validator("visited_at")
    @classmethod
    def validate_visited_at(cls, value: datetime | None) -> datetime | None:
        return value


class RejectDecision(ShopMutatingDecision):
    action: Literal["reject"]
    reason: str | None = Field(default=None, max_length=2_000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        return value if value and value.strip() else None


class DeferDecision(BaseDecision):
    action: Literal["defer"]
    note: str | None = Field(default=None, max_length=2_000)


ReviewDecisionRequest = Annotated[
    ApproveCurrentDecision
    | ApproveCandidateDecision
    | EditAndApproveDecision
    | MergeDecision
    | RejectDecision
    | ExcludeDecision
    | DeferDecision,
    Field(discriminator="action"),
]


class ReviewShopSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    version: int
    shop_name: str
    branch_name: str | None
    area: str | None
    category: str | None
    address: str | None
    phone: str | None
    canonical_url: str | None
    image_key: str | None = None


class ReviewDecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mention_id: int
    scope: Literal["identity", "metadata"]
    review_status: str
    metadata_review_status: str
    shop_id: int | None
    version: int
    automatically_resolved_count: int
    automatically_resolved_mention_ids: tuple[int, ...]
    shop: ReviewShopSnapshot | None


def _load_mention(db: Session, mention_id: int, expected_version: int) -> ShopMention:
    if db.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE, so acquire its write lock before checking versions.
        db.execute(
            text("UPDATE shop_mentions SET version = version WHERE id = :mention_id"),
            {"mention_id": mention_id},
        )
    mention = (
        db.query(ShopMention)
        .filter(ShopMention.id == mention_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if mention is None:
        raise ReviewNotFoundError(f"Review mention not found: mention_id={mention_id}")
    if mention.version != expected_version:
        raise ReviewConflictError(
            f"Review item changed: mention_id={mention_id}, "
            f"expected_version={expected_version}, actual_version={mention.version}",
            code="stale_mention",
        )
    return mention


def _load_shop_for_update(
    db: Session,
    shop_id: int,
    expected_version: int | None,
    role: str,
) -> Shop:
    if expected_version is None:
        raise ReviewConflictError(
            f"Shop version is required: role={role}, shop_id={shop_id}",
            code="missing_shop_version",
        )
    shop = (
        db.query(Shop)
        .filter(Shop.id == shop_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if shop is None:
        raise ReviewNotFoundError(f"Review shop not found: role={role}, shop_id={shop_id}")
    _validate_shop_version(shop, expected_version, role)
    return shop


def _validate_shop_version(
    shop: Shop,
    expected_version: int | None,
    role: str,
) -> None:
    if expected_version is None:
        raise ReviewConflictError(
            f"Shop version is required: role={role}, shop_id={shop.id}",
            code="missing_shop_version",
        )
    if shop.version != expected_version:
        raise ReviewConflictError(
            f"Shop changed: role={role}, shop_id={shop.id}, "
            f"expected_version={expected_version}, actual_version={shop.version}",
            code="stale_merge_target" if role == "target" else "stale_shop",
        )


def _load_source_shop_for_update(
    db: Session,
    mention: ShopMention,
    shop_version: int | None,
) -> Shop | None:
    if mention.shop_id is None:
        return None
    return _load_shop_for_update(db, mention.shop_id, shop_version, "source")


def _load_merge_shops_for_update(
    db: Session,
    mention: ShopMention,
    source_version: int | None,
    target_shop_id: int,
    target_version: int,
) -> tuple[Shop | None, Shop]:
    source_shop_id = mention.shop_id
    shop_ids = sorted(
        {shop_id for shop_id in (source_shop_id, target_shop_id) if shop_id is not None}
    )
    shops = (
        db.query(Shop)
        .filter(Shop.id.in_(shop_ids))
        .order_by(Shop.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    shops_by_id = {shop.id: shop for shop in shops}
    target = shops_by_id.get(target_shop_id)
    if target is None:
        raise ReviewNotFoundError(
            f"Review shop not found: role=target, shop_id={target_shop_id}"
        )
    _validate_shop_version(target, target_version, "target")
    if source_shop_id is None:
        return None, target
    source = shops_by_id.get(source_shop_id)
    if source is None:
        raise ReviewNotFoundError(
            f"Review shop not found: role=source, shop_id={source_shop_id}"
        )
    _validate_shop_version(source, source_version, "source")
    return source, target


def _candidate_shop_snapshot(
    shop: Shop | None,
    mention: ShopMention,
    candidate: ResolutionCandidate,
) -> _CandidateShopSnapshot:
    existing_area = shop.area if shop is not None else mention.extracted_area
    external_source, external_id = preferred_external_identity(
        external_source=candidate.external_source,
        external_id=candidate.external_id,
        urls=(candidate.canonical_url, candidate.evidence_url),
    )
    if external_source is None or external_id is None:
        external_source = shop.external_source if shop is not None else None
        external_id = shop.external_id if shop is not None else None
    return _CandidateShopSnapshot(
        shop_name=candidate.name,
        branch_name=(
            shop.branch_name if shop is not None else mention.extracted_branch_name
        ),
        area=(
            canonicalize_area(existing_area)
            or canonicalize_area(candidate.area)
            or canonicalize_area(mention.extracted_area)
        ),
        category=(
            candidate.category
            if candidate.category is not None
            else shop.category if shop is not None else None
        ),
        address=(
            candidate.address
            if candidate.address is not None
            else shop.address if shop is not None else None
        ),
        phone=(
            normalize_phone(candidate.phone)
            if candidate.phone is not None
            else shop.phone if shop is not None else None
        ),
        canonical_url=(
            candidate.canonical_url
            if candidate.canonical_url is not None
            else shop.canonical_url if shop is not None else None
        ),
        evidence_url=candidate.evidence_url,
        external_source=external_source,
        external_id=external_id,
    )


def _apply_candidate(shop: Shop, snapshot: _CandidateShopSnapshot) -> None:
    shop.shop_name = snapshot.shop_name
    shop.branch_name = snapshot.branch_name
    shop.area = snapshot.area
    shop.category = snapshot.category
    shop.address = snapshot.address
    shop.phone = snapshot.phone
    shop.canonical_url = snapshot.canonical_url
    shop.external_source = snapshot.external_source
    shop.external_id = snapshot.external_id
    shop.version += 1


def _approve(mention: ShopMention) -> None:
    if mention.shop is None:
        raise ReviewConflictError(f"Review item has no shop: mention_id={mention.id}")
    mention.review_status = ReviewStatus.APPROVED.value
    if mention.difference_type == MANUAL_EXCLUDED:
        mention.difference_type = None
    mention.resolution_status = ResolutionStatus.RESOLVED.value
    mention.resolution_method = ResolutionMethod.MANUAL.value
    mention.reviewed_at = utc_now()
    mention.version += 1


def _shop_from_mention(mention: ShopMention) -> Shop:
    return Shop(
        shop_name=mention.extracted_name,
        branch_name=mention.extracted_branch_name,
        area=canonicalize_area(mention.extracted_area),
        category=mention.extracted_category,
    )


def _validate_new_shop_creation(
    db: Session,
    *,
    shop_name: str,
    branch_name: str | None,
    area: str | None,
    address: str | None,
    phone: str | None,
    canonical_url: str | None,
    evidence_url: str | None,
    external_source: str | None,
    external_id: str | None,
    exclude_shop_id: int | None = None,
    existing_shop: Shop | None = None,
) -> None:
    validated_url = _validated_canonical_url(canonical_url)
    validated_evidence_url = _validated_canonical_url(evidence_url)
    candidate_identities = collect_external_identities(
        external_source=external_source,
        external_id=external_id,
        urls=(validated_url, validated_evidence_url),
    )
    if external_identities_conflict(candidate_identities):
        raise ReviewConflictError(
            "Candidate external IDs conflict within one service",
            code="candidate_identity_conflict",
        )
    # Existing duplicates must not block edits that preserve every collision key.
    if (
        existing_shop is not None
        and existing_shop.id == exclude_shop_id
        and validated_evidence_url is None
        and shop_name == existing_shop.shop_name
        and (branch_name or "") == (existing_shop.branch_name or "")
        and canonicalize_area(area) == canonicalize_area(existing_shop.area)
        and normalize_address(address) == normalize_address(existing_shop.address)
        and normalize_phone(phone) == normalize_phone(existing_shop.phone)
        and normalize_url_identity(validated_url) == normalize_url_identity(existing_shop.canonical_url)
        and (external_source, external_id) == (existing_shop.external_source, existing_shop.external_id)
    ):
        return
    lock_new_shop_creation(db)
    collision = find_shop_creation_collision(
        db,
        shop_name=shop_name,
        branch_name=branch_name,
        area=area,
        address=address,
        phone=phone,
        canonical_url=validated_url,
        evidence_url=validated_evidence_url,
        external_source=external_source,
        external_id=external_id,
        exclude_shop_id=exclude_shop_id,
    )
    if collision is not None:
        raise ReviewConflictError(
            f"New shop conflicts with an existing shop: kind={collision.kind}, "
            f"shop_id={collision.shop_id}",
            code="shop_collision",
        )


def _approve_metadata(mention: ShopMention) -> None:
    shop = mention.shop
    area = shop.area if shop is not None else None
    if not area:
        raise ReviewInvalidDecisionError(
            f"Metadata area is missing: mention_id={mention.id}"
        )
    if shop is None or not (shop.category or "").strip():
        raise ReviewInvalidDecisionError(f"Metadata category is missing: mention_id={mention.id}")
    canonical_area = canonicalize_area(area)
    if canonical_area is None:
        raise ReviewInvalidDecisionError(
            f"Metadata area is outside the managed vocabulary: "
            f"mention_id={mention.id}, area={area}"
        )
    if shop is None:
        raise AssertionError(f"Metadata shop disappeared: mention_id={mention.id}")
    if shop.area != canonical_area:
        shop.area = canonical_area
        shop.version += 1
    mention.metadata_review_status = MetadataReviewStatus.APPROVED.value
    mention.metadata_difference_type = None
    mention.metadata_reviewed_at = utc_now()
    mention.version += 1


def _reassess_metadata(mention: ShopMention) -> None:
    shop = mention.shop
    if shop is None:
        return
    area = shop.area
    if not area and mention.extracted_area and not is_known_area(mention.extracted_area):
        status = MetadataReviewStatus.PENDING
        difference_type = "unknown_area"
    elif not area:
        status = MetadataReviewStatus.PENDING
        difference_type = "missing_area"
    elif not is_known_area(area):
        status = MetadataReviewStatus.PENDING
        difference_type = "unknown_area"
    elif not shop.category:
        status = MetadataReviewStatus.PENDING
        difference_type = "missing_category"
    elif not is_known_category(shop.category):
        status = MetadataReviewStatus.PENDING
        difference_type = "unknown_category"
    else:
        status = MetadataReviewStatus.APPROVED
        difference_type = None
    mention.metadata_review_status = status.value
    mention.metadata_difference_type = difference_type
    mention.metadata_reviewed_at = (
        utc_now() if status == MetadataReviewStatus.APPROVED else None
    )


def _editable_canonical_url(shop: EditableShop) -> str | None:
    value = str(shop.canonical_url) if shop.canonical_url else None
    return _validated_canonical_url(value)


def _validated_canonical_url(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        normalize_url_identity(value)
    except ValueError as exc:
        raise ReviewInvalidDecisionError("Canonical URL is invalid") from exc
    return value


def _canonical_urls_match(left: str | None, right: str | None) -> bool:
    validated_left = _validated_canonical_url(left)
    validated_right = _validated_canonical_url(right)
    return normalize_url_identity(validated_left) == normalize_url_identity(
        validated_right
    )


def _apply_edit(shop: Shop, editable: EditableShop) -> None:
    shop.shop_name = editable.shop_name
    if "branch_name" in editable.model_fields_set:
        shop.branch_name = editable.branch_name
    shop.area = editable.area
    shop.category = editable.category
    shop.address = editable.address
    shop.phone = normalize_phone(editable.phone)
    shop.canonical_url = _editable_canonical_url(editable)
    shop.external_source, shop.external_id = extract_external_identity(shop.canonical_url)
    shop.version += 1


def _apply_metadata_edit(shop: Shop, editable: EditableShop) -> None:
    if editable.shop_name != shop.shop_name:
        raise ReviewInvalidDecisionError(
            "Metadata review cannot change shop identity: field=shop_name"
        )
    if not _canonical_urls_match(_editable_canonical_url(editable), shop.canonical_url):
        raise ReviewInvalidDecisionError(
            "Metadata review cannot change shop identity: field=canonical_url"
        )
    if (
        "branch_name" in editable.model_fields_set
        and editable.branch_name != shop.branch_name
    ):
        raise ReviewInvalidDecisionError(
            "Metadata review cannot change shop identity: field=branch_name"
        )
    shop.area = editable.area
    shop.category = editable.category
    shop.address = editable.address
    shop.phone = normalize_phone(editable.phone)
    shop.version += 1


def _validate_metadata_action(decision: ReviewDecisionRequest) -> None:
    if decision.scope != ReviewScope.METADATA.value:
        return
    if isinstance(
        decision,
        ApproveCurrentDecision | EditAndApproveDecision | DeferDecision,
    ):
        return
    raise ReviewInvalidDecisionError(
        f"Action is not allowed for metadata review: action={decision.action}"
    )


def apply_review_decision(
    db: Session,
    mention_id: int,
    decision: ReviewDecisionRequest,
    *,
    photo: PreparedReviewPhoto | None = None,
) -> ReviewDecisionResult:
    _validate_metadata_action(decision)
    # Acquire the shared write lock before locking mentions or shops.
    lock_new_shop_creation(db)
    mention = _load_mention(db, mention_id, decision.expected_version)
    if isinstance(decision, EditAndApproveDecision) and decision.photo_asset_id is not None:
        if photo is None or photo.asset_id != decision.photo_asset_id or photo.message_id != mention.message_id:
            raise ReviewInvalidDecisionError("写真の登録準備が確認できません。写真を選び直してください。")
        asset = db.query(SourceAsset).filter(SourceAsset.id == photo.asset_id).with_for_update().populate_existing().first()
        if asset is None or asset.message_id != mention.message_id or asset.kind != "image" or asset.url != photo.source_url or asset.fetch_status == "unavailable":
            raise ReviewConflictError("元投稿の写真が変更されています。最新情報を確認してください。", code="stale_mention")
    elif photo is not None:
        raise ReviewInvalidDecisionError("写真の対象が登録要求と一致しません。")
    if is_event_excluded_registration(mention):
        requires_origin = isinstance(decision, ApproveCurrentDecision | ApproveCandidateDecision)
        if isinstance(decision, DeferDecision) and decision.scope == ReviewScope.IDENTITY.value:
            requires_origin = True
        if isinstance(decision, EditAndApproveDecision) and decision.scope == ReviewScope.IDENTITY.value:
            requires_origin = bool(
                normalize_name(decision.shop.shop_name) == normalize_name(mention.extracted_name)
                and normalize_name(decision.shop.branch_name or "") == normalize_name(mention.extracted_branch_name or "")
                and not decision.shop.address
                and not normalize_phone(decision.shop.phone)
                and not decision.shop.canonical_url
            )
        if requires_origin:
            raise ReviewConflictError(
                "催事会場は登録対象外です。出店元の常設店舗を根拠付きで確認し、"
                "店名・支店名を修正するか住所・電話・店舗URLを追加して保存するか、"
                "確認済みの既存店舗へ関連付けてください。"
                f" mention_id={mention.id}",
                code="event_origin_required",
            )
    previous_shop_id = mention.shop_id
    previous_shop = ReviewShopSnapshot.model_validate(mention.shop) if mention.shop is not None else None
    selected_shop_id = mention.shop_id
    note: str | None = None

    if decision.scope == ReviewScope.METADATA.value:
        if isinstance(decision, ApproveCurrentDecision):
            if mention.shop_id is None:
                raise ReviewConflictError(
                    f"Metadata review item has no shop: mention_id={mention.id}",
                    code="missing_shop",
                )
            _load_source_shop_for_update(db, mention, decision.shop_version)
            _approve_metadata(mention)
        elif isinstance(decision, EditAndApproveDecision):
            shop = _load_source_shop_for_update(db, mention, decision.shop_version)
            if shop is None:
                raise ReviewConflictError(
                    f"Metadata review item has no shop: mention_id={mention.id}",
                    code="missing_shop",
                )
            editable_url = _editable_canonical_url(decision.shop)
            _validate_new_shop_creation(
                db,
                shop_name=shop.shop_name,
                branch_name=shop.branch_name,
                area=decision.shop.area,
                address=decision.shop.address,
                phone=decision.shop.phone,
                canonical_url=editable_url,
                evidence_url=None,
                external_source=shop.external_source,
                external_id=shop.external_id,
                exclude_shop_id=shop.id,
                existing_shop=shop,
            )
            _apply_metadata_edit(shop, decision.shop)
            _approve_metadata(mention)
        elif isinstance(decision, DeferDecision):
            mention.metadata_review_status = MetadataReviewStatus.DEFERRED.value
            mention.metadata_reviewed_at = utc_now()
            mention.version += 1
            note = decision.note
        else:
            raise AssertionError(f"Unhandled metadata review decision: {type(decision).__name__}")
    elif isinstance(decision, ApproveCurrentDecision):
        if mention.shop is None:
            if (
                mention.difference_type == _EXTRACTION_NOT_FOUND
                or mention.extracted_name == _UNRESOLVED_SHOP_LABEL
            ):
                raise ReviewConflictError(
                    f"Unresolved review item cannot create a shop: mention_id={mention.id}"
                )
            _validate_new_shop_creation(
                db,
                shop_name=mention.extracted_name,
                branch_name=mention.extracted_branch_name,
                area=mention.extracted_area,
                address=None,
                phone=None,
                canonical_url=None,
                evidence_url=None,
                external_source=None,
                external_id=None,
            )
            mention.shop = _shop_from_mention(mention)
            db.flush()
        else:
            _load_source_shop_for_update(db, mention, decision.shop_version)
        selected_shop_id = mention.shop.id
        _approve(mention)
        _reassess_metadata(mention)
    elif isinstance(decision, ApproveCandidateDecision):
        shop = _load_source_shop_for_update(db, mention, decision.shop_version)
        candidate = (
            db.query(ResolutionCandidate)
            .filter(
                ResolutionCandidate.id == decision.candidate_id,
                ResolutionCandidate.mention_id == mention.id,
            )
            .first()
        )
        if candidate is None:
            raise ReviewNotFoundError(
                f"Candidate not found: mention_id={mention.id}, candidate_id={decision.candidate_id}"
            )
        candidate_identities = collect_external_identities(
            external_source=candidate.external_source,
            external_id=candidate.external_id,
            urls=(candidate.canonical_url, candidate.evidence_url),
        )
        if external_identities_conflict(candidate_identities):
            raise ReviewConflictError(
                "Candidate external IDs conflict within one service",
                code="candidate_identity_conflict",
            )
        snapshot = _candidate_shop_snapshot(shop, mention, candidate)
        _validate_new_shop_creation(
            db,
            shop_name=snapshot.shop_name,
            branch_name=snapshot.branch_name,
            area=snapshot.area,
            address=snapshot.address,
            phone=snapshot.phone,
            canonical_url=snapshot.canonical_url,
            evidence_url=snapshot.evidence_url,
            external_source=snapshot.external_source,
            external_id=snapshot.external_id,
            exclude_shop_id=shop.id if shop is not None else None,
        )
        if shop is None:
            shop = Shop(shop_name=snapshot.shop_name)
            mention.shop = shop
            db.flush()
        _apply_candidate(shop, snapshot)
        selected_shop_id = shop.id
        _approve(mention)
        _reassess_metadata(mention)
    elif isinstance(decision, EditAndApproveDecision):
        shop = _load_source_shop_for_update(db, mention, decision.shop_version)
        editable_url = _editable_canonical_url(decision.shop)
        editable_source, editable_id = extract_external_identity(editable_url)
        _validate_new_shop_creation(
            db,
            shop_name=decision.shop.shop_name,
            branch_name=(
                decision.shop.branch_name
                if shop is None or "branch_name" in decision.shop.model_fields_set
                else shop.branch_name
            ),
            area=decision.shop.area,
            address=decision.shop.address,
            phone=decision.shop.phone,
            canonical_url=editable_url,
            evidence_url=None,
            external_source=editable_source,
            external_id=editable_id,
            exclude_shop_id=shop.id if shop is not None else None,
            existing_shop=shop,
        )
        if shop is None:
            shop = Shop(shop_name=decision.shop.shop_name)
            mention.shop = shop
            db.flush()
        _apply_edit(shop, decision.shop)
        selected_shop_id = shop.id
        _approve(mention)
        _reassess_metadata(mention)
    elif isinstance(decision, MergeDecision):
        if mention.shop_id == decision.target_shop_id:
            raise ReviewConflictError(
                f"Cannot merge a shop into itself: shop_id={decision.target_shop_id}"
            )
        source, target = _load_merge_shops_for_update(
            db,
            mention,
            decision.shop_version,
            decision.target_shop_id,
            decision.target_version,
        )
        moved_mentions = (
            tuple(
                db.query(ShopMention)
                .filter(ShopMention.shop_id == source.id)
                .order_by(ShopMention.id)
                .populate_existing()
                .all()
            )
            if source is not None
            else (mention,)
        )
        note = snapshot_merge(
            (source, target) if source is not None else (target,),
            moved_mentions,
            reason="manual_merge",
        )
        for moved in moved_mentions:
            if moved.id == mention.id:
                continue
            moved_version = moved.version
            reserved = (
                db.query(ShopMention)
                .filter(
                    ShopMention.id == moved.id,
                    ShopMention.shop_id == moved.shop_id,
                    ShopMention.version == moved_version,
                )
                .update(
                    {ShopMention.version: ShopMention.version + 1},
                    synchronize_session=False,
                )
            )
            if reserved != 1:
                raise ReviewConflictError(
                    "関連する投稿が更新されています。最新情報を確認してから統合してください。",
                    code="stale_mention",
                )
            set_committed_value(moved, "version", moved_version + 1)
        if source is None:
            mention.shop = target
        else:
            if (
                source.image_key is not None
                and target.image_key is not None
                and source.image_key != target.image_key
            ):
                raise ReviewConflictError(
                    "統合元と統合先に異なる写真が登録されています。"
                    "写真を確認してから統合してください。"
                    f"（統合元ID: {source.id}、統合先ID: {target.id}）"
                )
            if target.image_key is None:
                target.image_key = source.image_key
            incoming_redirects = (
                db.query(ShopRedirect)
                .filter(ShopRedirect.target_shop_id == source.id)
                .with_for_update()
                .all()
            )
            for redirect in incoming_redirects:
                redirect.target_shop_id = target.id
            db.add(
                ShopRedirect(
                    source_shop_id=source.id,
                    target_shop_id=target.id,
                    reason="merge",
                )
            )
            for source_mention in moved_mentions:
                source_mention.shop = target
                if source_mention.id != mention.id:
                    db.add(
                        ReviewEvent(
                            mention=source_mention,
                            scope=ReviewScope.IDENTITY.value,
                            action="merge_related",
                            previous_shop_id=source.id,
                            selected_shop_id=target.id,
                            note=note,
                        )
                    )
        target.is_visited = decision.is_visited
        target.visited_at = decision.visited_at if decision.is_visited else None
        target.rating = decision.rating
        target.memo = decision.memo
        target.version += 1
        selected_shop_id = target.id
        if source is not None:
            db.delete(source)
        _approve(mention)
        _reassess_metadata(mention)
    elif isinstance(decision, ExcludeDecision):
        _load_source_shop_for_update(db, mention, decision.shop_version)
        previous_reason = mention.difference_type
        mention.review_status = ReviewStatus.REJECTED.value
        mention.difference_type = MANUAL_EXCLUDED
        mention.resolution_status = ResolutionStatus.INVALID.value
        mention.resolution_method = ResolutionMethod.MANUAL.value
        mention.reviewed_at = utc_now()
        mention.version += 1
        note = json.dumps({
            "reason": decision.reason,
            "previous_reason": previous_reason,
            "preserved_shop": previous_shop.model_dump(mode="json") if previous_shop else None,
        }, ensure_ascii=False)
    elif isinstance(decision, RejectDecision):
        source = _load_source_shop_for_update(db, mention, decision.shop_version)
        mention.shop = None
        mention.review_status = ReviewStatus.REJECTED.value
        mention.resolution_status = ResolutionStatus.INVALID.value
        mention.resolution_method = ResolutionMethod.MANUAL.value
        mention.reviewed_at = utc_now()
        mention.version += 1
        note = decision.reason
        selected_shop_id = None
        if source and len(source.mentions) == 0:
            db.delete(source)
    elif isinstance(decision, DeferDecision):
        mention.review_status = ReviewStatus.DEFERRED.value
        mention.reviewed_at = utc_now()
        mention.version += 1
        note = decision.note
    else:
        raise AssertionError(f"Unhandled review decision: {type(decision).__name__}")

    if isinstance(decision, EditAndApproveDecision):
        if photo is not None and mention.shop is not None:
            mention.shop.image_key = photo.image_key
        if decision.confirm_metadata and decision.scope == ReviewScope.IDENTITY.value:
            _approve_metadata(mention)
            db.add(ReviewEvent(mention=mention, scope=ReviewScope.METADATA.value,
                action="edit_and_approve", previous_shop_id=previous_shop_id,
                selected_shop_id=selected_shop_id, note="店舗情報と写真を確認して登録"))
        note = json.dumps({
            "before": previous_shop.model_dump(mode="json") if previous_shop else None,
            "after": ReviewShopSnapshot.model_validate(mention.shop).model_dump(mode="json") if mention.shop else None,
            "photo_asset_id": decision.photo_asset_id,
            "supplement": decision.supplement,
            "selected_reference_urls": [str(url) for url in decision.suggestion_evidence_urls],
            "selected_suggestion_reason": decision.suggestion_reason,
        }, ensure_ascii=False)

    db.add(
        ReviewEvent(
            mention=mention,
            scope=decision.scope,
            action=decision.action,
            previous_shop_id=previous_shop_id,
            selected_shop_id=selected_shop_id,
            note=note,
        )
    )
    automatic_resolution = AutomaticMentionResolution(mention_ids=())
    if (
        decision.scope == ReviewScope.IDENTITY.value
        and mention.review_status == ReviewStatus.APPROVED.value
        and mention.shop is not None
    ):
        db.flush()
        automatic_resolution = auto_resolve_pending_mentions(
            db,
            source_mention=mention,
            target_shop=mention.shop,
        )
    db.commit()
    return ReviewDecisionResult(
        mention_id=mention.id,
        scope=decision.scope,
        review_status=mention.review_status,
        metadata_review_status=mention.metadata_review_status,
        shop_id=mention.shop_id,
        version=mention.version,
        automatically_resolved_count=len(automatic_resolution.mention_ids),
        automatically_resolved_mention_ids=automatic_resolution.mention_ids,
        shop=ReviewShopSnapshot.model_validate(mention.shop) if mention.shop else None,
    )
