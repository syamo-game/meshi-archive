from __future__ import annotations

import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError


TARGET_SIZE: tuple[int, int] = (960, 540)
MAX_DOWNLOAD_BYTES: int = 20 * 1024 * 1024
MAX_SOURCE_PIXELS: int = 40_000_000
MAX_REDIRECTS: int = 5
PUBLIC_IMAGE_PREFIX: str = "/media/shop-images"
ALLOWED_IMAGE_HOSTS: frozenset[str] = frozenset(
    {
        "pbs.twimg.com",
        "media.discordapp.net",
        "cdn.discordapp.com",
        "images-ext-1.discordapp.net",
        "images-ext-2.discordapp.net",
        "www.oimachi-tracks.com",
    }
)


@dataclass(frozen=True)
class CachedShopImage:
    source_url: str
    public_url: str
    file_path: Path
    width: int
    height: int
    already_cached: bool


def image_cache_dir() -> Path:
    configured = os.getenv("SHOP_IMAGE_CACHE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[1] / ".shop-image-cache").resolve()


def processed_image_filename(source_url: str) -> str:
    normalized = source_url.strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{digest}.webp"


def processed_image_path(source_url: str) -> Path:
    return image_cache_dir() / processed_image_filename(source_url)


def processed_image_public_url(source_url: str) -> str:
    return f"{PUBLIC_IMAGE_PREFIX}/{processed_image_filename(source_url)}"


def existing_processed_image_url(source_url: str) -> str | None:
    if processed_image_path(source_url).is_file():
        return processed_image_public_url(source_url)
    return None


def validate_image_source_url(source_url: str) -> str:
    cleaned = source_url.strip()
    try:
        parsed = urlparse(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid image source URL: source_url={source_url!r}") from exc
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or hostname not in ALLOWED_IMAGE_HOSTS
    ):
        raise ValueError(
            "Unsupported image source URL: "
            f"source_url={source_url!r} allowed_hosts={sorted(ALLOWED_IMAGE_HOSTS)}"
        )
    return cleaned


def crop_image_bytes(source_bytes: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(source_bytes)) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ValueError(
                    "Image dimensions exceed the processing limit: "
                    f"width={source.width} height={source.height} max_pixels={MAX_SOURCE_PIXELS}"
                )
            source.load()
            oriented = ImageOps.exif_transpose(source)
            fitted = ImageOps.fit(
                oriented,
                TARGET_SIZE,
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            if fitted.mode in {"RGBA", "LA"}:
                rgba = fitted.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                background.alpha_composite(rgba)
                output_image = background.convert("RGB")
            else:
                output_image = fitted.convert("RGB")
            output = io.BytesIO()
            output_image.save(output, format="WEBP", quality=84, method=6)
            return output.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(
            f"Downloaded content is not a processable image: bytes={len(source_bytes)}"
        ) from exc


async def _download_image(
    source_url: str,
    client: httpx.AsyncClient,
) -> bytes:
    current_url = validate_image_source_url(source_url)
    for redirect_count in range(MAX_REDIRECTS + 1):
        async with client.stream("GET", current_url, follow_redirects=False) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location or redirect_count == MAX_REDIRECTS:
                    raise RuntimeError(
                        "Image redirect could not be followed: "
                        f"source_url={source_url!r} current_url={current_url!r} "
                        f"status={response.status_code} location={location!r} "
                        f"redirects={redirect_count}"
                    )
                current_url = validate_image_source_url(urljoin(current_url, location))
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(
                    "Image download failed: "
                    f"source_url={source_url!r} final_url={current_url!r} "
                    f"status={response.status_code}"
                )
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if not content_type.startswith("image/"):
                raise RuntimeError(
                    "Image download returned an unexpected content type: "
                    f"source_url={source_url!r} final_url={current_url!r} "
                    f"status={response.status_code} content_type={content_type!r}"
                )
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        "Image download exceeded the size limit: "
                        f"source_url={source_url!r} final_url={current_url!r} "
                        f"bytes>{MAX_DOWNLOAD_BYTES}"
                    )
            return bytes(chunks)
    raise RuntimeError(f"Image redirect limit exceeded: source_url={source_url!r}")


async def cache_shop_image(
    source_url: str,
    *,
    client: httpx.AsyncClient | None = None,
    force: bool = False,
) -> CachedShopImage:
    validated_url = validate_image_source_url(source_url)
    output_path = processed_image_path(validated_url)
    public_url = processed_image_public_url(validated_url)
    if output_path.is_file() and not force:
        return CachedShopImage(
            source_url=validated_url,
            public_url=public_url,
            file_path=output_path,
            width=TARGET_SIZE[0],
            height=TARGET_SIZE[1],
            already_cached=True,
        )

    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    try:
        source_bytes = await _download_image(validated_url, active_client)
    finally:
        if owns_client:
            await active_client.aclose()

    processed_bytes = crop_image_bytes(source_bytes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(processed_bytes)
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return CachedShopImage(
        source_url=validated_url,
        public_url=public_url,
        file_path=output_path,
        width=TARGET_SIZE[0],
        height=TARGET_SIZE[1],
        already_cached=False,
    )
