from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Connection, create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import (
    Base, ImportBatch, ImportRow, LookupCache, Message, ProcessingRun,
    ResolutionCandidate, ReviewEvent, Shop, ShopMention, ShopRedirect,
    SourceAsset, SyncState,
)
from services.mention_linking import (
    LinkMentionRequest, link_mention, preview_mention_link,
)
from services.review_service import ReviewConflictError, ReviewNotFoundError
from web.routers import review


Row = tuple[tuple[str, object], ...]
DatabaseSnapshot = dict[str, tuple[Row, ...]]
_SOURCE = 1
_TARGET = 2
_SELECTED = 1
_OLD_TIME = datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc)
_MESSAGE_IDS = tuple(f"9234567890123456{index}" for index in range(1, 5))
_NOTE = "元投稿と常設支店の公式情報を確認した合成例"


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: sqlite3.Connection, _record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        shops = [
            Shop(
                id=index, shop_name=f"合成店舗{index}", branch_name=f"支店{index}", area="神田",
                category="カフェ・喫茶店", address=f"架空住所{index}", phone=f"031234560{index}",
                canonical_url=f"https://fixture.example/shop/{index}", image_key=str(index) * 64,
                external_source="fixture", external_id=f"synthetic-{index}",
                is_visited=True, visited_at=datetime(2025, index, index, 10, 15, tzinfo=timezone.utc),
                rating=index + 1, memo=f"保持する店舗別メモ{index}",
                created_at=_OLD_TIME, updated_at=_OLD_TIME, version={1: 7, 2: 11, 3: 5}[index],
            )
            for index in (1, 2, 3)
        ]
        messages = [
            Message(
                message_id=message_id, channel_id="82345678901234567",
                content=f"保持する合成原文{index}", source_created_at=_OLD_TIME,
                processing_status="succeeded", fetch_error="保持する合成取得メモ" if index == 0 else None,
            )
            for index, message_id in enumerate(_MESSAGE_IDS)
        ]
        session.add_all(shops + messages)
        mentions = [
            ShopMention(
                id=1, message=messages[0], shop=shops[0], occurrence_index=0,
                extracted_name="抽出された元の名称", extracted_branch_name="元の支店",
                extracted_area="元のエリア", extracted_category="元の細かな業態",
                source_url="https://fixture.example/post/selected", review_status="pending",
                metadata_review_status="deferred", metadata_difference_type="unknown_category",
                metadata_reviewed_at=_OLD_TIME, resolution_status="ambiguous",
                resolution_method="automatic", resolution_basis="source_reuse",
                difference_type="name_mismatch", extraction_source="synthetic",
                extraction_error="保持する抽出メモ", confidence_reason="保持する根拠",
                reviewed_at=_OLD_TIME, version=13,
            ),
            ShopMention(
                id=2, message=messages[0], shop=shops[0], occurrence_index=1,
                extracted_name="同じ投稿の別項目", source_url="https://fixture.example/post/sibling",
                review_status="pending", metadata_review_status="pending", version=3,
            ),
            ShopMention(
                id=3, message=messages[1], shop=shops[1], occurrence_index=0,
                extracted_name=shops[1].shop_name, extracted_branch_name=shops[1].branch_name,
                extracted_area="神田", extracted_category=shops[1].category,
                review_status="approved", metadata_review_status="approved",
                resolution_status="resolved", resolution_method="manual", version=4,
            ),
            ShopMention(
                id=4, message=messages[2], shop=shops[1], occurrence_index=0,
                extracted_name="移動先の別投稿", review_status="pending",
                metadata_review_status="pending", version=9,
            ),
            ShopMention(
                id=5, message=messages[3], occurrence_index=0,
                extracted_name=shops[1].shop_name, extracted_branch_name=shops[1].branch_name,
                extracted_area="神田", extracted_category=shops[1].category,
                review_status="pending", metadata_review_status="pending", version=6,
            ),
        ]
        session.add_all(mentions)
        session.flush()
        mentions[0].reused_from_mention_id = 3
        for index, message in enumerate(messages):
            session.add(SourceAsset(
                message=message, kind="attachment", url=f"https://fixture.example/evidence/{index}.pdf",
                title=f"保持する添付{index}", description="合成説明", extracted_text="合成添付原文",
                fetch_status="available",
            ))
        for mention in mentions:
            session.add(ReviewEvent(
                mention=mention, action="defer", previous_shop_id=mention.shop_id,
                selected_shop_id=mention.shop_id, note=f"保持する確認履歴{mention.id}", created_at=_OLD_TIME,
            ))
        session.add_all([
            ResolutionCandidate(
                mention_id=1, rank=1, name="保持する候補", canonical_url="https://fixture.example/candidate",
                evidence_url="https://fixture.example/candidate/evidence", verification_reason="保持する候補根拠",
            ),
            ResolutionCandidate(
                mention_id=5, rank=1, name=shops[1].shop_name, area="神田", category=shops[1].category,
                address=shops[1].address, phone=shops[1].phone, canonical_url=shops[1].canonical_url,
                external_source=shops[1].external_source, external_id=shops[1].external_id,
                is_verified=True, is_strong_match=True, provenance="structured_data",
                verification_reason="合成の強い一致根拠", name_similarity_milli=1000,
            ),
            ProcessingRun(message_id=_MESSAGE_IDS[0], stage="synthetic", status="succeeded", error="保持する処理記録"),
            LookupCache(cache_key="f" * 64, kind="synthetic", payload='{"synthetic":true}', expires_at=_OLD_TIME),
            SyncState(channel_id="82345678901234567", last_contiguous_message_id=_MESSAGE_IDS[-1]),
            ShopRedirect(source_shop_id=99, target_shop_id=3, reason="synthetic"),
        ])
        batch = ImportBatch(
            id="synthetic-batch", filename="synthetic.csv", sha256="e" * 64,
            row_count=1, message_count=1, review_count=1,
        )
        session.add(batch)
        session.flush()
        session.add(ImportRow(
            batch=batch, row_number=2, shop_id=1, message_id=_MESSAGE_IDS[0], created_at=_OLD_TIME,
            shop_name="保持する取込原本", is_visited=True, rating=5, memo="保持する取込メモ",
            source_url="https://fixture.example/import", needs_review=True, extraction_source="synthetic",
        ))
        session.commit()
        try:
            yield session
        finally:
            session.rollback()
    engine.dispose()


def _snapshot(db: Session) -> DatabaseSnapshot:
    return {
        table.name: tuple(
            tuple((column.name, row[column.name]) for column in table.columns)
            for row in db.execute(select(table).order_by(*table.primary_key.columns)).mappings()
        )
        for table in sorted(Base.metadata.tables.values(), key=lambda value: value.name)
    }


def _row(snapshot: DatabaseSnapshot, table: str, key: int) -> Row:
    return next(row for row in snapshot[table] if dict(row)["id"] == key)


def _without(row: Row, fields: set[str]) -> Row:
    return tuple((name, value) for name, value in row if name not in fields)


def _payload(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "expected_version": 13, "source_shop_id": _SOURCE, "source_shop_version": 7,
        "target_shop_id": _TARGET, "target_shop_version": 11, "note": _NOTE,
    }
    result.update(changes)
    return result


def _request(**changes: object) -> LinkMentionRequest:
    return LinkMentionRequest.model_validate(_payload(**changes))


def _assert_only_selected_link_changed(
    before: DatabaseSnapshot, after: DatabaseSnapshot, *, source_id: int | None,
) -> None:
    for name in before:
        if name not in {"shops", "shop_mentions", "review_events"}:
            assert after[name] == before[name], name
    changed_shops = {_TARGET} | ({source_id} if source_id is not None else set())
    assert len(after["shops"]) == len(before["shops"])
    for old_row in before["shops"]:
        old = dict(old_row)
        key = int(old["id"])
        current_row = _row(after, "shops", key)
        if key in changed_shops:
            assert _without(current_row, {"version", "updated_at"}) == _without(old_row, {"version", "updated_at"})
            assert dict(current_row)["version"] == old["version"] + 1
        else:
            assert current_row == old_row
    assert len(after["shop_mentions"]) == len(before["shop_mentions"])
    changed_mention_fields = {"shop_id", "review_status", "resolution_status", "resolution_method", "reviewed_at", "version"}
    for old_row in before["shop_mentions"]:
        key = int(dict(old_row)["id"])
        current_row = _row(after, "shop_mentions", key)
        assert _without(current_row, changed_mention_fields if key == _SELECTED else set()) == _without(
            old_row, changed_mention_fields if key == _SELECTED else set(),
        )
    selected = dict(_row(after, "shop_mentions", _SELECTED))
    assert (selected["shop_id"], selected["review_status"], selected["resolution_status"], selected["resolution_method"]) == (
        _TARGET, "approved", "resolved", "manual",
    )
    assert selected["version"] == dict(_row(before, "shop_mentions", _SELECTED))["version"] + 1
    assert selected["reviewed_at"] is not None
    old_events = {dict(row)["id"]: row for row in before["review_events"]}
    new_events = {dict(row)["id"]: row for row in after["review_events"]}
    assert len(new_events) == len(old_events) + 1
    assert all(new_events[key] == value for key, value in old_events.items())
    event_id = next(iter(set(new_events) - set(old_events)))
    saved = dict(new_events[event_id])
    assert (saved["mention_id"], saved["scope"], saved["action"], saved["previous_shop_id"], saved["selected_shop_id"], saved["note"]) == (
        _SELECTED, "identity", "link_existing", source_id, _TARGET, _NOTE,
    )


@pytest.mark.parametrize("source_mode", ["shared", "last", "unlinked"])
def test_link_changes_only_one_mention_and_keeps_every_shop_and_related_record(db: Session, source_mode: str) -> None:
    if source_mode == "last":
        db.get(ShopMention, 2).shop_id = 3
    elif source_mode == "unlinked":
        db.get(ShopMention, _SELECTED).shop_id = None
    db.commit()
    source_id = None if source_mode == "unlinked" else _SOURCE
    before = _snapshot(db)
    result = link_mention(db, _SELECTED, _request(
        source_shop_id=source_id, source_shop_version=7 if source_id is not None else None,
    ))
    assert (result.mention_id, result.shop_id, result.version) == (_SELECTED, _TARGET, 14)
    assert result.automatically_resolved_count == 0
    assert not result.automatically_resolved_mention_ids
    assert result.metadata_review_status == "deferred"
    assert result.shop is not None and (result.shop.id, result.shop.version) == (_TARGET, 12)
    if source_id is None:
        assert result.source_shop is None
    else:
        assert result.source_shop is not None
        assert (result.source_shop.id, result.source_shop.version) == (_SOURCE, 8)
    _assert_only_selected_link_changed(before, _snapshot(db), source_id=source_id)
    if source_mode == "last":
        assert db.get(Shop, _SOURCE) is not None
        assert db.query(ShopMention).filter_by(shop_id=_SOURCE).count() == 0


@pytest.mark.parametrize("source_mode,source_count,will_empty", [("shared", 2, False), ("last", 1, True), ("unlinked", 0, False)])
def test_preview_is_read_only_and_reports_current_source_target_and_counts(
    db: Session, source_mode: str, source_count: int, will_empty: bool,
) -> None:
    if source_mode == "last":
        db.get(ShopMention, 2).shop_id = 3
    elif source_mode == "unlinked":
        db.get(ShopMention, _SELECTED).shop_id = None
    db.commit()
    before = _snapshot(db)
    db.execute(text("PRAGMA query_only=ON"))
    try:
        result = preview_mention_link(db, _SELECTED, _TARGET)
        assert (result.mention_id, result.expected_version) == (_SELECTED, 13)
        assert result.source_mention_count == source_count
        assert result.target_mention_count == 2
        assert result.source_will_be_empty is will_empty
        assert (result.review_status, result.metadata_review_status) == ("pending", "deferred")
        assert (result.target_shop.id, result.target_shop.version, result.target_shop.memo, result.target_shop.image_key) == (
            _TARGET, 11, "保持する店舗別メモ2", "2" * 64,
        )
        if source_mode == "unlinked":
            assert result.source_shop is None
        else:
            assert result.source_shop is not None
            assert (result.source_shop.id, result.source_shop.version, result.source_shop.rating) == (_SOURCE, 7, 2)
        assert _snapshot(db) == before
    finally:
        db.rollback()
        db.execute(text("PRAGMA query_only=OFF"))


def test_preview_does_not_flush_or_replace_an_unrelated_unsaved_shop_edit(db: Session) -> None:
    before = _snapshot(db)
    target = db.get(Shop, _TARGET)
    target.memo = "保存していない合成入力"
    db.autoflush = True
    try:
        preview = preview_mention_link(db, _SELECTED, _TARGET)
        assert preview.target_shop.memo == "保持する店舗別メモ2"
        assert target.memo == "保存していない合成入力"
        assert db.is_modified(target)
        assert _snapshot(db) == before
    finally:
        db.rollback()


@pytest.mark.parametrize("field,value", [("expected_version", 12), ("source_shop_version", 6), ("target_shop_version", 10)])
def test_stale_mention_or_either_shop_version_cannot_change_any_row(db: Session, field: str, value: int) -> None:
    before = _snapshot(db)
    with pytest.raises(ReviewConflictError):
        link_mention(db, _SELECTED, _request(**{field: value}))
    db.rollback()
    assert _snapshot(db) == before


def test_another_post_updating_target_shop_invalidates_link_preview(db: Session) -> None:
    request = _request()
    with Session(db.get_bind()) as other:
        target = other.get(Shop, _TARGET)
        target.memo = "別投稿の確認中に保存された合成メモ"
        target.version += 1
        other.commit()
    before = _snapshot(db)
    assert dict(_row(before, "shop_mentions", _SELECTED))["version"] == request.expected_version
    with pytest.raises(ReviewConflictError):
        link_mention(db, _SELECTED, request)
    db.rollback()
    assert _snapshot(db) == before


@pytest.mark.parametrize("new_source", [None, 3])
def test_expected_source_identity_drift_is_rejected_even_without_mention_version_change(db: Session, new_source: int | None) -> None:
    request = _request()
    selected = db.get(ShopMention, _SELECTED)
    selected.shop_id = new_source
    db.commit()
    before = _snapshot(db)
    with pytest.raises(ReviewConflictError) as caught:
        link_mention(db, _SELECTED, request)
    assert caught.value.code == "source_changed"
    db.rollback()
    assert _snapshot(db) == before


def test_identical_retry_creates_no_second_event_or_extra_version_change(db: Session) -> None:
    request = _request()
    link_mention(db, _SELECTED, request)
    after_first = _snapshot(db)
    with pytest.raises(ReviewConflictError):
        link_mention(db, _SELECTED, request)
    db.rollback()
    assert _snapshot(db) == after_first


def test_history_write_failure_rolls_back_link_and_both_shop_versions(db: Session) -> None:
    before = _snapshot(db)
    bind = db.get_bind()

    def fail_history(
        _connection: Connection, _cursor: object, statement: str,
        _parameters: object, _context: object, _executemany: bool,
    ) -> None:
        if statement.lower().lstrip().startswith("insert into review_events"):
            raise RuntimeError("Synthetic history persistence failure")

    event.listen(bind, "before_cursor_execute", fail_history)
    try:
        with pytest.raises(RuntimeError, match="Synthetic history persistence failure"):
            link_mention(db, _SELECTED, _request())
    finally:
        event.remove(bind, "before_cursor_execute", fail_history)
    assert _snapshot(db) == before


def test_link_to_current_shop_is_rejected_without_changes(db: Session) -> None:
    before = _snapshot(db)
    with pytest.raises(ReviewConflictError) as caught:
        link_mention(db, _SELECTED, _request(target_shop_id=_SOURCE, target_shop_version=7))
    assert caught.value.code == "same_shop"
    db.rollback()
    assert _snapshot(db) == before


@pytest.mark.parametrize("mention_id,target_id", [(999, _TARGET), (_SELECTED, 999)])
def test_missing_mention_or_target_is_rejected_without_changes(db: Session, mention_id: int, target_id: int) -> None:
    before = _snapshot(db)
    with pytest.raises(ReviewNotFoundError):
        preview_mention_link(db, mention_id, target_id)
    db.rollback()
    with pytest.raises(ReviewNotFoundError):
        link_mention(db, mention_id, _request(target_shop_id=target_id))
    db.rollback()
    assert _snapshot(db) == before


@pytest.mark.parametrize("field", ["expected_version", "source_shop_id", "source_shop_version", "target_shop_id", "target_shop_version", "note"])
def test_all_version_source_and_reason_fields_are_required(field: str) -> None:
    payload = _payload()
    del payload[field]
    with pytest.raises(ValidationError):
        LinkMentionRequest.model_validate(payload)


@pytest.mark.parametrize("changes", [
    {"source_shop_id": None}, {"source_shop_version": None}, {"source_shop_id": 0},
    {"source_shop_version": 0}, {"target_shop_id": -1}, {"target_shop_version": 0},
    {"expected_version": 0}, {"note": ""}, {"note": " \n\t "}, {"note": "x" * 2001},
    {"memo": "店舗メモを上書きする余分な入力"},
    {"target_shop_id": True}, {"target_shop_version": 11.0}, {"expected_version": "13"},
    {"source_shop_id": "1"}, {"source_shop_version": False}, {"note": 42},
])
def test_invalid_link_request_shapes_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LinkMentionRequest.model_validate(_payload(**changes))


def test_unlinked_request_requires_explicit_null_pair_and_normalizes_reason() -> None:
    request = _request(source_shop_id=None, source_shop_version=None, note=f"  {_NOTE}  ")
    assert request.source_shop_id is None and request.source_shop_version is None
    assert request.note == _NOTE


def _test_app(db: Session) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="synthetic-mention-link-session")

    def get_db() -> Iterator[Session]:
        yield db

    @app.get("/test/session")
    def authenticate(request: Request) -> dict[str, str]:
        request.session["admin_authenticated"] = True
        request.session["csrf_token"] = "synthetic-csrf"
        return {"csrf_token": "synthetic-csrf"}

    app.include_router(review.router)
    app.dependency_overrides[review.get_db] = get_db
    return app


def test_api_guards_preview_and_link_with_admin_csrf_and_read_only_mode(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _snapshot(db)
    with TestClient(_test_app(db)) as client:
        assert client.get("/api/admin/reviews/1/link-preview?target_shop_id=2").status_code == 403
        assert client.post("/api/admin/reviews/1/link", json=_payload()).status_code == 403
        client.get("/test/session")
        preview = client.get("/api/admin/reviews/1/link-preview?target_shop_id=2")
        assert preview.status_code == 200
        assert "no-store" in preview.headers["cache-control"]
        assert client.post("/api/admin/reviews/1/link", json=_payload()).status_code == 403
        monkeypatch.setenv("APP_READ_ONLY", "true")
        assert client.get("/api/admin/reviews/1/link-preview?target_shop_id=2").status_code == 200
        blocked = client.post("/api/admin/reviews/1/link", json=_payload(), headers={"X-CSRF-Token": "synthetic-csrf"})
        assert blocked.status_code == 503
    db.rollback()
    assert _snapshot(db) == before


def test_api_links_once_and_returns_retry_conflict_without_more_changes(db: Session) -> None:
    before = _snapshot(db)
    with TestClient(_test_app(db)) as client:
        client.get("/test/session")
        first = client.post("/api/admin/reviews/1/link", json=_payload(), headers={"X-CSRF-Token": "synthetic-csrf"})
        assert first.status_code == 200
        assert "no-store" in first.headers["cache-control"]
        assert first.json()["shop_id"] == _TARGET
        assert first.json()["shop"]["version"] == 12
        assert first.json()["source_shop"]["id"] == _SOURCE
        assert first.json()["source_shop"]["version"] == 8
        after_first = _snapshot(db)
        _assert_only_selected_link_changed(before, after_first, source_id=_SOURCE)
        retry = client.post("/api/admin/reviews/1/link", json=_payload(), headers={"X-CSRF-Token": "synthetic-csrf"})
        assert retry.status_code == 409
        assert "no-store" in retry.headers["cache-control"]
        assert retry.json()["detail"]["code"] == "stale_mention"
    db.rollback()
    assert _snapshot(db) == after_first
