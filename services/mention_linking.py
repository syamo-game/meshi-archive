from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    ResolutionMethod,
    ResolutionStatus,
    ReviewEvent,
    ReviewScope,
    ReviewStatus,
    Shop,
    ShopMention,
    utc_now,
)
from services.review_service import (
    ReviewConflictError,
    ReviewDecisionResult,
    ReviewNotFoundError,
    ReviewShopSnapshot,
    _load_mention,
)
from services.shop_creation_lock import lock_new_shop_creation


class LinkMentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    expected_version: int = Field(ge=1)
    source_shop_id: int | None = Field(..., gt=0)
    source_shop_version: int | None = Field(..., ge=1)
    target_shop_id: int = Field(gt=0)
    target_shop_version: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_source_pair(self) -> "LinkMentionRequest":
        if (self.source_shop_id is None) != (self.source_shop_version is None):
            raise ValueError("source_shop_id and source_shop_version must both be null or both be present")
        return self


class LinkShopSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, frozen=True)

    id: int
    version: int
    shop_name: str
    branch_name: str | None
    area: str | None
    category: str | None
    address: str | None
    phone: str | None
    canonical_url: str | None
    is_visited: bool
    visited_at: datetime | None
    rating: int | None
    memo: str | None
    image_key: str | None
    external_source: str | None
    external_id: str | None


class MentionLinkPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mention_id: int
    expected_version: int
    source_shop: LinkShopSnapshot | None
    target_shop: LinkShopSnapshot
    source_mention_count: int = Field(ge=0)
    target_mention_count: int = Field(ge=0)
    review_status: str
    metadata_review_status: str
    source_will_be_empty: bool


class MentionLinkResult(ReviewDecisionResult):
    source_shop: ReviewShopSnapshot | None


class _PreviewMention(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    version: int
    shop_id: int | None
    review_status: str
    metadata_review_status: str


def _reject_same_shop(source_shop_id: int | None, target_shop_id: int) -> None:
    if source_shop_id == target_shop_id:
        raise ReviewConflictError(
            f"Mention is already linked to the target shop: shop_id={target_shop_id}",
            code="same_shop",
        )


def preview_mention_link(
    db: Session,
    mention_id: int,
    target_shop_id: int,
) -> MentionLinkPreview:
    # Scalar reads avoid flushing edits or reusing stale ORM instances in a preview.
    with db.no_autoflush:
        row = db.execute(
            select(
                ShopMention.id,
                ShopMention.version,
                ShopMention.shop_id,
                ShopMention.review_status,
                ShopMention.metadata_review_status,
            ).where(ShopMention.id == mention_id)
        ).mappings().first()
        if row is None:
            raise ReviewNotFoundError(f"Review mention not found: mention_id={mention_id}")
        mention = _PreviewMention.model_validate(row)
        _reject_same_shop(mention.shop_id, target_shop_id)
        shop_ids = {target_shop_id}
        if mention.shop_id is not None:
            shop_ids.add(mention.shop_id)
        columns = tuple(Shop.__table__.c[name] for name in LinkShopSnapshot.model_fields)
        shop_rows = db.execute(select(*columns).where(Shop.id.in_(shop_ids))).mappings().all()
        shops: dict[int, LinkShopSnapshot] = {}
        for shop_row in shop_rows:
            snapshot = LinkShopSnapshot.model_validate(shop_row)
            shops[snapshot.id] = snapshot
        target = shops.get(target_shop_id)
        if target is None:
            raise ReviewNotFoundError(f"Review shop not found: role=target, shop_id={target_shop_id}")
        source = shops.get(mention.shop_id) if mention.shop_id is not None else None
        if mention.shop_id is not None and source is None:
            raise ReviewNotFoundError(f"Review shop not found: role=source, shop_id={mention.shop_id}")
        counts: dict[int, int] = {
            shop_id: count
            for shop_id, count in db.execute(
                select(ShopMention.shop_id, func.count(ShopMention.id))
                .where(ShopMention.shop_id.in_(shop_ids))
                .group_by(ShopMention.shop_id)
            )
        }
        source_count = counts.get(mention.shop_id, 0) if mention.shop_id is not None else 0
        return MentionLinkPreview(
            mention_id=mention.id,
            expected_version=mention.version,
            source_shop=source,
            target_shop=target,
            source_mention_count=source_count,
            target_mention_count=counts.get(target.id, 0),
            review_status=mention.review_status,
            metadata_review_status=mention.metadata_review_status,
            source_will_be_empty=source is not None and source_count == 1,
        )


def _locked_shops(db: Session, request: LinkMentionRequest) -> tuple[Shop | None, Shop]:
    shop_ids = {request.target_shop_id}
    if request.source_shop_id is not None:
        shop_ids.add(request.source_shop_id)
    rows = (
        db.query(Shop)
        .filter(Shop.id.in_(shop_ids))
        .order_by(Shop.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    shops: dict[int, Shop] = {shop.id: shop for shop in rows}
    for role, shop_id, expected_version in (
        ("source", request.source_shop_id, request.source_shop_version),
        ("target", request.target_shop_id, request.target_shop_version),
    ):
        if shop_id is None:
            continue
        shop = shops.get(shop_id)
        if shop is None:
            raise ReviewNotFoundError(f"Review shop not found: role={role}, shop_id={shop_id}")
        if shop.version != expected_version:
            raise ReviewConflictError(
                f"Shop changed: role={role}, shop_id={shop_id}, "
                f"expected_version={expected_version}, actual_version={shop.version}",
                code="stale_shop",
            )
    source = shops[request.source_shop_id] if request.source_shop_id is not None else None
    return source, shops[request.target_shop_id]


def link_mention(
    db: Session,
    mention_id: int,
    request: LinkMentionRequest,
) -> MentionLinkResult:
    try:
        with db.no_autoflush:
            lock_new_shop_creation(db)
            mention = _load_mention(db, mention_id, request.expected_version)
            if mention.shop_id != request.source_shop_id:
                raise ReviewConflictError(
                    f"Mention source changed: mention_id={mention_id}, "
                    f"expected_shop_id={request.source_shop_id}, actual_shop_id={mention.shop_id}",
                    code="source_changed",
                )
            _reject_same_shop(mention.shop_id, request.target_shop_id)
            source, target = _locked_shops(db, request)
            mention.shop = target
            mention.review_status = ReviewStatus.APPROVED.value
            mention.resolution_status = ResolutionStatus.RESOLVED.value
            mention.resolution_method = ResolutionMethod.MANUAL.value
            mention.reviewed_at = utc_now()
            mention.version += 1
            if source is not None:
                source.version += 1
            target.version += 1
            db.add(ReviewEvent(
                mention_id=mention.id,
                scope=ReviewScope.IDENTITY.value,
                action="link_existing",
                previous_shop_id=request.source_shop_id,
                selected_shop_id=target.id,
                note=request.note,
            ))
            db.flush()
            result = MentionLinkResult(
                mention_id=mention.id,
                scope=ReviewScope.IDENTITY.value,
                review_status=mention.review_status,
                metadata_review_status=mention.metadata_review_status,
                shop_id=target.id,
                version=mention.version,
                automatically_resolved_count=0,
                automatically_resolved_mention_ids=(),
                shop=ReviewShopSnapshot.model_validate(target),
                source_shop=ReviewShopSnapshot.model_validate(source) if source is not None else None,
            )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
