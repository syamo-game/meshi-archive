from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image

from services import shop_image_upload
from services.shop_image_upload import (
    MAX_UPLOAD_BYTES,
    UploadValidationError,
    save_uploaded_image,
    uploaded_image_path,
    uploaded_image_public_url,
)


def image_bytes(format_name: str = "PNG") -> bytes:
    with Image.new("RGB", (80, 120), "#3c8844") as source:
        output = io.BytesIO()
        source.save(output, format=format_name)
        return output.getvalue()


@pytest.mark.parametrize("format_name", ["JPEG", "PNG", "WEBP"])
def test_upload_validates_real_image_and_stores_a_content_keyed_webp(
    format_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    saved = save_uploaded_image(image_bytes(format_name))
    processed = saved.file_path.read_bytes()
    assert saved.image_key == hashlib.sha256(processed).hexdigest()
    assert saved.file_path == tmp_path / "uploads" / f"{saved.image_key}.webp"
    assert saved.public_url == f"/media/shop-uploads/{saved.image_key}.webp"
    with Image.open(io.BytesIO(processed)) as image:
        assert image.format == "WEBP"
        assert image.size == (960, 540)


def test_identical_uploads_reuse_the_same_key_without_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    first = save_uploaded_image(image_bytes())
    second = save_uploaded_image(image_bytes())
    assert first == second
    assert list((tmp_path / "uploads").iterdir()) == [first.file_path]


@pytest.mark.parametrize("format_name", ["GIF", "BMP", "TIFF"])
def test_upload_rejects_other_real_image_formats(
    format_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    with pytest.raises(UploadValidationError, match="JPEG・PNG・WebP"):
        save_uploaded_image(image_bytes(format_name))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("source_bytes", [
    b"", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
    b"<html>not a photograph.jpg</html>", b"\xff\xd8\xff\xe0broken JPEG",
])
def test_upload_rejects_empty_or_invalid_image_data(
    source_bytes: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    with pytest.raises(UploadValidationError):
        save_uploaded_image(source_bytes)
    assert list(tmp_path.iterdir()) == []


def test_upload_rejects_a_truncated_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    with pytest.raises(UploadValidationError, match="写真を読み取れませんでした"):
        save_uploaded_image(image_bytes()[:60])
    assert list(tmp_path.iterdir()) == []


def test_upload_rejects_files_above_twenty_mib_before_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    with pytest.raises(UploadValidationError, match="20MiB以下"):
        save_uploaded_image(b"x" * (MAX_UPLOAD_BYTES + 1))
    assert list(tmp_path.iterdir()) == []


def test_upload_rejects_more_than_forty_million_pixels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    with Image.new("1", (4000, 10001)) as source:
        output = io.BytesIO()
        source.save(output, format="PNG")
    with pytest.raises(UploadValidationError, match="4,000万画素以下"):
        save_uploaded_image(output.getvalue())
    assert list(tmp_path.iterdir()) == []


def test_upload_applies_exif_orientation_before_cropping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    with Image.new("RGB", (120, 60), "red") as source:
        source.paste("blue", (60, 0, 120, 60))
        exif = Image.Exif()
        exif[274] = 6
        output = io.BytesIO()
        source.save(output, format="JPEG", exif=exif)
    saved = save_uploaded_image(output.getvalue())
    with Image.open(saved.file_path) as image:
        rgb = image.convert("RGB")
        upper = rgb.getpixel((480, 100))
        lower = rgb.getpixel((480, 440))
        assert upper[0] > upper[2] + 100
        assert lower[2] > lower[0] + 100
        assert image.getexif().get(274) is None


@pytest.mark.parametrize("key", ["", "../photo", "a" * 63, "A" * 64, "a" * 64 + ".webp", "..\\photo"])
def test_uploaded_image_helpers_reject_unsafe_keys(key: str) -> None:
    with pytest.raises(UploadValidationError, match="保存キーが不正"):
        uploaded_image_path(key)
    with pytest.raises(UploadValidationError, match="保存キーが不正"):
        uploaded_image_public_url(key)


def test_upload_preserves_io_errors_and_removes_an_unpublished_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))

    def fail_replace(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent == tmp_path / "uploads"
        assert source.is_file()
        raise OSError("storage unavailable")

    monkeypatch.setattr(shop_image_upload.os, "replace", fail_replace)
    with pytest.raises(OSError, match="storage unavailable"):
        save_uploaded_image(image_bytes())
    assert list((tmp_path / "uploads").iterdir()) == []
