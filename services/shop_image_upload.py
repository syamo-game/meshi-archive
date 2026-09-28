from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from services.shop_image_cache import MAX_SOURCE_PIXELS, crop_image_bytes, image_cache_dir


MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024
ALLOWED_UPLOAD_FORMATS: frozenset[str] = frozenset({"JPEG", "PNG", "WEBP"})
IMAGE_KEY: re.Pattern[str] = re.compile(r"[0-9a-f]{64}")


class UploadValidationError(ValueError):
    pass


@dataclass(frozen=True)
class UploadedShopImage:
    image_key: str
    public_url: str
    file_path: Path


def _validate_image_key(image_key: str) -> None:
    if IMAGE_KEY.fullmatch(image_key) is None:
        raise UploadValidationError("写真の保存キーが不正です。")


def uploaded_image_path(image_key: str) -> Path:
    _validate_image_key(image_key)
    return image_cache_dir() / "uploads" / f"{image_key}.webp"


def uploaded_image_public_url(image_key: str) -> str:
    _validate_image_key(image_key)
    return f"/media/shop-uploads/{image_key}.webp"


def _validate_source_image(source_bytes: bytes) -> None:
    if not source_bytes:
        raise UploadValidationError("写真ファイルが空です。別の写真を選んでください。")
    if len(source_bytes) > MAX_UPLOAD_BYTES:
        raise UploadValidationError("写真は20MiB以下のファイルを選んでください。")
    try:
        with Image.open(io.BytesIO(source_bytes)) as source:
            if source.format not in ALLOWED_UPLOAD_FORMATS:
                raise UploadValidationError("写真はJPEG・PNG・WebP形式を選んでください。")
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise UploadValidationError("写真は4,000万画素以下の画像を選んでください。")
            source.verify()
    except UploadValidationError:
        raise
    except Image.DecompressionBombError as exc:
        raise UploadValidationError("写真は4,000万画素以下の画像を選んでください。") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise UploadValidationError(
            "写真を読み取れませんでした。JPEG・PNG・WebP形式の正常な画像を選んでください。"
        ) from exc


def save_uploaded_image(source_bytes: bytes) -> UploadedShopImage:
    _validate_source_image(source_bytes)
    try:
        processed_bytes = crop_image_bytes(source_bytes)
    except ValueError as exc:
        raise UploadValidationError("写真を処理できませんでした。別の写真を選んでください。") from exc
    image_key = hashlib.sha256(processed_bytes).hexdigest()
    output_path = uploaded_image_path(image_key)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output_path.parent, prefix=f".{image_key}-", suffix=".tmp", delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(processed_bytes)
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return UploadedShopImage(
        image_key=image_key,
        public_url=uploaded_image_public_url(image_key),
        file_path=output_path,
    )
