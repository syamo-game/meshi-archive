from __future__ import annotations

import io
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from db.models import Base, Message, Shop, ShopMention, SourceAsset
from services.shop_image_cache import processed_image_public_url
from services.shop_image_upload import MAX_UPLOAD_BYTES, UploadedShopImage, save_uploaded_image
from services.admin_sessions import issue_admin_session
from web.routers import admin, home, shop_images


SOURCE_URL = "https://pbs.twimg.com/media/shared-photo.jpg"
XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def photo_bytes(image_format: str = "PNG", color: str = "#235781") -> bytes:
    output = io.BytesIO()
    with Image.new("RGB", (120, 200), color) as photo:
        photo.save(output, format=image_format)
    return output.getvalue()


@dataclass(frozen=True)
class PhotoEditor:
    client: TestClient
    sessions: sessionmaker[Session]
    shop_id: int
    other_shop_id: int
    original_photo: UploadedShopImage
    created_at: datetime


@pytest.fixture
def editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[PhotoEditor, None, None]:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(tmp_path / "photos"))
    original = save_uploaded_image(photo_bytes(color="#792451"))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        message = Message(message_id="93333333333333333")
        message.assets.append(SourceAsset(kind="image", url=SOURCE_URL, fetch_status="available"))
        shops = [
            Shop(shop_name="元の店名", area="銀座", image_key=original.image_key,
                 created_at=datetime(2020, 1, 2, 3, 4, 5)),
            Shop(shop_name="同じ投稿の別店舗", area="銀座"),
        ]
        for index, shop in enumerate(shops):
            db.add(ShopMention(
                message=message, shop=shop, occurrence_index=index, extracted_name=shop.shop_name,
                review_status="approved", metadata_review_status="approved", resolution_status="resolved",
            ))
        db.commit()
        shop_id, other_shop_id = (shop.id for shop in shops)
        created_at = shops[0].created_at

    def get_db() -> Generator[Session, None, None]:
        with sessions() as db:
            yield db

    app = FastAPI()

    @app.middleware("http")
    async def add_session(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.scope["session"] = {
            "authenticated": request.headers.get("x-test-role", "admin") != "anonymous",
            "admin_authenticated": request.headers.get("x-test-role", "admin") == "admin",
            "csrf_token": "photo-token",
        }
        return await call_next(request)

    app.include_router(home.router)
    app.include_router(shop_images.router)
    app.dependency_overrides[home.get_db] = get_db
    try:
        with TestClient(app) as client:
            yield PhotoEditor(client, sessions, shop_id, other_shop_id, original, created_at)
    finally:
        engine.dispose()


def edit_payload() -> dict[str, str]:
    return {
        "shop_name": "変更した店名", "area": "銀座", "category": "カフェ",
        "memo": "写真と一緒に保存\n2行目", "rating": "4", "csrf_token": "photo-token",
        "is_visited": "on", "visited_at": "2026-09-01", "return_to": "/?sort=created_desc&page=3",
        "expected_version": "1",
    }


def assert_original_shop(editor: PhotoEditor) -> None:
    with editor.sessions() as db:
        shop = db.get(Shop, editor.shop_id)
        assert shop is not None
        assert shop.image_key == editor.original_photo.image_key
        assert shop.shop_name == "元の店名"
        assert shop.memo is None
        assert shop.version == 1
        assert shop.created_at == editor.created_at
    assert editor.original_photo.file_path.is_file()


def test_edit_snapshot_is_admin_only_and_matches_the_current_shop_image(editor: PhotoEditor) -> None:
    with editor.sessions() as db:
        saved = {
            name: [tuple(row) for row in db.execute(table.select()).all()]
            for name, table in Base.metadata.tables.items()
        }
    for role in ("member", "anonymous"):
        denied = editor.client.get(
            f"/shop/{editor.shop_id}/edit-snapshot", headers={"x-test-role": role},
        )
        assert denied.status_code == 403
        assert "values" not in denied.json() and "version" not in denied.json()
        assert editor.original_photo.public_url not in denied.text
    snapshot = editor.client.get(f"/shop/{editor.shop_id}/edit-snapshot")
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "private, no-store"
    assert snapshot.json()["image_url"] == editor.original_photo.public_url
    assert snapshot.json()["image_url"] in editor.client.get(f"/shop/{editor.shop_id}").text
    other = editor.client.get(f"/shop/{editor.other_shop_id}/edit-snapshot")
    assert other.status_code == 200
    assert other.json()["image_url"] is None
    with editor.sessions() as db:
        assert {
            name: [tuple(row) for row in db.execute(table.select()).all()]
            for name, table in Base.metadata.tables.items()
        } == saved
    assert list(editor.original_photo.file_path.parent.glob("*.webp")) == [editor.original_photo.file_path]


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
def test_upload_saves_fields_and_photo_for_only_the_edited_shop(editor: PhotoEditor, image_format: str) -> None:
    response = editor.client.post(
        f"/shop/{editor.shop_id}/edit", data=edit_payload(),
        files={"photo": ("../untrusted-name.bin", photo_bytes(image_format), "application/octet-stream")},
        headers=XHR_HEADERS,
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["redirect_url"].startswith(f"/shop/{editor.shop_id}?saved=true&return_to=")
    with editor.sessions() as db:
        shop = db.get(Shop, editor.shop_id)
        other = db.get(Shop, editor.other_shop_id)
        assert shop is not None and other is not None
        assert shop.shop_name == "変更した店名"
        assert shop.memo == edit_payload()["memo"]
        assert shop.rating == 4 and shop.is_visited
        assert shop.visited_at is not None and shop.visited_at.day == 1
        assert shop.version == 2
        assert shop.created_at == editor.created_at
        assert shop.image_key is not None and shop.image_key != editor.original_photo.image_key
        assert other.image_key is None
        assert other.shop_name == "同じ投稿の別店舗"
        assert db.query(SourceAsset).count() == 1
        assert db.query(SourceAsset).one().url == SOURCE_URL
        image_url = f"/media/shop-uploads/{shop.image_key}.webp"
    image_response = editor.client.get(image_url)
    assert image_response.status_code == 200
    with Image.open(io.BytesIO(image_response.content)) as uploaded:
        assert uploaded.format == "WEBP" and uploaded.size == (960, 540)
    assert image_url in editor.client.get("/").text
    assert image_url in editor.client.get(f"/shop/{editor.shop_id}").text
    other_html = editor.client.get(f"/shop/{editor.other_shop_id}").text
    assert image_url not in other_html
    assert processed_image_public_url(SOURCE_URL) not in other_html
    assert editor.original_photo.file_path.is_file()
    assert editor.client.get(editor.original_photo.public_url).status_code == 404


@pytest.mark.parametrize("empty_file", [False, True])
def test_edit_without_new_photo_keeps_the_current_photo(editor: PhotoEditor, empty_file: bool) -> None:
    response = editor.client.post(
        f"/shop/{editor.shop_id}/edit", data=edit_payload(), headers=XHR_HEADERS,
        files={"photo": ("", b"", "application/octet-stream")} if empty_file else None,
    )
    assert response.status_code == 200
    with editor.sessions() as db:
        shop = db.get(Shop, editor.shop_id)
        assert shop is not None
        assert shop.image_key == editor.original_photo.image_key
        assert shop.shop_name == "変更した店名"


@pytest.mark.parametrize("invalid_photo", [b"not an image", b"", b"x" * (MAX_UPLOAD_BYTES + 1)],
                         ids=["invalid-content", "empty", "too-large"])
def test_invalid_upload_preserves_the_current_photo_and_fields(editor: PhotoEditor, invalid_photo: bytes) -> None:
    response = editor.client.post(
        f"/shop/{editor.shop_id}/edit", data=edit_payload(), headers=XHR_HEADERS,
        files={"photo": ("invalid.jpg", invalid_photo, "image/jpeg")},
    )
    assert response.status_code == 400
    assert "photo" in response.json()["errors"]
    assert_original_shop(editor)


def test_invalid_text_does_not_store_an_uploaded_photo(editor: PhotoEditor) -> None:
    payload = edit_payload()
    payload["shop_name"] = " "
    response = editor.client.post(
        f"/shop/{editor.shop_id}/edit", data=payload, headers=XHR_HEADERS,
        files={"photo": ("new.png", photo_bytes(), "image/png")},
    )
    assert response.status_code == 400
    assert response.json()["errors"] == {"shop_name": "店名を入力してください。"}
    assert list(editor.original_photo.file_path.parent.glob("*.webp")) == [editor.original_photo.file_path]
    assert_original_shop(editor)


@pytest.mark.parametrize("version", [None, "invalid", "1"])
def test_stale_or_invalid_version_never_stores_an_uploaded_photo(
    editor: PhotoEditor, monkeypatch: pytest.MonkeyPatch, version: str | None,
) -> None:
    other_payload = edit_payload()
    other_payload["shop_name"] = "Newer saved name"
    other_payload["memo"] = "Newer saved memo"
    saved = editor.client.post(
        f"/shop/{editor.shop_id}/edit", data=other_payload, headers=XHR_HEADERS,
    )
    assert saved.status_code == 200

    def unexpected_photo_storage(source_bytes: bytes) -> UploadedShopImage:
        raise AssertionError("A rejected version must not write photo storage")

    monkeypatch.setattr(home, "save_uploaded_image", unexpected_photo_storage)
    payload = edit_payload()
    if version is None:
        del payload["expected_version"]
    else:
        payload["expected_version"] = version
    response = editor.client.post(
        f"/shop/{editor.shop_id}/edit", data=payload, headers=XHR_HEADERS,
        files={"photo": ("new.png", photo_bytes(), "image/png")},
    )
    assert response.status_code == 409
    with editor.sessions() as db:
        shop = db.get(Shop, editor.shop_id)
        assert shop is not None and shop.version == 2
        assert shop.shop_name == "Newer saved name" and shop.memo == "Newer saved memo"
        assert shop.image_key == editor.original_photo.image_key
        assert shop.created_at == editor.created_at
        assert db.query(SourceAsset).one().url == SOURCE_URL
    assert list(editor.original_photo.file_path.parent.glob("*.webp")) == [editor.original_photo.file_path]


def test_invalid_photo_returns_field_error_and_retained_text_in_html(editor: PhotoEditor) -> None:
    response = editor.client.post(
        f"/shop/{editor.shop_id}/edit", data=edit_payload(),
        files={"photo": ("bad.png", b"invalid", "image/png")},
    )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert 'value="変更した店名"' in response.text
    assert "写真と一緒に保存\n2行目" in response.text
    assert 'aria-describedby="shop-photo-help shop-photo-error"' in response.text
    assert editor.original_photo.public_url in response.text
    assert_original_shop(editor)


def test_storage_failure_keeps_old_photo_and_allows_identical_retry(
    editor: PhotoEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_path = tmp_path / "not-a-directory"
    blocked_path.write_bytes(b"storage failure")
    with monkeypatch.context() as patch:
        patch.setenv("SHOP_IMAGE_CACHE_DIR", str(blocked_path))
        response = editor.client.post(
            f"/shop/{editor.shop_id}/edit", data=edit_payload(), headers=XHR_HEADERS,
            files={"photo": ("new.png", photo_bytes(), "image/png")},
        )
    assert response.status_code == 503
    assert "写真を保存できませんでした" in response.json()["detail"]
    assert str(blocked_path) not in response.text
    assert_original_shop(editor)
    retry = editor.client.post(
        f"/shop/{editor.shop_id}/edit", data=edit_payload(), headers=XHR_HEADERS,
        files={"photo": ("new.png", photo_bytes(), "image/png")},
    )
    assert retry.status_code == 200


def test_database_failure_rolls_back_photo_and_fields_then_retries(editor: PhotoEditor) -> None:
    def fail_commit(session: Session) -> None:
        raise OperationalError("commit fault for test", (), RuntimeError("storage unavailable"))

    event.listen(editor.sessions, "before_commit", fail_commit)
    try:
        response = editor.client.post(
            f"/shop/{editor.shop_id}/edit", data=edit_payload(), headers=XHR_HEADERS,
            files={"photo": ("new.png", photo_bytes(), "image/png")},
        )
    finally:
        event.remove(editor.sessions, "before_commit", fail_commit)
    assert response.status_code == 503
    assert "保存できませんでした" in response.json()["detail"]
    assert "storage unavailable" not in response.text
    assert_original_shop(editor)
    unreferenced = set(editor.original_photo.file_path.parent.glob("*.webp")) - {editor.original_photo.file_path}
    assert len(unreferenced) == 1
    assert editor.client.get(f"/media/shop-uploads/{unreferenced.pop().name}").status_code == 404
    retry = editor.client.post(
        f"/shop/{editor.shop_id}/edit", data=edit_payload(), headers=XHR_HEADERS,
        files={"photo": ("new.png", photo_bytes(), "image/png")},
    )
    assert retry.status_code == 200
    with editor.sessions() as db:
        shop = db.get(Shop, editor.shop_id)
        assert shop is not None and shop.image_key != editor.original_photo.image_key
        assert shop.shop_name == "変更した店名" and shop.version == 2


@pytest.mark.parametrize("failure,status", [("auth", 401), ("csrf", 403), ("readonly", 503)])
def test_upload_requires_admin_csrf_and_writable_mode(
    editor: PhotoEditor, monkeypatch: pytest.MonkeyPatch, failure: str, status: int,
) -> None:
    headers = dict(XHR_HEADERS)
    payload = edit_payload()
    if failure == "auth":
        headers["x-test-role"] = "member"
    elif failure == "csrf":
        payload["csrf_token"] = "invalid"
    else:
        monkeypatch.setenv("APP_READ_ONLY", "true")
    response = editor.client.post(
        f"/shop/{editor.shop_id}/edit", data=payload, headers=headers,
        files={"photo": ("new.png", photo_bytes(), "image/png")},
    )
    assert response.status_code == status
    assert_original_shop(editor)
    assert list(editor.original_photo.file_path.parent.glob("*.webp")) == [editor.original_photo.file_path]
    if failure in {"auth", "csrf"}:
        assert response.headers["x-csrf-token"] == "photo-token"
        assert "photo-token" not in response.text
        payload["csrf_token"] = response.headers["x-csrf-token"]
        retry = editor.client.post(
            f"/shop/{editor.shop_id}/edit", data=payload, headers=XHR_HEADERS,
            files={"photo": ("new.png", photo_bytes(), "image/png")},
        )
        assert retry.status_code == 200


def test_expired_session_can_log_in_and_retry_the_same_photo(
    editor: PhotoEditor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="photo-session-test")
    app.include_router(home.router)
    app.include_router(admin.router, prefix="/admin")

    def get_db() -> Generator[Session, None, None]:
        with editor.sessions() as db:
            yield db

    app.dependency_overrides[home.get_db] = get_db
    payload = edit_payload()
    image = photo_bytes()
    app.dependency_overrides[admin.get_db] = get_db

    @app.get("/_test/discord-admin")
    def discord_admin_session(request: Request) -> dict[str, bool]:
        with editor.sessions() as db:
            issue_admin_session(db, request.session, method="discord", credential="123456789012345678", discord_user_id="123456789012345678")
        return {"ok": True}
    with TestClient(app, client=("192.0.2.1", 50000), follow_redirects=False) as client:
        expired = client.post(
            f"/shop/{editor.shop_id}/edit", data=payload, headers=XHR_HEADERS,
            files={"photo": ("new.png", image, "image/png")},
        )
        assert expired.status_code == 401
        current_token = expired.headers["x-csrf-token"]
        assert current_token != payload["csrf_token"]
        assert client.get("/_test/discord-admin").status_code == 200
        stale = client.post(
            f"/shop/{editor.shop_id}/edit", data=payload, headers=XHR_HEADERS,
            files={"photo": ("new.png", image, "image/png")},
        )
        assert stale.status_code == 403
        assert stale.headers["x-csrf-token"] != current_token
        assert_original_shop(editor)
        payload["csrf_token"] = stale.headers["x-csrf-token"]
        retried = client.post(
            f"/shop/{editor.shop_id}/edit", data=payload, headers=XHR_HEADERS,
            files={"photo": ("new.png", image, "image/png")},
        )
        assert retried.status_code == 200
