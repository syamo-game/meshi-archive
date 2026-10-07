from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Generator
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from services.review_photos import ReviewPhotoFetchError, prepare_review_photo
from services.review_service import EditAndApproveDecision
from services.shop_image_cache import cache_shop_image, validate_image_source_url
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from db.database import SessionLocal
from db.models import (
    Message,
    MetadataReviewStatus,
    ProcessingStatus,
    ResolutionCandidate,
    ReviewScope,
    ReviewStatus,
    Shop,
    ShopMention,
    SourceAsset,
)
from services.review_service import (
    MANUAL_EXCLUDED,
    ReviewConflictError,
    ReviewDecisionRequest,
    ReviewDecisionResult,
    ReviewInvalidDecisionError,
    ReviewNotFoundError,
    apply_review_decision,
)
from services.review_audit import export_review_audit
from services.shop_creation_lock import lock_new_shop_creation
from bot.restaurant_extractor import is_known_category
from services.category_normalization import CATEGORY_VALUES
from web.area_groups import is_known_area
from services.mention_linking import (
    LinkMentionRequest,
    MentionLinkResult,
    MentionLinkPreview,
    link_mention,
    preview_mention_link,
)
from services.extraction_safety import EVENT_EXCLUDED, requires_manual_evidence_review
from services.mention_reevaluation import (
    ReviewGroupSummary,
    build_open_review_group_index,
)
from web.auth import is_admin
from web.csrf import get_csrf_token, verify_csrf_token
from web.read_only import is_read_only, require_writable


logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

_DISCORD_IMAGE_HOSTS = frozenset(
    {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "images-ext-1.discordapp.net",
        "images-ext-2.discordapp.net",
    }
)
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/admin/review/audit.csv")
def review_audit_csv(request: Request, db: Session = Depends(get_db)) -> Response:
    if not is_admin(request):
        raise HTTPException(
            status_code=403, detail="管理者認証が必要です。",
            headers={"Cache-Control": "private, no-store"},
        )
    audit = export_review_audit(db)
    generated_at = datetime.now(timezone.utc)
    checksum = hashlib.sha256(audit.content).hexdigest()
    filename = f"meshi_review_audit_{generated_at:%Y%m%dT%H%M%S%fZ}_{checksum[:12]}.csv"
    return Response(
        content=audit.content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Export-Generated-At": generated_at.isoformat(),
            "X-Export-Content-SHA256": checksum,
            "X-Export-Row-Count": str(audit.row_count),
            "X-Export-Row-Unit": "mention",
            "X-Export-Message-Count": str(audit.message_count),
            "X-Export-Unlinked-Count": str(audit.unlinked_count),
            "X-Export-Scope": "all-mentions",
        },
    )


class ReviewCounters(BaseModel):
    """Global diagnostics, independent of queue filters and pagination.

    Status counts use the selected scope. Statuses and source_unavailable count
    mentions; failed counts messages and can overlap with source_unavailable.
    Metadata status counts exclude unlinked, rejected event registrations.
    """

    model_config = ConfigDict(extra="forbid")

    pending: int
    approved: int
    deferred: int
    rejected: int
    source_unavailable: int
    failed: int
    unresolved: int = Field(ge=0)


class AssetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    kind: str
    url: str
    title: str | None
    description: str | None
    mime_type: str | None
    extracted_text: str | None
    fetch_status: str
    proxy_url: str | None
    photo_url: str | None = None


def _photo_preview_url(asset: SourceAsset) -> str | None:
    if asset.kind != "image" or asset.fetch_status == "unavailable":
        return None
    try:
        validate_image_source_url(asset.url)
    except ValueError:
        logger.warning("Review photo preview unavailable: asset_id=%s message_id=%s", asset.id, asset.message_id)
        return None
    return f"/api/admin/evidence/photo/{asset.id}"


class CandidateView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    rank: int
    name: str
    area: str | None
    category: str | None
    address: str | None
    phone: str | None
    canonical_url: str | None
    external_source: str | None
    external_id: str | None
    evidence_url: str | None
    provenance: str
    is_verified: bool
    verification_reason: str | None
    matched_fields: str | None
    conflicting_fields: str | None
    name_similarity: float
    is_strong_match: bool


class ShopView(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    image_key: str | None = None


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    label: str


class ReviewItemView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    version: int
    message_id: str
    discord_url: str | None
    source_created_at: datetime | None
    message_content: str | None
    processing_status: str
    fetch_error: str | None
    extracted_name: str
    extracted_branch_name: str | None
    extracted_area: str | None
    extracted_category: str | None
    source_url: str | None
    resolution_status: str
    review_status: str
    metadata_review_status: str
    difference_type: str | None
    metadata_difference_type: str | None
    extraction_source: str
    extraction_error: str | None
    confidence_reason: str | None
    issues: list[ReviewIssue]
    shop: ShopView | None
    assets: list[AssetView]
    candidates: list[CandidateView]
    review_group_key: str | None
    review_group_count: int
    review_group_mention_ids: list[int]
    review_group_reason: str | None


class ReviewQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["all", "identity", "metadata"]
    total_count: int = Field(
        ge=0,
        description="Matching mentions before cursor and limit are applied.",
    )
    counters: ReviewCounters = Field(
        description="Global diagnostics; only status counts depend on scope.",
    )
    items: list[ReviewItemView]
    next_cursor: int | None


def _metadata_scope_eligible() -> ColumnElement[bool]:
    return and_(
        ~and_(
            ShopMention.review_status == ReviewStatus.REJECTED.value,
            func.coalesce(ShopMention.difference_type, "") == MANUAL_EXCLUDED,
        ),
        or_(
            ShopMention.shop_id.isnot(None),
            ShopMention.review_status != ReviewStatus.REJECTED.value,
            func.coalesce(ShopMention.difference_type, "") != EVENT_EXCLUDED,
        ),
    )


def _all_status_filter(status: str) -> ColumnElement[bool]:
    if status == ReviewStatus.APPROVED.value:
        return and_(
            ShopMention.review_status == status,
            or_(ShopMention.metadata_review_status == status, ~_metadata_scope_eligible()),
        )
    if status == ReviewStatus.REJECTED.value:
        return ShopMention.review_status == status
    return or_(
        ShopMention.review_status == status,
        and_(
            ShopMention.review_status == ReviewStatus.APPROVED.value,
            _metadata_scope_eligible(),
            ShopMention.metadata_review_status == status,
        ),
    )


def _counters(db: Session, scope: str) -> ReviewCounters:
    if scope == "all":
        counts: dict[str, int] = {
            status.value: db.query(ShopMention).filter(_all_status_filter(status.value)).count()
            for status in ReviewStatus
        }
    elif scope == ReviewScope.IDENTITY:
        counts = dict(
            db.query(ShopMention.review_status, func.count(ShopMention.id))
            .group_by(ShopMention.review_status)
            .all()
        )
    else:
        counts = dict(
            db.query(ShopMention.metadata_review_status, func.count(ShopMention.id))
            .filter(_metadata_scope_eligible())
            .group_by(ShopMention.metadata_review_status)
            .all()
        )
    source_unavailable = (
        db.query(func.count(ShopMention.id))
        .join(Message, Message.message_id == ShopMention.message_id)
        .filter(Message.fetch_error.isnot(None))
        .scalar()
        or 0
    )
    failed = (
        db.query(func.count(Message.message_id))
        .filter(Message.processing_status == ProcessingStatus.FAILED.value)
        .scalar()
        or 0
    )
    return ReviewCounters(
        pending=int(counts.get(ReviewStatus.PENDING.value, 0)),
        approved=int(counts.get(ReviewStatus.APPROVED.value, 0)),
        deferred=int(counts.get(ReviewStatus.DEFERRED.value, 0)),
        rejected=int(counts.get(ReviewStatus.REJECTED.value, 0)),
        source_unavailable=int(source_unavailable),
        failed=int(failed),
        unresolved=db.query(ShopMention).filter(or_(
            _all_status_filter(ReviewStatus.PENDING.value),
            _all_status_filter(ReviewStatus.DEFERRED.value),
        )).count(),
    )


def _discord_url(message: Message) -> str | None:
    guild_id = os.getenv("DISCORD_GUILD_ID")
    channel_id = message.channel_id or os.getenv("DISCORD_CHANNEL_ID")
    if not guild_id or not channel_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message.message_id}"


def _candidate_view(candidate: ResolutionCandidate) -> CandidateView:
    return CandidateView(
        id=candidate.id,
        rank=candidate.rank,
        name=candidate.name,
        area=candidate.area,
        category=candidate.category,
        address=candidate.address,
        phone=candidate.phone,
        canonical_url=candidate.canonical_url,
        external_source=candidate.external_source,
        external_id=candidate.external_id,
        evidence_url=candidate.evidence_url,
        provenance=candidate.provenance,
        is_verified=candidate.is_verified,
        verification_reason=candidate.verification_reason,
        matched_fields=candidate.matched_fields,
        conflicting_fields=candidate.conflicting_fields,
        name_similarity=candidate.name_similarity_milli / 1000,
        is_strong_match=candidate.is_strong_match,
    )


def _extraction_error_display(error: str | None) -> str | None:
    if error is None or not requires_manual_evidence_review(error):
        return error
    fallback = "この項目は確認待ちです。原文・添付と候補を確認してください。"
    try:
        payload = json.loads(error.partition(":")[2])
    except (ValueError, TypeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    codes = payload.get("codes")
    reasons = payload.get("reasons")
    if (
        not isinstance(codes, list) or not codes
        or not all(isinstance(code, str) and code.strip() for code in codes)
        or not isinstance(reasons, list) or not reasons
        or not all(isinstance(reason, str) and reason.strip() for reason in reasons)
    ):
        return fallback
    return " / ".join(dict.fromkeys(reason.strip() for reason in reasons))


def _review_issues(mention: ShopMention) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    shop = mention.shop
    name = shop.shop_name if shop else mention.extracted_name
    area = shop.area if shop else mention.extracted_area
    category = shop.category if shop else mention.extracted_category
    if not name or name == "（店舗名未特定）":
        issues.append(ReviewIssue(code="missing_name", label="店舗名未特定"))
    elif mention.review_status in {"pending", "deferred"}:
        issues.append(ReviewIssue(code="identity_review", label="店・支店要確認"))
    if not area:
        issues.append(ReviewIssue(code="missing_area", label="エリア未入力"))
    elif not is_known_area(area):
        issues.append(ReviewIssue(code="unknown_area", label="エリア要確認"))
    if not category:
        issues.append(ReviewIssue(code="missing_category", label="カテゴリ未入力"))
    elif not is_known_category(category):
        issues.append(ReviewIssue(code="unknown_category", label="カテゴリ要確認"))
    elif mention.metadata_review_status in {"pending", "deferred"} and not any(
        issue.code in {"missing_area", "unknown_area"} for issue in issues
    ):
        issues.append(ReviewIssue(code="metadata_review", label="登録情報要確認"))
    if mention.difference_type in {"new_pipeline_difference", "new_ambiguous"}:
        issues.append(ReviewIssue(code="identity_difference", label="店舗情報に不一致・候補あり"))
    if mention.message.fetch_error:
        issues.append(ReviewIssue(code="source_unavailable", label="元投稿の取得失敗"))
    if mention.message.processing_status == ProcessingStatus.FAILED.value:
        issues.append(ReviewIssue(code="processing_failed", label="登録処理失敗"))
    return issues


def _review_item(
    mention: ShopMention,
    review_group: ReviewGroupSummary | None,
) -> ReviewItemView:
    message = mention.message
    shop = mention.shop
    return ReviewItemView(
        id=mention.id,
        version=mention.version,
        message_id=mention.message_id,
        discord_url=_discord_url(message),
        source_created_at=message.source_created_at,
        message_content=message.content,
        processing_status=message.processing_status,
        fetch_error=message.fetch_error,
        extracted_name=mention.extracted_name,
        extracted_branch_name=mention.extracted_branch_name,
        extracted_area=mention.extracted_area,
        extracted_category=mention.extracted_category,
        source_url=mention.source_url,
        resolution_status=mention.resolution_status,
        review_status=mention.review_status,
        metadata_review_status=mention.metadata_review_status,
        difference_type=mention.difference_type,
        metadata_difference_type=mention.metadata_difference_type,
        extraction_source=mention.extraction_source,
        extraction_error=_extraction_error_display(mention.extraction_error),
        confidence_reason=mention.confidence_reason,
        issues=_review_issues(mention),
        shop=(
            ShopView(
                id=shop.id,
                version=shop.version,
                shop_name=shop.shop_name,
                branch_name=shop.branch_name,
                area=shop.area,
                category=shop.category,
                address=shop.address,
                phone=shop.phone,
                canonical_url=shop.canonical_url,
                is_visited=shop.is_visited,
                visited_at=shop.visited_at,
                rating=shop.rating,
                memo=shop.memo,
                image_key=shop.image_key,
            )
            if shop
            else None
        ),
        assets=[
            AssetView(
                id=asset.id,
                kind=asset.kind,
                url=asset.url,
                title=asset.title,
                description=asset.description,
                mime_type=asset.mime_type,
                extracted_text=asset.extracted_text,
                fetch_status=asset.fetch_status,
                photo_url=_photo_preview_url(asset),
                proxy_url=(
                    f"/api/admin/evidence/image/{asset.id}" if asset.kind == "image" else None
                ),
            )
            for asset in message.assets
        ],
        candidates=[_candidate_view(candidate) for candidate in mention.candidates],
        review_group_key=review_group.key if review_group is not None else None,
        review_group_count=(
            len(review_group.mention_ids) if review_group is not None else 1
        ),
        review_group_mention_ids=(
            list(review_group.mention_ids) if review_group is not None else [mention.id]
        ),
        review_group_reason=(review_group.reason if review_group is not None else None),
    )


@router.get("/admin/review")
def review_page(request: Request) -> Response:
    if not is_admin(request):
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/admin/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={"csrf_token": get_csrf_token(request), "category_values": CATEGORY_VALUES, "read_only": is_read_only()},
    )


@router.get("/api/admin/reviews", response_model=ReviewQueueResponse)
def review_queue(
    request: Request,
    response: Response,
    scope: str = Query(default="all"),
    status: str = Query(default=ReviewStatus.PENDING.value),
    reason: str | None = None,
    source: str | None = None,
    q: str | None = None,
    cursor: int | None = Query(default=None, ge=1),
    limit: int = Query(default=25, ge=1, le=50),
    db: Session = Depends(get_db),
) -> ReviewQueueResponse:
    response.headers["Cache-Control"] = "private, no-store"
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="管理者認証が必要です。")
    allowed_scopes = {"all", *(item.value for item in ReviewScope)}
    if scope not in allowed_scopes:
        raise HTTPException(status_code=400, detail=f"Invalid review scope: {scope}")
    review_scope = scope
    allowed_statuses = (
        {item.value for item in ReviewStatus}
        if review_scope in {"all", ReviewScope.IDENTITY}
        else {item.value for item in MetadataReviewStatus}
    )
    allowed_statuses.add("unresolved")
    if status not in allowed_statuses:
        raise HTTPException(status_code=400, detail=f"不正なデータ確認状態です: {status}")

    query = db.query(ShopMention)
    if review_scope == "all":
        query = query.filter(or_(_all_status_filter("pending"), _all_status_filter("deferred")) if status == "unresolved" else _all_status_filter(status))
    elif review_scope == ReviewScope.IDENTITY:
        query = query.filter(ShopMention.review_status.in_(["pending", "deferred"]) if status == "unresolved" else ShopMention.review_status == status)
    else:
        query = query.filter(ShopMention.metadata_review_status.in_(["pending", "deferred"]) if status == "unresolved" else ShopMention.metadata_review_status == status, _metadata_scope_eligible())
    if reason:
        if reason == "source_unavailable":
            query = query.join(Message, Message.message_id == ShopMention.message_id).filter(
                Message.fetch_error.isnot(None)
            )
        elif review_scope == "all":
            query = query.filter(or_(
                ShopMention.difference_type == reason,
                ShopMention.metadata_difference_type == reason,
            ))
        elif review_scope == ReviewScope.IDENTITY:
            query = query.filter(ShopMention.difference_type == reason)
        else:
            query = query.filter(ShopMention.metadata_difference_type == reason)
    if source:
        query = query.filter(ShopMention.extraction_source == source)
    if q:
        term = q.strip()
        query = query.outerjoin(Shop, Shop.id == ShopMention.shop_id).filter(
            or_(
                ShopMention.extracted_name.ilike(f"%{term}%"),
                Shop.shop_name.ilike(f"%{term}%"),
            )
        )
    total_count = query.count()
    if cursor:
        query = query.filter(ShopMention.id > cursor)
    mentions = query.order_by(ShopMention.id.asc()).limit(limit + 1).all()
    has_more = len(mentions) > limit
    visible = mentions[:limit]
    review_group_index = (
        build_open_review_group_index(db)
        if review_scope in {"all", ReviewScope.IDENTITY}
        else {}
    )
    return ReviewQueueResponse(
        scope=review_scope,
        total_count=total_count,
        counters=_counters(db, review_scope),
        items=[
            _review_item(mention, review_group_index.get(mention.id))
            for mention in visible
        ],
        next_cursor=visible[-1].id if has_more and visible else None,
    )


class ReviewFailureItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    message_content: str | None
    discord_url: str | None
    error: str
    mention_ids: list[int]
    review_mention_id: int | None = None
    recovery_status: Literal["not_prepared", "in_review", "handled"] = "not_prepared"


class ReviewFailureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReviewFailureItem]
    next_cursor: str | None


@router.get("/api/admin/review-failures", response_model=ReviewFailureResponse)
def review_failures(
    request: Request, response: Response,
    cursor: str | None = Query(default=None, pattern=r"^[1-9][0-9]{16,19}$"),
    limit: int = Query(default=25, ge=1, le=50), active_only: bool = False,
    db: Session = Depends(get_db),
) -> ReviewFailureResponse:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="管理者認証が必要です。")
    response.headers["Cache-Control"] = "private, no-store"
    query = db.query(Message).filter(Message.processing_status == ProcessingStatus.FAILED.value)
    if active_only:
        open_review = or_(
            ShopMention.review_status.in_(["pending", "deferred"]),
            and_(
                ShopMention.review_status == "approved",
                ShopMention.metadata_review_status.in_(["pending", "deferred"]),
            ),
        )
        query = query.filter(or_(~Message.mentions.any(), Message.mentions.any(open_review)))
    if cursor:
        query = query.filter(Message.message_id > cursor)
    messages = query.order_by(Message.message_id).limit(limit + 1).all()
    visible = messages[:limit]
    return ReviewFailureResponse(
        items=[ReviewFailureItem(
            message_id=message.message_id, message_content=message.content,
            discord_url=_discord_url(message),
            error="元投稿を取得できませんでした。" if message.fetch_error else "店舗情報の登録処理に失敗しました。",
            mention_ids=[mention.id for mention in message.mentions],
            review_mention_id=next((
                mention.id for mention in sorted(message.mentions, key=lambda item: item.id)
                if mention.review_status in {"pending", "deferred"} or (
                    mention.review_status == "approved" and mention.metadata_review_status in {"pending", "deferred"}
                )
            ), None),
            recovery_status=("not_prepared" if not message.mentions else "in_review" if any(
                mention.review_status in {"pending", "deferred"} or (
                    mention.review_status == "approved" and mention.metadata_review_status in {"pending", "deferred"}
                ) for mention in message.mentions
            ) else "handled"),
        ) for message in visible],
        next_cursor=visible[-1].message_id if len(messages) > limit else None,
    )


@router.post("/api/admin/review-failures/{message_id}/prepare", response_model=ReviewItemView)
def prepare_review_failure(
    message_id: str, request: Request, response: Response, db: Session = Depends(get_db),
) -> ReviewItemView:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="管理者認証が必要です。")
    require_writable()
    verify_csrf_token(request)
    response.headers["Cache-Control"] = "private, no-store"
    try:
        lock_new_shop_creation(db)
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("UPDATE messages SET is_target = is_target WHERE message_id = :message_id"),
                       {"message_id": message_id})
        message = db.query(Message).filter(Message.message_id == message_id).with_for_update().first()
        if message is None:
            raise HTTPException(status_code=404, detail="投稿が見つかりません。")
        if message.processing_status != ProcessingStatus.FAILED.value:
            raise HTTPException(status_code=409, detail="投稿の処理状態が変わりました。一覧を再読み込みしてください。")
        mention = db.query(ShopMention).filter(ShopMention.message_id == message_id).order_by(ShopMention.id).first()
        if mention is None:
            mention = ShopMention(
                message=message, occurrence_index=0, extracted_name="（店舗名未特定）",
                review_status=ReviewStatus.PENDING.value,
                metadata_review_status=MetadataReviewStatus.PENDING.value,
                metadata_difference_type="missing_area", difference_type="extraction_not_found",
                resolution_status="not_found", extraction_source="manual_recovery",
            )
            db.add(mention)
            db.flush()
            from db.models import ReviewEvent
            db.add(ReviewEvent(mention=mention, action="prepare_failed", note="失敗した投稿を手作業で確認する項目を作成"))
        db.commit()
        return _review_item(mention, None)
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("Failed review preparation: message_id=%s", message_id)
        raise HTTPException(status_code=500, detail="確認項目を準備できませんでした。再試行してください。") from exc


@router.get("/api/admin/reviews/{mention_id}", response_model=ReviewItemView)
def review_item(
    mention_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ReviewItemView:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Administrator authentication required")
    response.headers["Cache-Control"] = "private, no-store"
    mention = db.query(ShopMention).filter(ShopMention.id == mention_id).first()
    if mention is None:
        raise HTTPException(status_code=404, detail=f"Review item not found: mention_id={mention_id}")
    groups = build_open_review_group_index(db)
    return _review_item(mention, groups.get(mention.id))


@router.post(
    "/api/admin/reviews/{mention_id}/decision",
    response_model=ReviewDecisionResult,
)
async def review_decision(
    mention_id: int,
    decision: ReviewDecisionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ReviewDecisionResult:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="管理者認証が必要です。")
    require_writable()
    verify_csrf_token(request)
    try:
        photo = await prepare_review_photo(db, mention_id, decision) if isinstance(decision, EditAndApproveDecision) else None
        return await run_in_threadpool(apply_review_decision, db, mention_id, decision, photo=photo)
    except ReviewInvalidDecisionError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReviewNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReviewConflictError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
            headers={"Cache-Control": "private, no-store"},
        ) from exc
    except OSError as exc:
        db.rollback()
        logger.exception("Review photo storage failed: mention_id=%s", mention_id)
        raise HTTPException(status_code=503, detail="写真を保存できませんでした。入力を保持したまま再試行してください。") from exc
    except ReviewPhotoFetchError as exc:
        db.rollback()
        logger.exception("Review photo retrieval failed: mention_id=%s", mention_id)
        raise HTTPException(status_code=502, detail="写真を取得できませんでした。入力を保持したまま再試行してください。") from exc
    except Exception as exc:
        db.rollback()
        logger.exception("Review decision failed: mention_id=%s error=%s", mention_id, exc)
        raise HTTPException(
            status_code=500,
            detail=f"データ確認結果を保存できませんでした。mention_id={mention_id}",
        ) from exc


@router.get(
    "/api/admin/reviews/{mention_id}/link-preview",
    response_model=MentionLinkPreview,
)
def mention_link_preview(
    mention_id: int,
    request: Request,
    response: Response,
    target_shop_id: int = Query(gt=0),
    db: Session = Depends(get_db),
) -> MentionLinkPreview:
    response.headers["Cache-Control"] = "private, no-store"
    if not is_admin(request):
        raise HTTPException(
            status_code=403, detail="管理者認証が必要です。",
            headers={"Cache-Control": "private, no-store"},
        )
    try:
        return preview_mention_link(db, mention_id, target_shop_id)
    except ReviewNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=str(exc),
            headers={"Cache-Control": "private, no-store"},
        ) from exc
    except ReviewConflictError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)},
            headers={"Cache-Control": "private, no-store"},
        ) from exc


@router.post(
    "/api/admin/reviews/{mention_id}/link",
    response_model=MentionLinkResult,
)
def mention_link(
    mention_id: int,
    decision: LinkMentionRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> MentionLinkResult:
    response.headers["Cache-Control"] = "private, no-store"
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="管理者認証が必要です。")
    require_writable()
    verify_csrf_token(request)
    try:
        return link_mention(db, mention_id, decision)
    except ReviewInvalidDecisionError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReviewNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReviewConflictError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)},
            headers={"Cache-Control": "private, no-store"},
        ) from exc
    except Exception as exc:
        db.rollback()
        logger.exception(
            "Mention link failed: mention_id=%s target_shop_id=%s error=%s",
            mention_id, decision.target_shop_id, exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"投稿を関連付けできませんでした。mention_id={mention_id}",
        ) from exc


@router.get("/api/admin/shops/{shop_id}/merge-preview", response_model=ShopView)
def merge_preview(
    shop_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> ShopView:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="管理者認証が必要です。")
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if shop is None:
        raise HTTPException(status_code=404, detail=f"統合先店舗がありません: shop_id={shop_id}")
    return ShopView(
        id=shop.id,
        version=shop.version,
        shop_name=shop.shop_name,
        branch_name=shop.branch_name,
        area=shop.area,
        category=shop.category,
        address=shop.address,
        phone=shop.phone,
        canonical_url=shop.canonical_url,
        is_visited=shop.is_visited,
        visited_at=shop.visited_at,
        rating=shop.rating,
        memo=shop.memo,
    )


@router.get("/api/admin/evidence/image/{asset_id}")
async def evidence_image(
    asset_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="管理者認証が必要です。")
    asset = db.query(SourceAsset).filter(SourceAsset.id == asset_id).first()
    if asset is None or asset.kind != "image":
        raise HTTPException(status_code=404, detail="画像証跡がありません。")
    parsed = urlparse(asset.url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if parsed.scheme != "https" or host not in _DISCORD_IMAGE_HOSTS:
        raise HTTPException(status_code=400, detail="許可されていない画像URLです。")
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(asset.url)
        if response.status_code != 200:
            raise RuntimeError(f"status={response.status_code}")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/"):
            raise RuntimeError(f"content_type={content_type}")
        if len(response.content) > _MAX_IMAGE_BYTES:
            raise RuntimeError(f"size={len(response.content)}")
        return Response(
            content=response.content,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=300"},
        )
    except Exception as exc:
        logger.exception(
            "Evidence image fetch failed: asset_id=%s host=%s error=%s",
            asset_id,
            host,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail=f"画像証跡を取得できませんでした。asset_id={asset_id}",
        ) from exc


@router.get("/api/admin/evidence/photo/{asset_id}")
async def review_photo_preview(asset_id: int, request: Request, db: Session = Depends(get_db)) -> FileResponse:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="管理者認証が必要です。")
    asset = db.get(SourceAsset, asset_id)
    if asset is None or _photo_preview_url(asset) is None:
        raise HTTPException(status_code=404, detail="利用できる投稿写真がありません。")
    try:
        cached = await cache_shop_image(asset.url)
        return FileResponse(cached.file_path, media_type="image/webp", headers={"Cache-Control": "private, no-store"})
    except Exception as exc:
        logger.exception("Review photo preview failed: asset_id=%s", asset_id)
        raise HTTPException(status_code=502, detail="写真を取得できませんでした。再試行してください。") from exc
