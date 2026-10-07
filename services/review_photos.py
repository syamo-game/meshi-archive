from __future__ import annotations

import logging

import httpx

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from db.models import ShopMention, SourceAsset
from services.review_service import (
    EditAndApproveDecision, PreparedReviewPhoto, ReviewConflictError,
    ReviewInvalidDecisionError, ReviewNotFoundError,
)
from services.shop_image_cache import cache_shop_image, validate_image_source_url
from services.shop_image_upload import UploadValidationError, save_uploaded_image

logger = logging.getLogger(__name__)


class ReviewPhotoFetchError(RuntimeError):
    pass


async def prepare_review_photo(
    db: Session, mention_id: int, decision: EditAndApproveDecision,
) -> PreparedReviewPhoto | None:
    if decision.photo_asset_id is None:
        return None
    mention = db.get(ShopMention, mention_id)
    if mention is None:
        raise ReviewNotFoundError("確認項目が見つかりません。")
    if mention.version != decision.expected_version:
        raise ReviewConflictError("確認項目が更新されています。", code="stale_mention")
    if mention.shop is not None and mention.shop.version != decision.shop_version:
        raise ReviewConflictError("店舗情報が更新されています。", code="stale_shop")
    asset = db.get(SourceAsset, decision.photo_asset_id)
    if asset is None or asset.message_id != mention.message_id or asset.kind != "image" or asset.fetch_status == "unavailable":
        raise ReviewInvalidDecisionError("選択した写真はこの元投稿の利用可能な画像ではありません。")
    source_url: str = asset.url
    message_id: str = mention.message_id
    try:
        validate_image_source_url(source_url)
    except ValueError as exc:
        logger.warning("Review photo source rejected: mention_id=%s asset_id=%s", mention_id, asset.id)
        raise ReviewInvalidDecisionError("この写真の取得先は利用できません。別の写真を選んでください。") from exc
    # Release the read transaction while downloading. Saving checks versions again.
    db.rollback()
    try:
        cached = await cache_shop_image(source_url)
    except (ValueError, httpx.HTTPError, RuntimeError) as exc:
        raise ReviewPhotoFetchError(f"Photo fetch failed: mention_id={mention_id} asset_id={decision.photo_asset_id}") from exc
    source_bytes: bytes = await run_in_threadpool(cached.file_path.read_bytes)
    try:
        uploaded = await run_in_threadpool(save_uploaded_image, source_bytes)
    except UploadValidationError as exc:
        raise ReviewInvalidDecisionError("写真を処理できませんでした。別の写真を選んでください。") from exc
    return PreparedReviewPhoto(decision.photo_asset_id, message_id, source_url, uploaded.image_key)
