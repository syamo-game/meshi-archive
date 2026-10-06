from __future__ import annotations

import asyncio
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from services.shop_image_cache import TARGET_SIZE, cache_shop_image


def source_png() -> bytes:
    image = Image.new("RGB", (400, 800), "#2f855a")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("source_url", [
    "https://pbs.twimg.com/media/example.png",
    "https://www.oimachi-tracks.com/images/shop-photo.png",
])
def test_cache_shop_image_crops_and_reuses_a_local_webp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_url: str,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=source_png(),
        )

    async def scenario() -> tuple[bool, bool, str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await cache_shop_image(
                source_url,
                client=client,
            )
            second = await cache_shop_image(
                source_url,
                client=client,
            )
        with Image.open(first.file_path) as processed:
            assert processed.size == TARGET_SIZE
            assert processed.format == "WEBP"
        return first.already_cached, second.already_cached, first.public_url

    first_cached, second_cached, public_url = asyncio.run(scenario())

    assert first_cached is False
    assert second_cached is True
    assert public_url.startswith("/media/shop-images/")
    assert requests == [source_url]


@pytest.mark.parametrize("source_url", [
    "https://example.com/food.jpg",
    "https://www.oimachi-tracks.com.attacker.example/food.jpg",
    "https://www-oimachi-tracks.com/food.jpg",
    "https://images.oimachi-tracks.com/food.jpg",
])
def test_cache_shop_image_rejects_unapproved_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_url: str,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="Unsupported image source URL"):
        asyncio.run(cache_shop_image(source_url))


def test_redirect_is_validated_before_the_destination_is_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await cache_shop_image("https://pbs.twimg.com/media/source.png", client=client)

    with pytest.raises(ValueError, match="Unsupported image source URL"):
        asyncio.run(scenario())
    assert requests == ["https://pbs.twimg.com/media/source.png"]
    assert list(tmp_path.iterdir()) == []


def test_allowed_redirect_downloads_and_caches_the_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/media/source.png":
            return httpx.Response(302, headers={"location": "/media/photo.png"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=source_png())

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            image = await cache_shop_image("https://pbs.twimg.com/media/source.png", client=client)
        with Image.open(image.file_path) as cached:
            assert cached.size == TARGET_SIZE

    asyncio.run(scenario())
    assert requests == [
        "https://pbs.twimg.com/media/source.png",
        "https://pbs.twimg.com/media/photo.png",
    ]
