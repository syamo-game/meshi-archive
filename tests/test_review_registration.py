from __future__ import annotations

import io
import json
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
from httpx import Response
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import Base, Message, ReviewEvent, Shop, ShopMention, SourceAsset
from services.review_service import EditAndApproveDecision, EditableShop, PreparedReviewPhoto, ReviewConflictError, apply_review_decision
from services.shop_image_cache import processed_image_path
from services.shop_image_upload import save_uploaded_image, uploaded_image_path
from web.routers import review, shop_images


@dataclass(frozen=True)
class RegistrationFixture:
    db: Session
    client: TestClient
    mention: ShopMention
    asset: SourceAsset
    other_asset: SourceAsset
    cache: Path


def image_bytes(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1200, 800), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[RegistrationFixture, None, None]:
    cache = tmp_path / "photos"
    cache.mkdir()
    monkeypatch.setenv("SHOP_IMAGE_CACHE_DIR", str(cache))
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    old_photo = save_uploaded_image(image_bytes("#885544"))
    mention = ShopMention(
        message=Message(message_id="72345678901234567", content="寿司の写真を添付した元投稿"),
        shop=Shop(shop_name="寿司の店", area="銀座", category="寿司", image_key=old_photo.image_key, memo="保持するメモ"),
        occurrence_index=0, extracted_name="寿司の店", extracted_area="銀座", extracted_category="寿司",
        review_status="pending", metadata_review_status="pending", extraction_source="test",
    )
    asset = SourceAsset(message=mention.message, kind="image", url="https://cdn.discordapp.com/attachments/1/2/test.png", fetch_status="available")
    other = SourceAsset(message=Message(message_id="72345678901234568", content="別の元投稿"), kind="image", url="https://cdn.discordapp.com/attachments/1/3/other.png", fetch_status="available")
    db.add_all([mention, asset, other])
    db.commit()
    # Exercise the real cache, image decoding, filesystem writes, and transaction.
    with Image.open(io.BytesIO(image_bytes("#557755"))) as image:
        image.resize((960, 540)).save(processed_image_path(asset.url), format="WEBP")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="synthetic-registration-test")

    def database() -> Generator[Session, None, None]:
        yield db

    @app.get("/test/session")
    def session(request: Request) -> dict[str, str]:
        request.session["admin_authenticated"] = True
        request.session["authenticated"] = True
        request.session["csrf_token"] = "test-csrf"
        return {"csrf_token": "test-csrf"}

    app.include_router(review.router)
    app.include_router(shop_images.router)
    app.dependency_overrides[review.get_db] = database
    app.dependency_overrides[shop_images.get_db] = database
    client = TestClient(app)
    client.get("/test/session")
    try:
        yield RegistrationFixture(db, client, mention, asset, other, cache)
    finally:
        client.close()
        db.close()
        engine.dispose()


def decision(fixture: RegistrationFixture, *, photo: int | None = None, category: str = "寿司") -> EditAndApproveDecision:
    return EditAndApproveDecision(
        action="edit_and_approve", expected_version=fixture.mention.version, shop_version=fixture.mention.shop.version,
        photo_asset_id=photo, confirm_metadata=True,
        shop=EditableShop(shop_name="確認済みの寿司店", branch_name="本店", area="銀座", category=category),
        supplement="駅前の本店", suggestion_evidence_urls=["https://example.com/shop"],
    )


def register(fixture: RegistrationFixture, body: EditAndApproveDecision) -> Response:
    return fixture.client.post(f"/api/admin/reviews/{fixture.mention.id}/decision", headers={"X-CSRF-Token": "test-csrf"}, json=body.model_dump(mode="json"))


def test_registration_persists_photo_and_both_reviews_with_audit(registration: RegistrationFixture) -> None:
    old_key = registration.mention.shop.image_key
    old_asset_url = registration.asset.url
    result = register(registration, decision(registration, photo=registration.asset.id))
    assert result.status_code == 200, result.text
    registration.db.expire_all()
    mention = registration.mention
    assert mention.review_status == mention.metadata_review_status == "approved"
    assert mention.shop.shop_name == "確認済みの寿司店" and mention.shop.branch_name == "本店"
    assert mention.shop.memo == "保持するメモ"
    assert mention.shop.image_key != old_key
    with Image.open(uploaded_image_path(mention.shop.image_key)) as image:
        assert image.size == (960, 540) and image.format == "WEBP"
    events = registration.db.query(ReviewEvent).order_by(ReviewEvent.id).all()
    assert {event.scope for event in events} == {"identity", "metadata"}
    audit = json.loads(next(event.note for event in events if event.scope == "identity"))
    assert audit["before"]["image_key"] == old_key
    assert audit["after"]["image_key"] == mention.shop.image_key
    assert audit["photo_asset_id"] == registration.asset.id
    assert audit["supplement"] == "駅前の本店"
    assert registration.asset.url == old_asset_url and registration.db.query(Message).count() == 2
    public_photo = registration.client.get(f"/media/shop-uploads/{mention.shop.image_key}.webp")
    assert public_photo.status_code == 200 and public_photo.headers["content-type"] == "image/webp"
    queue = registration.client.get("/api/admin/reviews?scope=all&status=unresolved").json()
    assert queue["total_count"] == queue["counters"]["unresolved"] == 0


def test_admin_photo_preview_does_not_register(registration: RegistrationFixture) -> None:
    old_key = registration.mention.shop.image_key
    result = registration.client.get(f"/api/admin/evidence/photo/{registration.asset.id}")
    assert result.status_code == 200 and result.headers["cache-control"] == "private, no-store"
    assert registration.mention.shop.image_key == old_key
    assert registration.db.query(ReviewEvent).count() == 0


@pytest.mark.parametrize("invalid", ["foreign", "missing", "unavailable", "kind", "unsafe"])
def test_invalid_photo_never_changes_registration(registration: RegistrationFixture, invalid: str) -> None:
    asset_id = registration.asset.id
    if invalid == "foreign":
        asset_id = registration.other_asset.id
    elif invalid == "missing":
        asset_id = 999999
    elif invalid == "unavailable":
        registration.asset.fetch_status = "unavailable"
    elif invalid == "kind":
        registration.asset.kind = "link"
    elif invalid == "unsafe":
        registration.asset.url = "http://127.0.0.1/private"
    registration.db.commit()
    before = (registration.mention.shop.shop_name, registration.mention.shop.image_key, registration.mention.version)
    result = register(registration, decision(registration, photo=asset_id))
    assert result.status_code == 400, result.text
    registration.db.expire_all()
    assert (registration.mention.shop.shop_name, registration.mention.shop.image_key, registration.mention.version) == before
    assert registration.mention.review_status == "pending" and registration.db.query(ReviewEvent).count() == 0


@pytest.mark.parametrize("failure", ["storage", "corrupt", "metadata"])
def test_photo_or_metadata_failure_rolls_back_every_field(registration: RegistrationFixture, failure: str) -> None:
    old_key = registration.mention.shop.image_key
    if failure == "storage":
        for path in (registration.cache / "uploads").iterdir():
            path.unlink()
        (registration.cache / "uploads").rmdir()
        (registration.cache / "uploads").write_text("blocked directory", encoding="utf-8")
    elif failure == "corrupt":
        processed_image_path(registration.asset.url).write_bytes(b"not an image")
    if failure == "metadata":
        body = decision(registration, photo=registration.asset.id).model_dump(mode="json")
        body["shop"]["category"] = ""
        result = registration.client.post(f"/api/admin/reviews/{registration.mention.id}/decision", headers={"X-CSRF-Token": "test-csrf"}, json=body)
    else:
        result = register(registration, decision(registration, photo=registration.asset.id))
    assert result.status_code == {"storage": 503, "corrupt": 400, "metadata": 422}[failure], result.text
    registration.db.expire_all()
    assert registration.mention.shop.shop_name == "寿司の店" and registration.mention.shop.image_key == old_key
    assert registration.mention.review_status == registration.mention.metadata_review_status == "pending"
    assert registration.mention.version == registration.mention.shop.version == 1
    assert registration.db.query(ReviewEvent).count() == 0


def test_stale_photo_decision_and_asset_change_cannot_overwrite(registration: RegistrationFixture) -> None:
    stale = decision(registration, photo=registration.asset.id)
    registration.mention.shop.version += 1
    registration.db.commit()
    result = register(registration, stale)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "stale_shop"
    body = decision(registration, photo=registration.asset.id)
    prepared = PreparedReviewPhoto(registration.asset.id, registration.mention.message_id, registration.asset.url, "f" * 64)
    registration.asset.url += "?updated=1"
    registration.db.commit()
    with pytest.raises(ReviewConflictError):
        apply_review_decision(registration.db, registration.mention.id, body, photo=prepared)
    registration.db.rollback()
    assert registration.mention.shop.shop_name == "寿司の店" and registration.db.query(ReviewEvent).count() == 0


def test_keep_existing_photo_and_exclude_preserve_source_and_history(registration: RegistrationFixture) -> None:
    old_key = registration.mention.shop.image_key
    assert register(registration, decision(registration)).status_code == 200
    assert registration.mention.shop.image_key == old_key
    body = {"action": "exclude", "expected_version": registration.mention.version, "shop_version": registration.mention.shop.version}
    result = registration.client.post(f"/api/admin/reviews/{registration.mention.id}/decision", headers={"X-CSRF-Token": "test-csrf"}, json=body)
    assert result.status_code == 200, result.text
    assert registration.mention.shop.image_key == old_key and registration.mention.message.content
    assert registration.db.query(SourceAsset).count() == 2 and registration.db.query(Shop).count() == 1
    assert registration.db.query(ReviewEvent).count() == 3
    assert registration.client.get("/api/admin/reviews?scope=all&status=unresolved").json()["total_count"] == 0


def test_unresolved_count_includes_deferred_once(registration: RegistrationFixture) -> None:
    registration.mention.review_status = registration.mention.metadata_review_status = "deferred"
    registration.db.commit()
    result = registration.client.get("/api/admin/reviews?scope=all&status=unresolved").json()
    assert result["total_count"] == result["counters"]["unresolved"] == 1
    assert len(result["items"]) == 1


def test_photo_auth_csrf_and_read_only(registration: RegistrationFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"/api/admin/evidence/photo/{registration.asset.id}"
    registration.client.cookies.clear()
    assert registration.client.get(url).status_code == 403
    registration.client.get("/test/session")
    body = decision(registration, photo=registration.asset.id)
    assert registration.client.post(f"/api/admin/reviews/{registration.mention.id}/decision", json=body.model_dump(mode="json")).status_code == 403
    monkeypatch.setenv("APP_READ_ONLY", "true")
    assert register(registration, body).status_code == 503
    assert registration.mention.review_status == "pending" and registration.db.query(ReviewEvent).count() == 0
