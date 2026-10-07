from __future__ import annotations

import io
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import Base, Message, Shop, ShopMention, SourceAsset
from services import shop_image_cache
from web.routers import home, shop_images


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_image(
    db: Session,
    *,
    review_status: str = "approved",
    metadata_review_status: str = "approved",
    source_url: str = "https://pbs.twimg.com/media/delivery.png",
) -> SourceAsset:
    message = Message(message_id="90000000000000091")
    shop = Shop(shop_name="写真確認店")
    db.add(ShopMention(
        shop=shop, message=message, occurrence_index=0, extracted_name=shop.shop_name,
        review_status=review_status, metadata_review_status=metadata_review_status,
    ))
    asset = SourceAsset(message=message, kind="image", url=source_url, fetch_status="available")
    db.add(asset)
    db.commit()
    return asset


def image_client(db: Session) -> TestClient:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="image-delivery-test")
    app.include_router(shop_images.router)

    @app.post("/test-login/member")
    def login(request: Request) -> dict[str, bool]:
        request.session.update(authenticated=True, discord_user_id="123456789012345678")
        return {"ok": True}

    def session() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[home.get_db] = session
    return TestClient(app)


def png_bytes() -> bytes:
    image = Image.new("RGB", (40, 80), "#246824")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_first_image_request_downloads_saves_and_reuses_the_processed_photo(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")

    asset = add_image(db)
    requests: list[str] = []
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png_bytes())

    def download_client(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(shop_image_cache.httpx, "AsyncClient", download_client)
    with image_client(db) as client:
        client.post("/test-login/member")
        url = shop_image_cache.processed_image_public_url(asset.url)
        first = client.get(url)
        second = client.get(url)
    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"] == "image/webp"
    assert first.content == second.content
    with Image.open(io.BytesIO(first.content)) as image:
        assert image.size == (960, 540)
    assert requests == [asset.url]
    assert shop_image_cache.processed_image_path(asset.url).is_file()


@pytest.mark.parametrize(
    ("review_status", "metadata_status"),
    [("pending", "approved"), ("approved", "pending"), ("pending", "pending")],
)
def test_image_needs_both_approvals_even_when_a_cached_file_exists(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    review_status: str, metadata_status: str,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")

    asset = add_image(db, review_status=review_status, metadata_review_status=metadata_status)
    cached = shop_image_cache.processed_image_path(asset.url)
    cached.write_bytes(shop_image_cache.crop_image_bytes(png_bytes()))
    with image_client(db) as client:
        client.post("/test-login/member")
        response = client.get(shop_image_cache.processed_image_public_url(asset.url))
    assert response.status_code == 404


def test_images_from_separately_approved_mentions_are_not_public(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")

    asset = add_image(db, review_status="pending")
    db.add(ShopMention(
        message=asset.message, shop=Shop(shop_name="別の候補"), occurrence_index=1,
        extracted_name="別の候補", review_status="approved", metadata_review_status="pending",
    ))
    db.commit()
    with image_client(db) as client:
        client.post("/test-login/member")
        response = client.get(shop_image_cache.processed_image_public_url(asset.url))
    assert response.status_code == 404


def test_image_delivery_requires_login_and_a_registered_safe_filename(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = add_image(db)
    with image_client(db) as client:
        assert client.get(shop_image_cache.processed_image_public_url(asset.url)).status_code == 401
        monkeypatch.setenv("APP_ENV", "development")

        client.post("/test-login/member")
        for filename in ("0" * 64 + ".webp", "..%2Fsecret", "photo.webp", "..%5Csecret"):
            assert client.get("/media/shop-images/" + filename).status_code == 404


def test_failed_download_is_logged_and_can_be_retried(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")

    asset = add_image(db)
    original_client = httpx.AsyncClient
    request_count: int = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(503, text="upstream unavailable")
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png_bytes())

    def download_client(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(shop_image_cache.httpx, "AsyncClient", download_client)
    with image_client(db) as client:
        client.post("/test-login/member")
        url = shop_image_cache.processed_image_public_url(asset.url)
        failed = client.get(url)
        assert failed.status_code == 502
        assert not shop_image_cache.processed_image_path(asset.url).exists()
        assert client.get(url).status_code == 200
    assert f"asset_id={asset.id}" in caplog.text
    assert "status=503" in caplog.text
    assert "pbs.twimg.com" not in failed.text
