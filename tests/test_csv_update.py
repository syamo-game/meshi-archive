from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier, local

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from db.models import (
    Base, Message, ProcessingRun, ResolutionCandidate, ReviewEvent, Shop,
    ShopMention, ShopRedirect, SourceAsset, SyncState,
)
from services.import_service import (
    ImportValidationFailure, apply_csv_update, parse_csv_update, preview_csv_update,
)
from web.routers import admin


def _message_id(shop_id: int) -> str:
    return str(12345678901234567 + shop_id)


def _row(shop_id: int = 1, **changes: str) -> dict[str, str]:
    return {
        "_id": str(shop_id), "shop.version": "1", "message_id": _message_id(shop_id),
        "shop.name": f"修正した店舗{shop_id}", **changes,
    }


def _csv(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.execute(text("PRAGMA foreign_keys=ON"))
        for shop_id in range(1, 18):
            session.add(Shop(
                id=shop_id, shop_name=f"店舗{shop_id}", area="神田", category="寿司・回転寿司",
                is_visited=True, visited_at=datetime(2026, 1, 2, 12, 34, 56),
                rating=4, memo="残すメモ", image_key="a" * 64,
                canonical_url=f"https://example.com/shop/{shop_id}",
            ))
            session.add(Message(message_id=_message_id(shop_id), content="元の投稿本文"))
        session.flush()
        for shop_id in range(1, 18):
            session.add(ShopMention(
                id=shop_id, shop_id=shop_id, message_id=_message_id(shop_id),
                occurrence_index=0, extracted_name=f"抽出店舗{shop_id}",
                source_url=f"https://example.com/post/{shop_id}",
                extracted_category="細かな元分類", review_status="pending",
            ))
        session.flush()
        session.add(ShopMention(
            id=18, shop_id=1, message_id=_message_id(2), occurrence_index=1,
            extracted_name="同じ店への別の投稿", review_status="approved",
        ))
        session.add(ShopMention(
            id=19, shop_id=None, message_id=_message_id(1), occurrence_index=1,
            extracted_name="未解決投稿",
        ))
        session.add(SourceAsset(message_id=_message_id(1), kind="attachment", url="https://example.com/image.png"))
        session.add(ReviewEvent(mention_id=1, action="defer", note="元の確認履歴"))
        session.add(ResolutionCandidate(mention_id=1, rank=1, name="確認候補"))
        session.add(ProcessingRun(message_id=_message_id(1), stage="extract", status="succeeded"))
        session.add(ShopRedirect(source_shop_id=900, target_shop_id=1))
        session.add(SyncState(channel_id="12345678901234567", last_contiguous_message_id=_message_id(1)))
        session.commit()
        yield session
    engine.dispose()


def _related_snapshot(db: Session) -> dict[str, list[tuple[object, ...]]]:
    return {
        name: [tuple(row) for row in db.execute(table.select()).all()]
        for name, table in Base.metadata.tables.items() if name != "shops"
    }


def test_five_updates_preserve_other_12_shops_and_every_related_record(db: Session) -> None:
    before_related = _related_snapshot(db)
    before_shops = {
        shop.id: (shop.memo, shop.visited_at, shop.rating, shop.canonical_url, shop.image_key)
        for shop in db.query(Shop).all()
    }
    parsed = parse_csv_update(_csv([_row(i) for i in range(1, 6)]), "changes.csv")
    preview = preview_csv_update(db, parsed)
    assert (preview.inserted, preview.updated, preview.deleted) == (0, 5, 0)
    assert preview.changes[0].previous == "店舗1"
    assert preview.changes[0].proposed == "修正した店舗1"
    assert db.get(Shop, 1).shop_name == "店舗1"

    apply_csv_update(db, parsed)

    assert db.query(Shop).count() == 17
    assert _related_snapshot(db) == before_related
    for shop in db.query(Shop).all():
        assert (shop.memo, shop.visited_at, shop.rating, shop.canonical_url, shop.image_key) == before_shops[shop.id]
        assert shop.shop_name == (f"修正した店舗{shop.id}" if shop.id <= 5 else f"店舗{shop.id}")
        assert shop.version == (2 if shop.id <= 5 else 1)


def test_update_template_round_trip_only_changes_edited_fields(
    client: TestClient, db: Session,
) -> None:
    template = client.get("/admin/import/update/template.csv")
    assert template.status_code == 200
    assert template.headers["cache-control"] == "private, no-store, max-age=0"
    rows = list(csv.DictReader(io.StringIO(template.content.decode("utf-8-sig"))))
    assert len(rows) == 17
    assert set(rows[0]) == {"_id", "shop.version", "message_id", *admin.CSV_UPDATE_COLUMNS}
    assert rows[0]["visited_at"] == "2026-01-02 12:34:56"
    assert rows[0]["shop.name"] == "店舗1"
    selected = rows[0]
    selected["shop.name"] = "修正後の店舗"
    raw = _csv([selected])
    preview = preview_csv_update(db, parse_csv_update(raw, "changes.csv"))
    assert preview.row_count == 1
    assert preview.updated == 1
    assert [(change.column, change.proposed) for change in preview.changes] == [
        ("shop.name", "修正後の店舗"),
    ]
    assert _post_csv(client, "/admin/import/validate", raw).status_code == 200
    assert _post_csv(client, "/admin/import/update/apply", raw).status_code == 200
    assert db.get(Shop, 1).shop_name == "修正後の店舗"
    assert db.get(Shop, 1).visited_at == datetime(2026, 1, 2, 12, 34, 56)
    assert db.get(Shop, 2).version == 1


def test_unchanged_template_row_does_not_increment_version(client: TestClient, db: Session) -> None:
    template = client.get("/admin/import/update/template.csv")
    row = next(csv.DictReader(io.StringIO(template.content.decode("utf-8-sig"))))
    parsed = parse_csv_update(_csv([row]), "changes.csv")
    preview = preview_csv_update(db, parsed)
    assert preview.updated == 0
    assert preview.changes == ()
    apply_csv_update(db, parsed)
    assert db.get(Shop, 1).version == 1


def test_unchanged_row_in_mixed_file_is_not_written(client: TestClient, db: Session) -> None:
    template = client.get("/admin/import/update/template.csv")
    rows = list(csv.DictReader(io.StringIO(template.content.decode("utf-8-sig"))))
    rows[0]["shop.name"] = "更新店舗"
    parsed = parse_csv_update(_csv(rows[:2]), "changes.csv")
    preview = preview_csv_update(db, parsed)
    assert (preview.row_count, preview.updated, len(preview.changes)) == (2, 1, 1)
    apply_csv_update(db, parsed)
    assert db.get(Shop, 1).version == 2
    assert db.get(Shop, 2).version == 1


def test_existing_unclassified_values_do_not_block_other_template_edits(
    client: TestClient, db: Session,
) -> None:
    shop = db.get(Shop, 1)
    shop.area = "未同定地域"
    shop.category = "ジェラート"
    db.commit()
    template = client.get("/admin/import/update/template.csv")
    row = next(csv.DictReader(io.StringIO(template.content.decode("utf-8-sig"))))
    row["memo"] = "追記したメモ"
    parsed = parse_csv_update(_csv([row]), "changes.csv")
    preview = preview_csv_update(db, parsed)
    assert [(change.column, change.proposed) for change in preview.changes] == [("memo", "追記したメモ")]
    apply_csv_update(db, parsed)
    assert (shop.area, shop.category, shop.memo) == ("未同定地域", "ジェラート", "追記したメモ")


@pytest.mark.parametrize(
    ("memo", "escaped"),
    [
        ("=SUM(1,2)", "'=SUM(1,2)"),
        ("'=SUM(1,2)", "''=SUM(1,2)"),
        ("''=SUM(1,2)", "'''=SUM(1,2)"),
    ],
)
def test_update_template_escapes_formula_and_restores_original_value(
    client: TestClient, db: Session, memo: str, escaped: str,
) -> None:
    shop = db.get(Shop, 1)
    shop.memo = memo
    db.commit()
    template = client.get("/admin/import/update/template.csv")
    row = next(csv.DictReader(io.StringIO(template.content.decode("utf-8-sig"))))
    assert row["memo"] == escaped
    parsed = parse_csv_update(_csv([row]), "changes.csv")
    assert preview_csv_update(db, parsed).updated == 0
    apply_csv_update(db, parsed)
    assert db.get(Shop, 1).memo == memo


def test_update_template_preserves_untouched_multiline_and_padded_text(
    client: TestClient, db: Session,
) -> None:
    shop = db.get(Shop, 1)
    shop.memo = "  注意事項\n"
    shop.address = "  合成住所  "
    db.commit()
    template = client.get("/admin/import/update/template.csv")
    row = next(csv.DictReader(io.StringIO(template.content.decode("utf-8-sig"))))
    assert (row["memo"], row["shop.address"]) == ("  注意事項\n", "  合成住所  ")
    row["shop.name"] = "修正後の店舗"
    parsed = parse_csv_update(_csv([row]), "changes.csv")
    preview = preview_csv_update(db, parsed)
    assert [(change.column, change.proposed) for change in preview.changes] == [
        ("shop.name", "修正後の店舗"),
    ]
    apply_csv_update(db, parsed)
    assert (shop.memo, shop.address) == ("  注意事項\n", "  合成住所  ")


def test_blank_explicit_optional_column_clears_only_that_column(db: Session) -> None:
    row = _row(memo="")
    del row["shop.name"]
    apply_csv_update(db, parse_csv_update(_csv([row]), "changes.csv"))
    shop = db.get(Shop, 1)
    assert shop.memo is None
    assert shop.shop_name == "店舗1"
    assert shop.rating == 4
    assert shop.is_visited is True
    assert shop.visited_at == datetime(2026, 1, 2, 12, 34, 56)


def test_update_after_preview_rejects_entire_file_and_preserves_other_users_edit(db: Session) -> None:
    parsed = parse_csv_update(_csv([_row(1), _row(2)]), "changes.csv")
    preview_csv_update(db, parsed)
    other_edit = db.get(Shop, 2)
    other_edit.shop_name = "他者の変更"
    other_edit.version += 1
    db.commit()
    with pytest.raises(ImportValidationFailure, match="更新済み"):
        apply_csv_update(db, parsed)
    assert db.get(Shop, 1).shop_name == "店舗1"
    assert db.get(Shop, 2).shop_name == "他者の変更"


def test_database_failure_on_later_row_rolls_back_earlier_row(db: Session) -> None:
    db.execute(text(
        "CREATE TRIGGER reject_second BEFORE UPDATE ON shops WHEN OLD.id = 2 "
        "BEGIN SELECT RAISE(ABORT, 'synthetic later-row failure'); END"
    ))
    db.commit()
    parsed = parse_csv_update(_csv([_row(1), _row(2)]), "changes.csv")
    with pytest.raises(IntegrityError, match="synthetic later-row failure"):
        apply_csv_update(db, parsed)
    assert db.get(Shop, 1).shop_name == "店舗1"
    assert db.get(Shop, 1).version == 1


def test_clearing_visit_status_requires_explicit_date_clear(db: Session) -> None:
    parsed = parse_csv_update(_csv([_row(**{"status.is_visited": "false"})]), "changes.csv")
    with pytest.raises(ImportValidationFailure, match="visited_at"):
        apply_csv_update(db, parsed)
    assert db.get(Shop, 1).is_visited is True
    assert db.get(Shop, 1).visited_at == datetime(2026, 1, 2, 12, 34, 56)
    explicit_clear = parse_csv_update(
        _csv([_row(**{"status.is_visited": "false", "visited_at": ""})]), "changes.csv",
    )
    apply_csv_update(db, explicit_clear)
    assert db.get(Shop, 1).is_visited is False
    assert db.get(Shop, 1).visited_at is None


def test_date_only_update_cannot_make_an_unvisited_shop_inconsistent(db: Session) -> None:
    shop = db.get(Shop, 1)
    shop.is_visited = False
    shop.visited_at = None
    db.commit()
    parsed = parse_csv_update(_csv([_row(visited_at="2026-01-03")]), "changes.csv")
    with pytest.raises(ImportValidationFailure, match="status.is_visited"):
        apply_csv_update(db, parsed)
    assert db.get(Shop, 1).visited_at is None


def test_valid_classification_keeps_original_extraction_and_review_history(db: Session) -> None:
    before = _related_snapshot(db)
    parsed = parse_csv_update(
        _csv([_row(**{"shop.area": "神田", "shop.category": "スイーツ・洋菓子"})]), "changes.csv",
    )
    apply_csv_update(db, parsed)
    assert db.get(Shop, 1).category == "スイーツ・洋菓子"
    assert _related_snapshot(db) == before


def test_canonical_url_cannot_conflict_with_existing_external_identity(db: Session) -> None:
    shop = db.get(Shop, 1)
    shop.external_source = "tabelog"
    shop.external_id = "12345678"
    db.commit()
    parsed = parse_csv_update(
        _csv([_row(canonical_url="https://tabelog.com/tokyo/A1301/A130101/99999999/")]), "changes.csv",
    )
    with pytest.raises(ImportValidationFailure, match="識別子"):
        apply_csv_update(db, parsed)
    assert db.get(Shop, 1).canonical_url == "https://example.com/shop/1"


@pytest.mark.parametrize("shop_id,message_id", [(999, _message_id(1)), (1, _message_id(3))])
def test_unknown_shop_or_mismatched_message_is_not_inserted_or_relinked(
    db: Session, shop_id: int, message_id: str,
) -> None:
    parsed = parse_csv_update(_csv([_row(shop_id, message_id=message_id)]), "changes.csv")
    before = _related_snapshot(db)
    with pytest.raises(ImportValidationFailure):
        apply_csv_update(db, parsed)
    assert db.query(Shop).count() == 17
    assert _related_snapshot(db) == before


@pytest.mark.parametrize("column,value", [
    ("shop.area", "神田淡路町"), ("shop.area", ""),
    ("shop.category", "ジェラート"), ("shop.category", ""),
    ("shop.name", ""), ("status.is_visited", ""),
    ("rating", "6"), ("canonical_url", "javascript:alert(1)"),
    ("review_status", "approved"), ("source_url", "https://example.com"),
])
def test_invalid_or_unsupported_fields_are_rejected(db: Session, column: str, value: str) -> None:
    if column in {"shop.area", "shop.category"}:
        parsed = parse_csv_update(_csv([_row(**{column: value})]), "changes.csv")
        with pytest.raises(ImportValidationFailure):
            preview_csv_update(db, parsed)
    else:
        with pytest.raises(ImportValidationFailure):
            parse_csv_update(_csv([_row(**{column: value})]), "changes.csv")


def test_missing_version_duplicate_rows_and_malformed_csv_are_rejected() -> None:
    missing = _row()
    del missing["shop.version"]
    invalid_files = [
        _csv([missing]), _csv([_row(), _row()]),
        b"_id,shop.version,message_id,memo\n1,1,12345678901234568\n",
        b"_id,shop.version,message_id,memo\n1,1,12345678901234568,one,extra\n",
        b"_id,shop.version,message_id,memo,memo\n1,1,12345678901234568,one,two\n",
        b'_id,shop.version,message_id,memo\n1,1,12345678901234568,"unfinished\n',
    ]
    for raw in invalid_files:
        with pytest.raises(ImportValidationFailure):
            parse_csv_update(raw, "changes.csv")


@pytest.fixture
def client(db: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="synthetic-session-secret")
    app.include_router(admin.router, prefix="/admin")

    def database() -> Iterator[Session]:
        yield db

    @app.get("/test-login")
    def login(request: Request) -> dict[str, bool]:
        request.session.update(admin_authenticated=True, csrf_token="test-csrf")
        return {"ok": True}

    app.dependency_overrides[admin.get_db] = database
    with TestClient(app) as browser:
        browser.get("/test-login")
        yield browser


def _post_csv(client: TestClient, path: str, raw: bytes) -> Response:
    return client.post(path, data={"csrf_token": "test-csrf"}, files={"file": ("changes.csv", raw, "text/csv")})


def test_web_requires_the_previewed_bytes_and_preserves_old_full_replace_batches(
    client: TestClient, db: Session,
) -> None:
    original = _csv([_row()])
    changed = _csv([_row(**{"shop.name": "未検証の値"})])
    assert _post_csv(client, "/admin/import/validate", original).status_code == 200
    rejected = _post_csv(client, "/admin/import/update/apply", changed)
    assert rejected.status_code == 409
    assert "確認したCSVと一致しません" in rejected.text
    assert db.get(Shop, 1).shop_name == "店舗1"
    blocked = client.post("/admin/import/old-batch/apply", data={"csrf_token": "test-csrf"})
    assert blocked.status_code == 410
    assert db.query(Shop).count() == 17
    assert _post_csv(client, "/admin/import/update/apply", original).status_code == 200
    assert db.get(Shop, 1).shop_name == "修正した店舗1"
    assert _post_csv(client, "/admin/import/update/apply", original).status_code == 409


def test_web_apply_accepts_large_csv_within_import_limit(client: TestClient, db: Session) -> None:
    raw = _csv([_row(i, memo="x" * 19_000) for i in range(1, 6)])
    assert len(raw) > 64 * 1024
    assert _post_csv(client, "/admin/import/validate", raw).status_code == 200
    assert _post_csv(client, "/admin/import/update/apply", raw).status_code == 200
    assert db.get(Shop, 1).memo == "x" * 19_000


def test_web_apply_rechecks_version_and_requires_csrf(client: TestClient, db: Session) -> None:
    raw = _csv([_row()])
    assert _post_csv(client, "/admin/import/validate", raw).status_code == 200
    without_csrf = client.post("/admin/import/update/apply", files={"file": ("changes.csv", raw, "text/csv")})
    assert without_csrf.status_code == 422
    current = db.get(Shop, 1)
    current.version += 1
    current.memo = "他者のメモ"
    db.commit()
    assert _post_csv(client, "/admin/import/update/apply", raw).status_code == 409
    assert db.get(Shop, 1).memo == "他者のメモ"
    assert db.get(Shop, 1).shop_name == "店舗1"


def test_lost_response_retry_with_saved_preview_cookie_does_not_apply_twice(
    independent_client: TestClient, independent_engine: Engine,
) -> None:
    client = independent_client
    raw = _csv([_row(1), _row(2)])
    assert _post_csv(client, "/admin/import/validate", raw).status_code == 200
    saved_cookie = client.cookies.get("session")
    assert saved_cookie is not None
    assert _post_csv(client, "/admin/import/update/apply", raw).status_code == 200
    with Session(independent_engine) as db:
        committed = {
            name: [tuple(row) for row in db.execute(table.select()).all()]
            for name, table in Base.metadata.tables.items()
        }
        assert db.get(Shop, 1).version == 2
        assert db.get(Shop, 2).version == 2
    for _ in range(2):
        client.cookies.clear()
        client.cookies.set("session", saved_cookie, domain="testserver.local", path="/")
        replay = _post_csv(client, "/admin/import/update/apply", raw)
        assert replay.status_code == 409
        assert "更新済み" in replay.text
        with Session(independent_engine) as db:
            assert {
                name: [tuple(row) for row in db.execute(table.select()).all()]
                for name, table in Base.metadata.tables.items()
            } == committed


@pytest.fixture
def independent_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'csv-interleaving.sqlite').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for shop_id in (1, 2):
            db.add(Shop(id=shop_id, shop_name=f"店舗{shop_id}", memo="保存済みメモ", rating=4))
            db.add(Message(message_id=_message_id(shop_id), content="保存済み本文"))
        db.flush()
        for shop_id in (1, 2):
            db.add(ShopMention(
                id=shop_id, shop_id=shop_id, message_id=_message_id(shop_id),
                occurrence_index=0, extracted_name=f"投稿内名称{shop_id}",
            ))
        db.add(SourceAsset(message_id=_message_id(1), kind="image", url="https://example.com/kept.png"))
        db.add(ReviewEvent(mention_id=1, action="defer", note="保存済み履歴"))
        db.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def independent_client(independent_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="synthetic-session-secret")
    app.include_router(admin.router, prefix="/admin")

    def database() -> Iterator[Session]:
        with Session(independent_engine) as session:
            yield session

    @app.get("/test-login")
    def login(request: Request) -> dict[str, bool]:
        request.session.update(admin_authenticated=True, csrf_token="test-csrf")
        return {"ok": True}

    app.dependency_overrides[admin.get_db] = database
    with TestClient(app) as browser:
        browser.get("/test-login")
        yield browser


def test_two_sessions_applying_same_csv_concurrently_commit_exactly_once(independent_engine: Engine) -> None:
    parsed = parse_csv_update(_csv([_row(1), _row(2)]), "changes.csv")
    ready = Barrier(2, timeout=10)
    worker_state = local()

    def synchronize_first_update(
        connection: Connection, cursor: object, statement: str,
        parameters: object, context: object, executemany: bool,
    ) -> None:
        if statement.startswith("UPDATE shops ") and not getattr(worker_state, "ready", False):
            worker_state.ready = True
            ready.wait()

    def apply_in_own_session() -> str:
        with Session(independent_engine) as session:
            try:
                apply_csv_update(session, parsed)
                return "applied"
            except ImportValidationFailure:
                return "rejected"

    with Session(independent_engine) as before:
        related = _related_snapshot(before)
    event.listen(independent_engine, "before_cursor_execute", synchronize_first_update)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: apply_in_own_session(), range(2)))
    finally:
        event.remove(independent_engine, "before_cursor_execute", synchronize_first_update)
    assert sorted(results) == ["applied", "rejected"]
    with Session(independent_engine) as verified:
        assert [(shop.id, shop.version, shop.shop_name, shop.memo, shop.rating) for shop in verified.query(Shop).order_by(Shop.id)] == [
            (1, 2, "修正した店舗1", "保存済みメモ", 4),
            (2, 2, "修正した店舗2", "保存済みメモ", 4),
        ]
        assert _related_snapshot(verified) == related


def test_other_session_update_between_validation_and_write_rolls_back_all_csv_rows(
    independent_engine: Engine,
) -> None:
    parsed = parse_csv_update(_csv([_row(1), _row(2)]), "changes.csv")
    interleaved = False

    def edit_second_shop_before_first_csv_write(
        connection: Connection, cursor: object, statement: str,
        parameters: object, context: object, executemany: bool,
    ) -> None:
        nonlocal interleaved
        if interleaved or not statement.startswith("UPDATE shops "):
            return
        interleaved = True
        with Session(independent_engine) as other:
            shop = other.get(Shop, 2)
            assert shop is not None
            shop.memo = "別セッションで保存したメモ"
            shop.version += 1
            other.commit()

    event.listen(independent_engine, "before_cursor_execute", edit_second_shop_before_first_csv_write)
    try:
        with Session(independent_engine) as session:
            with pytest.raises(ImportValidationFailure, match="全行の更新を取り消し"):
                apply_csv_update(session, parsed)
    finally:
        event.remove(independent_engine, "before_cursor_execute", edit_second_shop_before_first_csv_write)
    assert interleaved
    with Session(independent_engine) as verified:
        first = verified.get(Shop, 1)
        second = verified.get(Shop, 2)
        assert (first.shop_name, first.version, first.memo) == ("店舗1", 1, "保存済みメモ")
        assert (second.shop_name, second.version, second.memo) == ("店舗2", 2, "別セッションで保存したメモ")


def test_related_data_saved_in_another_session_after_preview_is_preserved(independent_engine: Engine) -> None:
    parsed = parse_csv_update(_csv([_row(1)]), "changes.csv")
    with Session(independent_engine) as csv_session:
        preview_csv_update(csv_session, parsed)
        with Session(independent_engine) as other:
            message = other.get(Message, _message_id(1))
            assert message is not None
            message.content = "別セッションで補完した投稿本文"
            other.add(SourceAsset(message_id=message.message_id, kind="attachment", url="https://example.com/new.png"))
            other.add(ReviewEvent(mention_id=1, action="defer", note="別セッションの追加履歴"))
            other.commit()
            updated_related = _related_snapshot(other)
        apply_csv_update(csv_session, parsed)
    with Session(independent_engine) as verified:
        assert verified.get(Shop, 1).shop_name == "修正した店舗1"
        assert verified.get(Shop, 1).memo == "保存済みメモ"
        assert _related_snapshot(verified) == updated_related


def test_link_removed_in_another_session_before_write_rejects_all_csv_rows(independent_engine: Engine) -> None:
    parsed = parse_csv_update(_csv([_row(1), _row(2)]), "changes.csv")
    interleaved = False

    def unlink_second_shop_before_first_csv_write(
        connection: Connection, cursor: object, statement: str,
        parameters: object, context: object, executemany: bool,
    ) -> None:
        nonlocal interleaved
        if interleaved or not statement.startswith("UPDATE shops "):
            return
        interleaved = True
        with Session(independent_engine) as other:
            mention = other.get(ShopMention, 2)
            assert mention is not None
            mention.shop_id = None
            mention.version += 1
            other.add(ReviewEvent(mention_id=2, action="reject", note="別セッションの関連解除"))
            other.commit()

    event.listen(independent_engine, "before_cursor_execute", unlink_second_shop_before_first_csv_write)
    try:
        with Session(independent_engine) as session:
            with pytest.raises(ImportValidationFailure, match="全行の更新を取り消し"):
                apply_csv_update(session, parsed)
    finally:
        event.remove(independent_engine, "before_cursor_execute", unlink_second_shop_before_first_csv_write)
    assert interleaved
    with Session(independent_engine) as verified:
        assert [(shop.id, shop.version, shop.shop_name) for shop in verified.query(Shop).order_by(Shop.id)] == [
            (1, 1, "店舗1"), (2, 1, "店舗2"),
        ]
        assert verified.get(ShopMention, 2).shop_id is None
        assert verified.query(ReviewEvent).filter(ReviewEvent.mention_id == 2).one().note == "別セッションの関連解除"


def test_bulk_preview_names_and_values_are_readable_and_escaped(client: TestClient, db: Session) -> None:
    shop = db.get(Shop, 1)
    assert shop is not None
    shop.branch_name = "本店"
    db.commit()
    row = _row(**{
        "shop.branch_name": "駅前店", "shop.area": "銀座", "shop.category": "ラーメン",
        "shop.address": "銀座1-2-3", "shop.phone": "03-1234-5678",
        "canonical_url": "https://example.com/new", "status.is_visited": "false",
        "visited_at": "", "rating": "3", "memo": "<script>alert(1)</script>\n追記",
    })
    response = _post_csv(client, "/admin/import/validate", _csv([row]))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert 'id="bulk-selection"' in response.text and 'aria-label="CSVを編集" hidden' in response.text
    assert "店舗1 本店" in response.text
    assert "1店舗を変更" in response.text
    for label in ("店名", "支店", "エリア", "カテゴリ", "住所", "電話番号", "店舗URL", "訪問状況", "訪問日", "評価", "メモ"):
        assert f'scope="row">{label}</th>' in response.text
    assert "訪問済み" in response.text and "未訪問" in response.text
    assert "4 / 5" in response.text and "3 / 5" in response.text
    assert "2026/01/02 12:34:56" in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;\n追記" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert "店舗ID" not in response.text and "shop.name" not in response.text
    assert response.text.count('type="file"') == 1
    assert 'id="apply-file-input"' not in response.text
    assert "編集時の注意" not in response.text
    assert 'id="bulk-save"' in response.text
    assert shop.shop_name == "店舗1"


def test_bulk_unchanged_csv_has_back_without_save(client: TestClient, db: Session) -> None:
    response = _post_csv(client, "/admin/import/validate", _csv([_row(**{"shop.name": "店舗1"})]))
    assert response.status_code == 200
    assert "変更はありません。戻ってCSVを編集し直してください。" in response.text
    assert "0店舗を変更" in response.text
    assert "data-bulk-back" in response.text
    assert 'id="bulk-save"' not in response.text
    assert db.get(Shop, 1).version == 1


def test_bulk_complete_has_just_completion_and_continue(client: TestClient, db: Session) -> None:
    raw = _csv([_row(1), _row(2)])
    assert _post_csv(client, "/admin/import/validate", raw).status_code == 200
    response = _post_csv(client, "/admin/import/update/apply", raw)
    assert response.status_code == 200
    assert 'data-bulk-stage="complete"' in response.text
    assert "2店舗の情報を更新しました。" in response.text
    assert 'aria-label="CSVを編集" hidden' in response.text
    assert "data-bulk-again" in response.text
    assert 'id="bulk-save-form"' not in response.text
    assert db.get(Shop, 1).version == 2


@pytest.mark.parametrize(("column", "value", "expected"), [
    ("rating", "6", "2行目：評価は1〜5の整数で入力してください。"),
    ("shop.name", " ", "2行目：店名は1〜500文字で入力してください。"),
    ("status.is_visited", "maybe", "2行目：訪問状況はtrue（訪問済み）またはfalse（未訪問）で入力してください。"),
    ("visited_at", "yesterday", "2行目：訪問日はYYYY-MM-DD形式で入力してください。"),
    ("canonical_url", "javascript:alert(1)", "2行目：店舗URLはhttpまたはhttpsで始まるURLを入力してください。"),
    ("message_id", "bad", "2行目：編集用CSVを取り直し、投稿の識別情報は変更しないでください。"),
    ("shop.area", "unknown", "エリア は登録済みの市区町村名・駅名を指定してください。"),
    ("shop.category", "unknown", "カテゴリ は確認画面の既定分類を指定してください。"),
])
def test_bulk_invalid_values_show_the_row_field_and_correction(
    client: TestClient, db: Session, column: str, value: str, expected: str,
) -> None:
    response = _post_csv(client, "/admin/import/validate", _csv([_row(**{column: value})]))
    assert response.status_code == 400
    assert expected in response.text
    assert 'data-bulk-stage="preview"' not in response.text
    assert 'id="bulk-save"' not in response.text
    assert "Value error" not in response.text and "must be" not in response.text
    assert db.get(Shop, 1).version == 1


def test_bulk_metadata_errors_do_not_expose_internal_identifiers(client: TestClient) -> None:
    response = _post_csv(client, "/admin/import/validate", _csv([_row(**{"shop.version": "0"})]))
    assert response.status_code == 400
    assert "編集用CSVを取り直し" in response.text
    assert "shop_version" not in response.text
    response = _post_csv(client, "/admin/import/validate", _csv([_row(), _row()]))
    assert "3行目：同じ店舗の行が重複しています。1店舗につき1行にしてください。" in response.text
    assert "_id=" not in response.text


def test_bulk_conflict_message_keeps_rows_and_explains_recovery(client: TestClient, db: Session) -> None:
    raw = _csv([_row()])
    assert _post_csv(client, "/admin/import/validate", raw).status_code == 200
    shop = db.get(Shop, 1)
    assert shop is not None
    shop.version += 1
    shop.memo = "別の変更"
    db.commit()
    response = _post_csv(client, "/admin/import/update/apply", raw)
    assert response.status_code == 409
    assert "2行目：この店舗は更新済みです。最新のCSVをダウンロードして編集し直してください。" in response.text
    assert "CSV version=" not in response.text
    assert shop.memo == "別の変更" and shop.shop_name == "店舗1"


def test_bulk_preview_groups_changes_without_combining_distinct_same_name_shops(
    client: TestClient, db: Session,
) -> None:
    for shop_id in (1, 2):
        shop: Shop | None = db.get(Shop, shop_id)
        assert shop is not None
        shop.shop_name = "同じ店名"
    db.commit()
    raw: bytes = _csv([
        _row(1, **{"shop.name": "同じ店名", "memo": "店舗1の新しいメモ", "rating": "3"}),
        _row(2, **{"shop.name": "同じ店名", "memo": "店舗2の新しいメモ", "rating": "5"}),
    ])
    response: Response = _post_csv(client, "/admin/import/validate", raw)
    assert response.status_code == 200
    sections: list[str] = response.text.split('<section class="bulk-shop-changes"')[1:]
    assert len(sections) == 2
    for index, section in enumerate(sections, start=1):
        group: str = section.split('</section>', 1)[0]
        assert group.count('>同じ店名</h3>') == 1
        assert f'店舗{index}の新しいメモ' in group
        assert f'店舗{3 - index}の新しいメモ' not in group
        assert 'data-label="変更前"' in group and 'data-label="変更後"' in group
    assert db.get(Shop, 1).memo == "残すメモ"
    assert db.get(Shop, 2).memo == "残すメモ"
