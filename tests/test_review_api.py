from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import (
    Base,
    Message,
    MetadataReviewStatus,
    ReviewEvent,
    ReviewScope,
    ReviewStatus,
    Shop,
    ShopMention,
)
from web.routers import review


def create_test_app(db: Session) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    def override_db() -> Generator[Session, None, None]:
        yield db

    @app.get("/test/session")
    def set_session(request: Request) -> dict[str, str]:
        request.session["admin_authenticated"] = True
        request.session["csrf_token"] = "test-csrf"
        return {"csrf_token": "test-csrf"}

    app.include_router(review.router)
    app.dependency_overrides[review.get_db] = override_db
    return app


def test_review_api_requires_admin_csrf_and_rejects_double_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    message = Message(message_id="12345678901234567", content="投稿本文")
    shop = Shop(shop_name="割烹みやび", area="銀座", category="割烹")
    mention = ShopMention(
        message=message,
        shop=shop,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    db.add(mention)
    db.commit()

    try:
        client = TestClient(create_test_app(db))
        assert client.get("/api/admin/reviews").status_code == 403
        client.get("/test/session")
        queue = client.get("/api/admin/reviews")
        assert queue.status_code == 200
        assert queue.json()["scope"] == ReviewScope.IDENTITY.value
        assert queue.json()["total_count"] == 1
        assert queue.json()["counters"]["pending"] == 1

        payload = {
            "action": "approve_current",
            "expected_version": 1,
            "shop_version": 1,
        }
        assert client.post(
            f"/api/admin/reviews/{mention.id}/decision", json=payload
        ).status_code == 403
        response = client.post(
            f"/api/admin/reviews/{mention.id}/decision",
            json=payload,
            headers={"X-CSRF-Token": "test-csrf"},
        )
        assert response.status_code == 200
        assert response.json()["scope"] == ReviewScope.IDENTITY.value
        assert response.json()["review_status"] == "approved"

        duplicate = client.post(
            f"/api/admin/reviews/{mention.id}/decision",
            json=payload,
            headers={"X-CSRF-Token": "test-csrf"},
        )
        assert duplicate.status_code == 409
    finally:
        db.close()
        engine.dispose()


def test_review_api_reject_without_reason_keeps_guards_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    mention = ShopMention(
        message=Message(message_id="92345678901234567", content="店舗ではない投稿"),
        shop=Shop(shop_name="確認対象", area="銀座", category="その他"),
        occurrence_index=0,
        extracted_name="確認対象",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    db.add(mention)
    db.commit()
    shop_id = mention.shop_id
    endpoint = f"/api/admin/reviews/{mention.id}/decision"
    payload: dict[str, str | int] = {
        "action": "reject",
        "expected_version": 1,
        "shop_version": 1,
    }
    headers = {"X-CSRF-Token": "test-csrf"}

    try:
        client = TestClient(create_test_app(db))
        assert client.post(endpoint, json=payload, headers=headers).status_code == 403
        client.get("/test/session")
        assert client.post(endpoint, json=payload).status_code == 403
        assert client.post(
            endpoint, json={**payload, "shop_version": 2}, headers=headers
        ).status_code == 409
        db.refresh(mention)
        assert mention.review_status == ReviewStatus.PENDING.value
        assert mention.shop_id == shop_id
        assert mention.version == 1
        assert db.query(ReviewEvent).count() == 0

        response = client.post(endpoint, json=payload, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["review_status"] == ReviewStatus.REJECTED.value
        assert body["shop_id"] is None
        assert body["version"] == 2
        assert db.get(Shop, shop_id) is None
        audit = db.query(ReviewEvent).filter_by(mention_id=mention.id).one()
        assert audit.action == "reject"
        assert audit.previous_shop_id == shop_id
        assert audit.selected_shop_id is None
        assert audit.note is None
        assert client.post(endpoint, json=payload, headers=headers).status_code == 409
        assert db.query(ReviewEvent).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_review_api_groups_related_mentions_and_reports_automatic_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    db = factory()
    source_message = Message(
        message_id="42345678901234567",
        content="最初の店舗投稿",
    )
    shop = Shop(shop_name="割烹みやび", area="銀座", category="割烹")
    source = ShopMention(
        message=source_message,
        shop=shop,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    related_message = Message(
        message_id="42345678901234568",
        content="同じ店舗の別投稿",
    )
    related = ShopMention(
        message=related_message,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    db.add_all((source, related))
    db.commit()

    try:
        client = TestClient(create_test_app(db))
        client.get("/test/session")
        queue = client.get("/api/admin/reviews")

        assert queue.status_code == 200
        items = queue.json()["items"]
        assert len(items) == 2
        assert items[0]["review_group_count"] == 2
        assert items[0]["review_group_mention_ids"] == [source.id, related.id]
        assert items[0]["review_group_reason"] == "抽出した店名・支店名・エリアが一致"

        response = client.post(
            f"/api/admin/reviews/{source.id}/decision",
            json={
                "action": "approve_current",
                "expected_version": 1,
                "shop_version": 1,
            },
            headers={"X-CSRF-Token": "test-csrf"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["automatically_resolved_count"] == 1
        assert body["automatically_resolved_mention_ids"] == [related.id]
        db.refresh(related)
        assert related.review_status == ReviewStatus.APPROVED.value
        assert related.shop_id == shop.id
    finally:
        db.close()
        engine.dispose()


def test_review_api_separates_metadata_queue_and_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    message = Message(
        message_id="22345678901234567",
        content="A restaurant recommendation",
    )
    shop = Shop(shop_name="Current Shop", area="銀座", category="Unknown")
    mention = ShopMention(
        message=message,
        shop=shop,
        occurrence_index=0,
        extracted_name="Current Shop",
        extracted_area="銀座",
        extracted_category="Unknown",
        resolution_status="resolved",
        review_status="approved",
        metadata_review_status="pending",
        metadata_difference_type="unknown_category",
        extraction_source="test",
    )
    db.add(mention)
    db.commit()

    try:
        client = TestClient(create_test_app(db))
        client.get("/test/session")

        invalid_scope = client.get("/api/admin/reviews?scope=other")
        assert invalid_scope.status_code == 400

        queue = client.get(
            "/api/admin/reviews?scope=metadata&status=pending&reason=unknown_category"
        )
        assert queue.status_code == 200
        queue_body = queue.json()
        assert queue_body["scope"] == ReviewScope.METADATA.value
        assert queue_body["total_count"] == 1
        assert queue_body["counters"]["pending"] == 1
        assert queue_body["counters"]["rejected"] == 0
        assert [item["id"] for item in queue_body["items"]] == [mention.id]
        assert queue_body["items"][0]["metadata_review_status"] == "pending"

        forbidden = client.post(
            f"/api/admin/reviews/{mention.id}/decision",
            json={
                "action": "reject",
                "scope": "metadata",
                "expected_version": 1,
                "shop_version": 1,
            },
            headers={"X-CSRF-Token": "test-csrf"},
        )
        assert forbidden.status_code == 400

        approved = client.post(
            f"/api/admin/reviews/{mention.id}/decision",
            json={
                "action": "approve_current",
                "scope": "metadata",
                "expected_version": 1,
                "shop_version": 1,
            },
            headers={"X-CSRF-Token": "test-csrf"},
        )
        assert approved.status_code == 200
        approved_body = approved.json()
        assert approved_body["scope"] == ReviewScope.METADATA.value
        assert approved_body["review_status"] == "approved"
        assert (
            approved_body["metadata_review_status"]
            == MetadataReviewStatus.APPROVED.value
        )
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("scope", "status"),
    [
        (ReviewScope.IDENTITY, "pending"),
        (ReviewScope.IDENTITY, "approved"),
        (ReviewScope.IDENTITY, "deferred"),
        (ReviewScope.IDENTITY, "rejected"),
        (ReviewScope.METADATA, "pending"),
        (ReviewScope.METADATA, "approved"),
        (ReviewScope.METADATA, "deferred"),
    ],
)
def test_review_queue_counts_matching_mentions_before_pagination(
    scope: ReviewScope,
    status: str,
) -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    unavailable = Message(
        message_id="52345678901234567",
        content="同じ投稿内の複数候補",
        fetch_error="Source fetch failed",
        processing_status="failed",
    )
    available = Message(
        message_id="52345678901234568",
        content="別の投稿",
        processing_status="succeeded",
    )
    failed_without_mentions = Message(
        message_id="52345678901234569",
        processing_status="failed",
    )
    matching_shop = Shop(shop_name="対象店 本店", area="銀座", category="和食")
    reason = "legacy_review" if scope == ReviewScope.IDENTITY else "missing_area"
    other_status = "pending" if status != "pending" else "approved"
    rows: tuple[tuple[str, Shop | None, str, str, str, Message], ...] = (
        ("抽出時の別名", matching_shop, status, reason, "legacy_import", unavailable),
        ("対象店 二号店", None, status, reason, "legacy_import", unavailable),
        ("対象店 状態違い", None, other_status, reason, "legacy_import", unavailable),
        ("対象店 理由違い", None, status, "other_reason", "legacy_import", available),
        ("対象店 登録方法違い", None, status, reason, "responses_structured", available),
        ("無関係の店", None, status, reason, "legacy_import", available),
    )
    mentions: list[ShopMention] = []
    for index, (name, shop, item_status, item_reason, source, message) in enumerate(rows):
        mention = ShopMention(
            message=message,
            shop=shop,
            occurrence_index=index,
            extracted_name=name,
            review_status=(item_status if scope == ReviewScope.IDENTITY else other_status),
            metadata_review_status=(
                item_status if scope == ReviewScope.METADATA else other_status
            ),
            difference_type=(item_reason if scope == ReviewScope.IDENTITY else "legacy_review"),
            metadata_difference_type=(
                item_reason if scope == ReviewScope.METADATA else "missing_area"
            ),
            extraction_source=source,
        )
        mentions.append(mention)
    db.add_all([*mentions, failed_without_mentions])
    db.commit()

    try:
        with TestClient(create_test_app(db)) as client:
            client.get("/test/session")

            def get_queue(params: dict[str, str | int]) -> review.ReviewQueueResponse:
                response = client.get("/api/admin/reviews", params=params)
                assert response.status_code == 200
                return review.ReviewQueueResponse.model_validate(response.json())

            filters: dict[str, str | int] = {"scope": scope.value, "status": status}
            all_in_status = get_queue(filters)
            assert all_in_status.total_count == 5
            assert len(all_in_status.items) == 5
            assert getattr(all_in_status.counters, status) == 5
            assert all_in_status.counters.source_unavailable == 3
            assert all_in_status.counters.failed == 2

            filters["reason"] = reason
            assert get_queue(filters).total_count == 4
            filters["source"] = "legacy_import"
            assert get_queue(filters).total_count == 3
            filters["q"] = " 対象店 "
            matching = get_queue(filters)
            assert matching.total_count == 2
            assert [item.id for item in matching.items] == [mentions[0].id, mentions[1].id]
            assert matching.counters == all_in_status.counters

            filters["limit"] = 1
            first_page = get_queue(filters)
            assert first_page.total_count == 2
            assert [item.id for item in first_page.items] == [mentions[0].id]
            assert first_page.next_cursor == mentions[0].id

            filters["cursor"] = mentions[0].id
            last_page = get_queue(filters)
            assert last_page.total_count == 2
            assert [item.id for item in last_page.items] == [mentions[1].id]
            assert last_page.next_cursor is None

            filters["cursor"] = mentions[1].id
            exhausted = get_queue(filters)
            assert exhausted.total_count == 2
            assert exhausted.items == []
            assert exhausted.next_cursor is None
            assert exhausted.counters == all_in_status.counters

            unavailable_queue = get_queue(
                {**filters, "reason": "source_unavailable", "cursor": mentions[0].id}
            )
            assert unavailable_queue.total_count == 2
            assert [item.id for item in unavailable_queue.items] == [mentions[1].id]
            assert unavailable_queue.counters == all_in_status.counters

            empty = get_queue({**filters, "q": "存在しない店"})
            assert empty.total_count == 0
            assert empty.items == []
            assert empty.next_cursor is None
            assert empty.counters == all_in_status.counters
    finally:
        db.close()
        engine.dispose()


def test_review_api_rejects_canonical_url_userinfo_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    message = Message(message_id="32345678901234567", content="投稿本文")
    mention = ShopMention(
        message=message,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    db.add(mention)
    db.commit()

    try:
        client = TestClient(create_test_app(db))
        client.get("/test/session")
        secret = "do-not-echo"
        response = client.post(
            f"/api/admin/reviews/{mention.id}/decision",
            json={
                "action": "edit_and_approve",
                "expected_version": 1,
                "shop": {
                    "shop_name": "割烹みやび",
                    "area": "銀座",
                    "category": "割烹",
                    "canonical_url": f"https://user:{secret}@example.com/shop",
                },
            },
            headers={"X-CSRF-Token": "test-csrf"},
        )

        assert response.status_code == 400
        assert secret not in response.text
        assert db.query(Shop).count() == 0
        assert mention.review_status == "pending"
    finally:
        db.close()
        engine.dispose()
