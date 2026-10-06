from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from db.models import (
    AssetKind,
    FetchStatus,
    MetadataReviewStatus,
    ReviewStatus,
    Shop,
    ShopMention,
    SourceAsset,
)
from services.shop_image_cache import cache_shop_image, processed_image_filename
from services.shop_image_upload import uploaded_image_path
from web.auth import is_admin
from web.routers.home import _is_authenticated, get_db


router = APIRouter()
logger = logging.getLogger(__name__)
IMAGE_FILENAME: re.Pattern[str] = re.compile(r"[0-9a-f]{64}\.webp")


@router.get("/media/shop-images/{filename}")
async def shop_image(
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
) -> FileResponse:
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="ログインが必要です。")
    if IMAGE_FILENAME.fullmatch(filename) is None:
        raise HTTPException(status_code=404, detail="画像が見つかりません。")
    assets: list[SourceAsset] = (
        db.query(SourceAsset)
        .join(ShopMention, ShopMention.message_id == SourceAsset.message_id)
        .filter(
            SourceAsset.kind == AssetKind.IMAGE.value,
            SourceAsset.fetch_status != FetchStatus.UNAVAILABLE.value,
            ShopMention.shop_id.is_not(None),
            ShopMention.review_status == ReviewStatus.APPROVED.value,
            ShopMention.metadata_review_status == MetadataReviewStatus.APPROVED.value,
        )
        .distinct()
        .all()
    )
    asset: SourceAsset | None = next(
        (item for item in assets if processed_image_filename(item.url) == filename),
        None,
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="画像が見つかりません。")
    try:
        image = await cache_shop_image(asset.url)
    except Exception as exc:
        logger.exception(
            "Shop image delivery failed: asset_id=%s message_id=%s filename=%s error=%s",
            asset.id,
            asset.message_id,
            filename,
            exc,
        )
        raise HTTPException(status_code=502, detail="写真を取得できませんでした。") from exc
    return FileResponse(
        image.file_path,
        media_type="image/webp",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/media/shop-uploads/{filename}")
def uploaded_shop_image(
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
) -> FileResponse:
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="ログインが必要です。")
    if IMAGE_FILENAME.fullmatch(filename) is None:
        raise HTTPException(status_code=404, detail="画像が見つかりません。")
    image_key = filename.removesuffix(".webp")
    query = db.query(Shop).filter(Shop.image_key == image_key)
    if not is_admin(request):
        query = query.join(ShopMention, ShopMention.shop_id == Shop.id).filter(
            ShopMention.review_status == ReviewStatus.APPROVED.value,
            ShopMention.metadata_review_status == MetadataReviewStatus.APPROVED.value,
        )
    if query.first() is None:
        raise HTTPException(status_code=404, detail="画像が見つかりません。")
    image_path = uploaded_image_path(image_key)
    if not image_path.is_file():
        raise HTTPException(status_code=404, detail="画像が見つかりません。")
    return FileResponse(
        image_path,
        media_type="image/webp",
        headers={"Cache-Control": "private, no-store"},
    )
