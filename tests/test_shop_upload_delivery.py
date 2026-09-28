from __future__ import annotations

import io
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import Base, Message, Shop, ShopMention, SourceAsset
from services.shop_image_upload import UploadedShopImage, save_uploaded_image
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


@pytest.fixture
def photo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UploadedShopImage:
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path))
    with Image.new("RGB", (80, 120), "#357245") as source:
        output = io.BytesIO()
        source.save(output, format="PNG")
    return save_uploaded_image(output.getvalue())


def image_client(db: Session) -> TestClient:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="upload-delivery-test")
    app.include_router(shop_images.router)

    def session() -> Generator[Session, None, None]:
        yield db

    @app.post("/test-login/{role}")
    def login(role: str, request: Request) -> dict[str, bool]:
        request.session["authenticated"] = True
        request.session["admin_authenticated"] = role == "admin"
        return {"ok": True}

    app.dependency_overrides[home.get_db] = session
    return TestClient(app)


def add_shop(
    db: Session, photo: UploadedShopImage,
    *, identity_status: str = "approved", metadata_status: str = "approved",
) -> Shop:
    shop = Shop(shop_name="写真登録店", image_key=photo.image_key)
    db.add(ShopMention(
        message=Message(message_id="91111111111111111"), shop=shop, occurrence_index=0,
        extracted_name=shop.shop_name, review_status=identity_status,
        metadata_review_status=metadata_status,
    ))
    db.commit()
    return shop


def test_uploaded_photo_requires_login_and_delivers_the_saved_bytes(
    db: Session, photo: UploadedShopImage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(home, "WEB_PASSWORD", "required")
    add_shop(db, photo)
    with image_client(db) as client:
        assert client.get(photo.public_url).status_code == 401
        client.post("/test-login/member")
        response = client.get(photo.public_url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content == photo.file_path.read_bytes()
    assert db.query(SourceAsset).count() == 0


@pytest.mark.parametrize("identity_status,metadata_status", [
    ("pending", "approved"), ("approved", "pending"), ("pending", "pending"),
])
def test_uploaded_photo_is_private_until_both_reviews_are_approved(
    db: Session, photo: UploadedShopImage, monkeypatch: pytest.MonkeyPatch,
    identity_status: str, metadata_status: str,
) -> None:
    monkeypatch.setattr(home, "WEB_PASSWORD", "required")
    add_shop(db, photo, identity_status=identity_status, metadata_status=metadata_status)
    with image_client(db) as client:
        client.post("/test-login/member")
        assert client.get(photo.public_url).status_code == 404
        client.post("/test-login/admin")
        assert client.get(photo.public_url).status_code == 200


def test_uploaded_photo_cannot_combine_approvals_from_different_mentions(
    db: Session, photo: UploadedShopImage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ALLOW_ANONYMOUS_READ", "true")
    monkeypatch.setattr(home, "WEB_PASSWORD", None)
    shop = add_shop(db, photo, identity_status="pending")
    db.add(ShopMention(
        message=shop.mentions[0].message, shop=shop, occurrence_index=1,
        extracted_name=shop.shop_name, review_status="approved", metadata_review_status="pending",
    ))
    db.commit()
    with image_client(db) as client:
        assert client.get(photo.public_url).status_code == 404


def test_another_public_shop_in_the_same_post_does_not_publish_the_upload(
    db: Session, photo: UploadedShopImage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ALLOW_ANONYMOUS_READ", "true")
    monkeypatch.setattr(home, "WEB_PASSWORD", None)
    private_shop = add_shop(db, photo, identity_status="pending")
    message = private_shop.mentions[0].message
    public_shop = Shop(shop_name="同じ投稿の別店舗")
    db.add(ShopMention(
        message=message, shop=public_shop, occurrence_index=1, extracted_name=public_shop.shop_name,
        review_status="approved", metadata_review_status="approved",
    ))
    asset = SourceAsset(
        message=message, kind="image", url="https://pbs.twimg.com/media/original-post.png",
        fetch_status="available",
    )
    db.add(asset)
    db.commit()
    with image_client(db) as client:
        assert client.get(photo.public_url).status_code == 404
        private_shop.mentions[0].review_status = "approved"
        db.commit()
        assert client.get(photo.public_url).status_code == 200
    assert public_shop.image_key is None
    assert db.query(SourceAsset).one().url == "https://pbs.twimg.com/media/original-post.png"
    assert home._shop_links(public_shop, None)["image_url"] != photo.public_url


def test_admin_still_needs_a_current_shop_reference_and_existing_file(
    db: Session, photo: UploadedShopImage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(home, "WEB_PASSWORD", "required")
    with image_client(db) as client:
        client.post("/test-login/admin")
        assert client.get(photo.public_url).status_code == 404
        shop = add_shop(db, photo)
        assert client.get(photo.public_url).status_code == 200
        shop.image_key = "0" * 64
        db.commit()
        assert client.get(photo.public_url).status_code == 404
        assert client.get("/media/shop-uploads/" + "0" * 64 + ".webp").status_code == 404
        for filename in ("photo.webp", "..%2Fsecret", "..%5Csecret", "A" * 64 + ".webp"):
            assert client.get("/media/shop-uploads/" + filename).status_code == 404
    assert photo.file_path.is_file()


def test_deleting_a_shop_stops_delivery_without_removing_its_file(
    db: Session, photo: UploadedShopImage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ALLOW_ANONYMOUS_READ", "true")
    monkeypatch.setattr(home, "WEB_PASSWORD", None)
    shop = add_shop(db, photo)
    with image_client(db) as client:
        assert client.get(photo.public_url).status_code == 200
        db.delete(shop)
        db.commit()
        assert client.get(photo.public_url).status_code == 404
    assert photo.file_path.is_file()
