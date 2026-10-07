from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from db.models import Base, Message, ReviewEvent, Shop, ShopMention, SourceAsset
from services.review_audit import export_review_audit
from web.routers import home, review


@pytest.fixture
def audit_engine() -> Iterator[Engine]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        messages = [
            Message(
                message_id=str(12345678901234567 + index), content=f"Source body {index}",
                source_created_at=datetime(2026, 1, index + 1, tzinfo=timezone.utc),
                processing_status="failed" if index >= 2 else "succeeded",
                fetch_error="Source incomplete" if index == 2 else None,
            )
            for index in range(4)
        ]
        alpha = Shop(id=10, shop_name="Alpha", area="神田", category="カフェ")
        beta = Shop(id=20, shop_name="Beta", area="銀座", category="中華")
        db.add_all(messages)
        db.add_all([
            ShopMention(
                id=11, message=messages[0], occurrence_index=0, shop=alpha,
                extracted_name="Alpha", review_status="approved", metadata_review_status="pending",
                metadata_difference_type="unknown_category", extracted_category="Fine original category",
            ),
            ShopMention(
                id=12, message=messages[1], occurrence_index=0, shop=alpha,
                extracted_name="Alpha", review_status="approved", metadata_review_status="approved",
            ),
            ShopMention(
                id=13, message=messages[0], occurrence_index=1, shop=beta,
                extracted_name="Beta", review_status="pending", metadata_review_status="deferred",
            ),
            ShopMention(
                id=14, message=messages[2], occurrence_index=0,
                extracted_name="Unlinked", review_status="deferred", metadata_review_status="pending",
                difference_type="source_incomplete", extraction_error="Need full text",
            ),
        ])
        db.add(SourceAsset(
            message=messages[0], kind="attachment",
            url="https://cdn.discordapp.com/attachments/file.png?signature=private-signature",
        ))
        db.add(ReviewEvent(mention_id=14, action="defer", note="History is retained"))
        db.commit()
    try:
        yield engine
    finally:
        engine.dispose()


def audit_app(engine: Engine) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def add_session(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.scope["session"] = {"admin_authenticated": request.headers.get("x-test-admin") == "yes"}
        return await call_next(request)

    def get_db() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    app.include_router(review.router)
    app.include_router(home.router)
    app.dependency_overrides[review.get_db] = get_db
    app.dependency_overrides[home.get_db] = get_db
    return app


def rows(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))


def test_event_exclusion_is_auditable_without_counting_it_as_pending_work(
    audit_engine: Engine,
) -> None:
    with Session(audit_engine) as db:
        message = Message(message_id="22345678901234567", content="浦和の物産展に合成海鮮食堂が出店。出店元支店は未特定。")
        excluded = ShopMention(
            id=15, message=message, occurrence_index=0, extracted_name="合成海鮮食堂",
            extracted_branch_name="浦和催事会場", review_status="rejected",
            metadata_review_status="deferred", resolution_status="invalid",
            difference_type="event_excluded", extraction_error="出店元の常設店舗を特定できません。",
        )
        db.add_all([
            excluded,
            ShopMention(
                id=16, message=message, occurrence_index=1, extracted_name="別の未確認対象",
                review_status="rejected", metadata_review_status="deferred", difference_type=None,
            ),
            ShopMention(
                id=17, message=message, occurrence_index=2, extracted_name="Alpha",
                shop_id=10, review_status="approved", metadata_review_status="pending",
                difference_type="event_excluded",
            ),
        ])
        db.add(SourceAsset(message=message, kind="attachment", url="https://example.invalid/event.png"))
        db.commit()
        before = {
            name: [tuple(row) for row in db.execute(table.select()).all()]
            for name, table in Base.metadata.tables.items()
        }
        exported = {row["mention_id"]: row for row in rows(export_review_audit(db).content)}
        assert exported["15"]["needs_review"] == "False"
        assert exported["15"]["review_status"] == "rejected"
        assert exported["15"]["difference_type"] == "event_excluded"
        assert exported["15"]["shop.id"] == ""
        assert exported["15"]["message.content"] == message.content
        assert exported["15"]["message.asset_count"] == "1"
        assert exported["15"]["extraction_error"] == excluded.extraction_error
        assert exported["16"]["needs_review"] == "True"
        assert exported["17"]["needs_review"] == "True"
        after = {
            name: [tuple(row) for row in db.execute(table.select()).all()]
            for name, table in Base.metadata.tables.items()
        }
        assert after == before


def test_audit_keeps_unlinked_and_multiple_mentions_with_stable_identifiers_and_reasons(
    audit_engine: Engine,
) -> None:
    with Session(audit_engine) as db:
        before = {
            name: [tuple(row) for row in db.execute(table.select()).all()]
            for name, table in Base.metadata.tables.items()
        }
        audit = export_review_audit(db)
        exported = rows(audit.content)
        assert [row["mention_id"] for row in exported] == ["11", "12", "13", "14"]
        assert (audit.row_count, audit.message_count, audit.unlinked_count) == (4, 3, 1)
        assert exported[0]["message_id"] == exported[2]["message_id"]
        assert [exported[index]["occurrence_index"] for index in (0, 2)] == ["0", "1"]
        assert exported[3]["shop.id"] == ""
        assert exported[3]["difference_type"] == "source_incomplete"
        assert exported[3]["extraction_error"] == "Need full text"
        assert exported[3]["message.fetch_error"] == "Source incomplete"
        assert exported[3]["mention.review_event_count"] == "1"
        assert exported[0]["message.asset_count"] == exported[2]["message.asset_count"] == "1"
        assert exported[0]["extracted_category"] == "Fine original category"
        assert "private-signature" not in audit.content.decode("utf-8-sig")
        assert "History is retained" not in audit.content.decode("utf-8-sig")
        assert str(12345678901234570) not in {row["message_id"] for row in exported}
        assert all(None not in row for row in exported)
        assert before == {
            name: [tuple(row) for row in db.execute(table.select()).all()]
            for name, table in Base.metadata.tables.items()
        }


def test_queue_filters_counters_and_csv_units_can_be_reconciled(audit_engine: Engine) -> None:
    with TestClient(audit_app(audit_engine)) as client:
        client.headers["x-test-admin"] = "yes"
        filtered = client.get("/api/admin/reviews?scope=identity&status=approved&q=Alpha&limit=1").json()
        assert filtered["total_count"] == 2 and len(filtered["items"]) == 1
        assert filtered["counters"] == {
            "pending": 1, "approved": 2, "deferred": 1, "rejected": 0,
            "source_unavailable": 1, "failed": 2, "unresolved": 3,
        }
        metadata = client.get("/api/admin/reviews?scope=metadata&status=pending&q=NoMatch").json()
        assert metadata["total_count"] == 0 and metadata["items"] == []
        assert metadata["counters"]["pending"] == 2
        assert metadata["counters"]["approved"] == 1
        assert metadata["counters"]["failed"] == 2
        audit = client.get("/admin/review/audit.csv")
        assert audit.status_code == 200
        exported = rows(audit.content)
        assert len(exported) == 4
        assert sum(row["metadata_review_status"] == "pending" for row in exported) == 2
        assert sum(bool(row["message.fetch_error"]) for row in exported) == 1
        public_ids = {row["shop.id"] for row in exported if row["shop.is_public"] == "True"}
        assert public_ids == {"10"}
        store_rows = rows(client.get("/export.csv").content)
        assert len(store_rows) == 2
        assert sum(
            row["review_status"] == row["metadata_review_status"] == "approved"
            for row in store_rows
        ) == 0
        representatives = {row["mention_id"] for row in exported if row["is_representative_mention"] == "True"}
        assert representatives == {"11", "13"}
    with Session(audit_engine) as db:
        assert {shop.id for shop in db.query(Shop).filter(home._has_public_mention()).all()} == {10}


def test_audit_requires_admin_and_is_read_only_uncached_with_verifiable_identity(
    audit_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "true")
    with TestClient(audit_app(audit_engine)) as client:
        denied = client.get("/admin/review/audit.csv")
        assert denied.status_code == 403
        assert "Source body" not in denied.text
        client.headers["x-test-admin"] = "yes"
        first = client.get("/admin/review/audit.csv")
        second = client.get("/admin/review/audit.csv")
    assert first.status_code == 200
    assert first.content == second.content
    assert first.headers["content-disposition"] != second.headers["content-disposition"]
    assert first.content.startswith(b"\xef\xbb\xbf")
    assert "no-store" in first.headers["cache-control"]
    assert first.headers["x-export-content-sha256"] == hashlib.sha256(first.content).hexdigest()
    assert first.headers["x-export-row-count"] == "4"
    assert first.headers["x-export-message-count"] == "3"
    assert first.headers["x-export-unlinked-count"] == "1"
    assert first.headers["x-export-row-unit"] == "mention"
    assert first.headers["x-export-scope"] == "all-mentions"


@pytest.mark.parametrize("value", ["=1+1", "+1+1", "-1+1", "@SUM(1)", " \t=1+1", "\ttext", "\rtext", "\ntext"])
def test_audit_neutralizes_formula_cells_without_losing_original_text(audit_engine: Engine, value: str) -> None:
    with Session(audit_engine) as db:
        mention = db.get(ShopMention, 14)
        assert mention is not None
        mention.extracted_name = value
        mention.message.content = value
        mention.extraction_error = value
        db.commit()
        exported = rows(export_review_audit(db).content)[3]
        assert exported["extracted_name"] == "'" + value
        assert exported["message.content"] == "'" + value
        assert exported["extraction_error"] == "'" + value


@pytest.mark.parametrize("source", ["asset", "embedded_attachment", "signed_url"])
def test_attachment_queries_are_redacted_in_all_exported_text_but_retained_in_database(
    audit_engine: Engine, source: str,
) -> None:
    base = "https://files.example.com/attachment.png"
    query = "signature=private-audit-token" if source == "signed_url" else "ticket=private-audit-token"
    attachment_url = base + "?" + query
    normal_url = "https://example.com/article?id=42&lang=ja"
    with Session(audit_engine) as db:
        mention = db.get(ShopMention, 14)
        assert mention is not None
        content = f"Original text {normal_url}\n"
        if source == "embedded_attachment":
            content += f"[Attachment] filename=photo.png content_type=image/png url={attachment_url}"
        else:
            content += f"Image ({attachment_url})."
        if source == "asset":
            db.add(SourceAsset(message=mention.message, kind="attachment", url=attachment_url))
        mention.message.content = content
        mention.source_url = attachment_url
        mention.message.fetch_error = f"Fetch failed: url={attachment_url}"
        mention.extraction_error = f"Unavailable {attachment_url}"
        mention.confidence_reason = f"Evidence {attachment_url}"
        db.commit()
        audit = export_review_audit(db)
        exported = rows(audit.content)[3]
        assert "private-audit-token" not in audit.content.decode("utf-8-sig")
        for name in ("message.content", "source_url", "message.fetch_error", "extraction_error", "confidence_reason"):
            assert base + "?__redacted__" in exported[name]
        assert normal_url in exported["message.content"]
        db.expire_all()
        assert mention.message.content == content
        assert mention.source_url == attachment_url
        assert "private-audit-token" in mention.message.fetch_error
        assert "private-audit-token" in mention.confidence_reason
