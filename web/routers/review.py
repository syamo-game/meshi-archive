from __future__ import annotations

import logging
import os
from collections.abc import Generator
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

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
    ReviewConflictError,
    ReviewDecisionRequest,
    ReviewDecisionResult,
    ReviewInvalidDecisionError,
    ReviewNotFoundError,
    apply_review_decision,
)
from services.mention_reevaluation import (
    ReviewGroupSummary,
    build_open_review_group_index,
)
from web.auth import is_admin
from web.csrf import get_csrf_token, verify_csrf_token
from web.read_only import require_writable


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


class ReviewCounters(BaseModel):
    """Global diagnostics, independent of queue filters and pagination.

    Status counts use the selected scope. Statuses and source_unavailable count
    mentions; failed counts messages and can overlap with source_unavailable.
    """

    model_config = ConfigDict(extra="forbid")

    pending: int
    approved: int
    deferred: int
    rejected: int
    source_unavailable: int
    failed: int


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
    shop: ShopView | None
    assets: list[AssetView]
    candidates: list[CandidateView]
    review_group_key: str | None
    review_group_count: int
    review_group_mention_ids: list[int]
    review_group_reason: str | None


class ReviewQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["identity", "metadata"]
    total_count: int = Field(
        ge=0,
        description="Matching mentions before cursor and limit are applied.",
    )
    counters: ReviewCounters = Field(
        description="Global diagnostics; only status counts depend on scope.",
    )
    items: list[ReviewItemView]
    next_cursor: int | None


def _counters(db: Session, scope: ReviewScope) -> ReviewCounters:
    if scope == ReviewScope.IDENTITY:
        counts = dict(
            db.query(ShopMention.review_status, func.count(ShopMention.id))
            .group_by(ShopMention.review_status)
            .all()
        )
    else:
        counts = dict(
            db.query(ShopMention.metadata_review_status, func.count(ShopMention.id))
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
        extraction_error=mention.extraction_error,
        confidence_reason=mention.confidence_reason,
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
        context={"csrf_token": get_csrf_token(request)},
    )


@router.get("/api/admin/reviews", response_model=ReviewQueueResponse)
def review_queue(
    request: Request,
    scope: str = Query(default=ReviewScope.IDENTITY.value),
    status: str = Query(default=ReviewStatus.PENDING.value),
    reason: str | None = None,
    source: str | None = None,
    q: str | None = None,
    cursor: int | None = Query(default=None, ge=1),
    limit: int = Query(default=25, ge=1, le=50),
    db: Session = Depends(get_db),
) -> ReviewQueueResponse:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="管理者認証が必要です。")
    allowed_scopes = {item.value for item in ReviewScope}
    if scope not in allowed_scopes:
        raise HTTPException(status_code=400, detail=f"Invalid review scope: {scope}")
    review_scope = ReviewScope(scope)
    allowed_statuses = (
        {item.value for item in ReviewStatus}
        if review_scope == ReviewScope.IDENTITY
        else {item.value for item in MetadataReviewStatus}
    )
    if status not in allowed_statuses:
        raise HTTPException(status_code=400, detail=f"不正なデータ確認状態です: {status}")

    query = db.query(ShopMention)
    if review_scope == ReviewScope.IDENTITY:
        query = query.filter(ShopMention.review_status == status)
    else:
        query = query.filter(ShopMention.metadata_review_status == status)
    if reason:
        if reason == "source_unavailable":
            query = query.join(Message, Message.message_id == ShopMention.message_id).filter(
                Message.fetch_error.isnot(None)
            )
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
        if review_scope == ReviewScope.IDENTITY
        else {}
    )
    return ReviewQueueResponse(
        scope=review_scope.value,
        total_count=total_count,
        counters=_counters(db, review_scope),
        items=[
            _review_item(mention, review_group_index.get(mention.id))
            for mention in visible
        ],
        next_cursor=visible[-1].id if has_more and visible else None,
    )


@router.post(
    "/api/admin/reviews/{mention_id}/decision",
    response_model=ReviewDecisionResult,
)
def review_decision(
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
        return apply_review_decision(db, mention_id, decision)
    except ReviewInvalidDecisionError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReviewNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReviewConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        logger.exception("Review decision failed: mention_id=%s error=%s", mention_id, exc)
        raise HTTPException(
            status_code=500,
            detail=f"データ確認結果を保存できませんでした。mention_id={mention_id}",
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
