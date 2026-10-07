from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Generator
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import Base, Message, Shop, ShopMention
from web.routers import home, review


@pytest.fixture
def export_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="csv-test-secret")

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    @app.get("/test/session")
    def authenticate(request: Request) -> dict[str, str]:
        request.session["admin_authenticated"] = True
        request.session["csrf_token"] = "test-csrf"
        return {"csrf_token": "test-csrf"}

    app.include_router(home.router)
    app.include_router(review.router)
    app.dependency_overrides[home.get_db] = override_db
    app.dependency_overrides[review.get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client, factory
    finally:
        engine.dispose()


def add_mention(
    db: Session,
    *,
    mention_id: int = 1,
    shop: Shop | None = None,
    review_status: str = "pending",
    metadata_status: str = "pending",
) -> ShopMention:
    if shop is None:
        shop = Shop(id=1, shop_name="Original shop", area="神田", category="割烹")
    mention = ShopMention(
        id=mention_id,
        message=Message(message_id=f"1234567890123456{mention_id}"),
        shop=shop,
        occurrence_index=0,
        extracted_name=shop.shop_name,
        extracted_area=shop.area,
        extracted_category=shop.category,
        source_url=f"https://example.com/posts/{mention_id}",
        review_status=review_status,
        resolution_status="resolved" if review_status == "approved" else "ambiguous",
        metadata_review_status=metadata_status,
        extraction_source="test",
    )
    db.add(mention)
    db.flush()
    return mention


def csv_rows(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))


def test_export_has_verifiable_download_identity_without_changing_unchanged_data(
    export_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = export_client
    with factory() as db:
        add_mention(db)
        db.commit()
    client.get("/test/session")
    before = datetime.now(timezone.utc)
    first = client.get("/export.csv")
    second = client.get("/export.csv")
    after = datetime.now(timezone.utc)

    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert "url" not in csv_rows(first.content)[0]
    assert "source_url" in csv_rows(first.content)[0]
    assert "canonical_url" in csv_rows(first.content)[0]
    assert first.headers["content-disposition"] != second.headers["content-disposition"]
    for response in (first, second):
        checksum = hashlib.sha256(response.content).hexdigest()
        assert response.headers["x-export-content-sha256"] == checksum
        assert checksum[:12] in response.headers["content-disposition"]
        exported_at = datetime.fromisoformat(response.headers["x-export-generated-at"])
        assert before <= exported_at <= after
        assert "no-store" in response.headers["cache-control"]
        assert "private" in response.headers["cache-control"]
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["expires"] == "0"
        assert response.headers["x-export-row-count"] == "1"
        assert response.headers["x-export-row-unit"] == "shop"
        assert response.headers["x-export-review-scope"] == "representative-mention"
        assert response.content.startswith(b"\xef\xbb\xbf")


def test_export_after_review_save_reads_committed_name_category_and_review_status(
    export_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = export_client
    with factory() as db:
        add_mention(db)
        db.commit()
    client.get("/test/session")
    before = client.get("/export.csv")
    saved = client.post(
        "/api/admin/reviews/1/decision",
        headers={"X-CSRF-Token": "test-csrf"},
        json={
            "action": "edit_and_approve",
            "scope": "identity",
            "expected_version": 1,
            "shop_version": 1,
            "shop": {"shop_name": "Corrected shop", "area": "神田", "category": "中華"},
        },
    )
    assert saved.status_code == 200, saved.text
    after = client.get("/export.csv")
    exported = csv_rows(after.content)[0]

    assert before.content != after.content
    assert exported["shop.name"] == "Corrected shop"
    assert exported["shop.category"] == "中華"
    assert exported["review_status"] == "approved"
    assert exported["shop.version"] == str(saved.json()["shop"]["version"])
    assert exported["mention_id"] == "1"
    assert exported["mention.version"] == str(saved.json()["version"])
    assert exported["shop.mention_versions"] == f"1:{saved.json()['version']}"
    assert after.headers["x-export-content-sha256"] == hashlib.sha256(after.content).hexdigest()


def test_secondary_mention_review_is_visible_without_replacing_representative_status(
    export_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = export_client
    with factory() as db:
        representative = add_mention(db, review_status="approved", metadata_status="approved")
        add_mention(
            db,
            mention_id=2,
            shop=representative.shop,
            review_status="approved",
            metadata_status="pending",
        )
        db.commit()
    client.get("/test/session")
    before = client.get("/export.csv")
    before_row = csv_rows(before.content)[0]
    saved = client.post(
        "/api/admin/reviews/2/decision",
        headers={"X-CSRF-Token": "test-csrf"},
        json={
            "action": "approve_current",
            "scope": "metadata",
            "expected_version": 1,
            "shop_version": 1,
        },
    )
    assert saved.status_code == 200, saved.text
    after = client.get("/export.csv")
    after_row = csv_rows(after.content)[0]

    assert before.content != after.content
    assert before_row["mention_id"] == after_row["mention_id"] == "1"
    assert before_row["metadata_review_status"] == after_row["metadata_review_status"] == "approved"
    assert before_row["needs_review"] == after_row["needs_review"] == "False"
    assert before_row["shop.mention_count"] == after_row["shop.mention_count"] == "2"
    assert before_row["shop.metadata_pending_count"] == "1"
    assert after_row["shop.metadata_pending_count"] == "0"
    assert before_row["shop.needs_review"] == "True"
    assert after_row["shop.needs_review"] == "False"
    assert before_row["shop.mention_versions"] == "1:1;2:1"
    assert after_row["shop.mention_versions"] == "1:1;2:2"


def test_export_counts_pending_and_deferred_separately_and_preserves_shop_filters(
    export_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = export_client
    with factory() as db:
        first = add_mention(db, review_status="approved", metadata_status="approved")
        add_mention(db, mention_id=2, shop=first.shop)
        add_mention(db, mention_id=3, shop=first.shop, review_status="deferred", metadata_status="deferred")
        add_mention(db, mention_id=4, shop=Shop(id=2, shop_name="Other shop", area="銀座", category="中華"))
        db.commit()
    client.get("/test/session")
    response = client.get("/export.csv?area=神田")
    rows = csv_rows(response.content)

    assert response.status_code == 200
    assert response.headers["x-export-row-count"] == "1"
    assert len(rows) == 1
    assert rows[0]["_id"] == "1"
    assert rows[0]["shop.mention_count"] == "3"
    assert rows[0]["shop.identity_pending_count"] == "1"
    assert rows[0]["shop.identity_deferred_count"] == "1"
    assert rows[0]["shop.metadata_pending_count"] == "1"
    assert rows[0]["shop.metadata_deferred_count"] == "1"


def test_export_requires_admin_authentication(
    export_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, _factory = export_client
    response = client.get("/export.csv", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"
    assert "x-export-content-sha256" not in response.headers
