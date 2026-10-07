from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from queue import Queue
from threading import Event

import pytest
from sqlalchemy import Connection, Engine, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from bot.restaurant_extractor import (
    ExtractedMention, ExtractedMessage, ExtractionCallResult, ModelCallMetrics,
)
from db.models import (
    Base, Message, ProcessingRun, ResolutionCandidate, ReviewEvent,
    Shop, ShopMention, ShopRedirect, SourceAsset,
)
from services import duplicate_service, evaluation_service
from services.import_service import ImportValidationFailure, apply_csv_update, parse_csv_update
from services.mention_linking import LinkMentionRequest, link_mention, preview_mention_link
from services.review_service import (
    DeferDecision, EditAndApproveDecision, EditableShop, ReviewConflictError,
    apply_review_decision,
)
from web.routers.home import _reserve_shop_version


@pytest.mark.parametrize("same_mention", [True, False])
def test_postgresql_single_link_waits_and_rejects_replay_or_opposite_link(
    pg_engine: Engine, same_mention: bool,
) -> None:
    records = _seed(pg_engine, duplicate_shops=True)
    assert records.second_shop_id is not None
    first_request = LinkMentionRequest(
        expected_version=1, source_shop_id=records.shop_id, source_shop_version=1,
        target_shop_id=records.second_shop_id, target_shop_version=1,
        note="The source identifies the existing shop.",
    )
    second_request = first_request if same_mention else LinkMentionRequest(
        expected_version=1, source_shop_id=records.second_shop_id, source_shop_version=1,
        target_shop_id=records.shop_id, target_shop_version=1,
        note="An older preview selected the opposite shop.",
    )
    second_mention_id = records.first_mention_id if same_mention else records.second_mention_id
    pids: Queue[int] = Queue()

    def second_link() -> str:
        with Session(pg_engine) as db:
            pids.put(_pid(db.connection()))
            try:
                link_mention(db, second_mention_id, second_request)
            except ReviewConflictError as exc:
                db.rollback()
                return exc.code
            return "saved"

    with pg_engine.connect() as connection:
        transaction = connection.begin()
        first_pid = _pid(connection)
        with Session(bind=connection, join_transaction_mode="rollback_only") as first:
            result = link_mention(first, records.first_mention_id, first_request)
            assert result.automatically_resolved_count == 0
            assert transaction.is_active
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(second_link)
                try:
                    _wait_until_blocked(connection, blocked_pid=pids.get(timeout=5), blocker_pid=first_pid)
                    transaction.commit()
                    assert future.result(timeout=20) == ("stale_mention" if same_mention else "stale_shop")
                finally:
                    if transaction.is_active:
                        transaction.rollback()
    with Session(pg_engine) as verified:
        source = verified.get(Shop, records.shop_id)
        target = verified.get(Shop, records.second_shop_id)
        assert source is not None and target is not None
        assert (source.version, target.version) == (2, 2)
        assert source.memo == target.memo == "保持するメモ"
        assert source.mentions == []
        assert verified.get(ShopMention, records.first_mention_id).version == 2
        assert verified.get(ShopMention, records.second_mention_id).version == 1
        assert {mention.shop_id for mention in verified.scalars(select(ShopMention))} == {target.id}
        assert verified.query(ReviewEvent).one().action == "link_existing"
        assert verified.query(ShopRedirect).count() == 0
        assert verified.query(SourceAsset).one().url == "https://example.com/kept.pdf"
        assert verified.query(ResolutionCandidate).count() == 1


@pytest.mark.parametrize("edited_role", ["source", "target"])
def test_postgresql_link_preserves_shop_edit_committed_after_preview(
    pg_engine: Engine, edited_role: str,
) -> None:
    records = _seed(pg_engine, duplicate_shops=True)
    assert records.second_shop_id is not None
    with Session(pg_engine) as reader:
        preview = preview_mention_link(reader, records.first_mention_id, records.second_shop_id)
        assert preview.source_shop is not None
        request = LinkMentionRequest(
            expected_version=preview.expected_version,
            source_shop_id=preview.source_shop.id, source_shop_version=preview.source_shop.version,
            target_shop_id=preview.target_shop.id, target_shop_version=preview.target_shop.version,
            note="The earlier preview must not override another edit.",
        )
    pids: Queue[int] = Queue()

    def stale_link() -> str:
        with Session(pg_engine) as db:
            pids.put(_pid(db.connection()))
            try:
                link_mention(db, records.first_mention_id, request)
            except ReviewConflictError as exc:
                db.rollback()
                return exc.code
            return "saved"

    edited_id = records.shop_id if edited_role == "source" else records.second_shop_id
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        editor_pid = _pid(connection)
        with Session(bind=connection, join_transaction_mode="rollback_only") as editor:
            shop = editor.get(Shop, edited_id)
            assert shop is not None and _reserve_shop_version(editor, shop)
            shop.memo = "プレビュー後に保存した利用者メモ"
            editor.commit()
            assert transaction.is_active
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(stale_link)
                try:
                    _wait_until_blocked(connection, blocked_pid=pids.get(timeout=5), blocker_pid=editor_pid)
                    transaction.commit()
                    assert future.result(timeout=20) == "stale_shop"
                finally:
                    if transaction.is_active:
                        transaction.rollback()
    with Session(pg_engine) as verified:
        shop = verified.get(Shop, edited_id)
        assert shop is not None and shop.memo == "プレビュー後に保存した利用者メモ"
        assert shop.version == 2
        assert verified.get(ShopMention, records.first_mention_id).shop_id == records.shop_id
        assert {mention.version for mention in verified.scalars(select(ShopMention))} == {1}
        assert verified.query(ReviewEvent).count() == 0
        assert verified.query(Shop).count() == 2


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    url = os.getenv("TEST_POSTGRES_URL", "")
    if not url or os.getenv("ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST") != "1":
        pytest.skip("An opted-in TEST_POSTGRES_URL is required for PostgreSQL business tests")
    parsed = make_url(url)
    database = parsed.database or ""
    if not parsed.drivername.startswith("postgresql") or not (
        database.startswith("test_") or database.endswith("_test")
    ):
        raise RuntimeError("PostgreSQL business tests require a database named test_* or *_test")

    schema = f"review_pg_{uuid.uuid4().hex}"
    options = "-c statement_timeout=15000 -c lock_timeout=10000"
    owner = create_engine(url, connect_args={"connect_timeout": 5, "options": options})
    database_engine: Engine | None = None
    created = False
    try:
        with owner.begin() as connection:
            connection.execute(CreateSchema(schema))
            created = True
        database_engine = create_engine(
            url, pool_size=5, max_overflow=0,
            connect_args={"connect_timeout": 5, "options": f"{options} -c search_path={schema}"},
        )
        with database_engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
        Base.metadata.create_all(database_engine)
        yield database_engine
    finally:
        if database_engine is not None:
            database_engine.dispose()
        if created:
            with owner.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        owner.dispose()


@dataclass(frozen=True)
class _Records:
    shop_id: int
    first_mention_id: int
    second_mention_id: int
    first_message_id: str
    second_shop_id: int | None = None


def _seed(engine: Engine, *, duplicate_shops: bool = False) -> _Records:
    with Session(engine) as db:
        first = Shop(
            shop_name="銀座鮨はな", area="銀座", category="寿司・回転寿司",
            address="東京都中央区銀座1-2-3", phone="0312345678", memo="保持するメモ",
        )
        second = Shop(
            shop_name=first.shop_name, area=first.area, category=first.category,
            address=first.address, phone=first.phone, memo=first.memo,
        ) if duplicate_shops else first
        mentions: list[ShopMention] = []
        for index, shop in enumerate((first, second)):
            message = Message(message_id=f"8534567890123456{index}", content="銀座鮨はな 銀座")
            mention = ShopMention(
                shop=shop, message=message, occurrence_index=0,
                extracted_name=shop.shop_name, extracted_area="銀座", extracted_category=shop.category,
                review_status="approved", metadata_review_status="approved", resolution_status="resolved",
                resolution_method="manual", extraction_source="legacy_import",
                source_url=f"https://example.com/post/{index}", confidence_reason="保存済み根拠",
            )
            db.add(mention)
            mentions.append(mention)
        db.flush()
        db.add(SourceAsset(message_id=mentions[0].message_id, kind="attachment", url="https://example.com/kept.pdf"))
        db.add(ResolutionCandidate(mention_id=mentions[0].id, rank=1, name="保持する候補", provenance="web_search"))
        db.commit()
        return _Records(first.id, mentions[0].id, mentions[1].id, mentions[0].message_id,
                        second.id if duplicate_shops else None)


def _pid(connection: Connection) -> int:
    value = connection.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(value, int)
    return value


def _wait_until_blocked(observer: Connection, *, blocked_pid: int, blocker_pid: int) -> None:
    assert blocked_pid != blocker_pid
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        blockers = observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": blocked_pid})
        if isinstance(blockers, list) and blocker_pid in blockers:
            return
        time.sleep(0.02)
    raise AssertionError(f"Backend {blocked_pid} did not wait for backend {blocker_pid}")


def _edit(address: str) -> EditAndApproveDecision:
    return EditAndApproveDecision(
        action="edit_and_approve", scope="metadata", expected_version=1, shop_version=1,
        shop=EditableShop(shop_name="銀座鮨はな", area="銀座", category="寿司・回転寿司", address=address),
    )


@pytest.mark.parametrize("same_mention", [True, False])
def test_postgresql_review_rechecks_versions_after_waiting_for_another_save(
    pg_engine: Engine, same_mention: bool,
) -> None:
    records = _seed(pg_engine)
    second_id = records.first_mention_id if same_mention else records.second_mention_id
    pids: Queue[int] = Queue()

    def second_save() -> str:
        with Session(pg_engine) as db:
            pids.put(_pid(db.connection()))
            try:
                apply_review_decision(db, second_id, _edit("古いフォームの入力"))
            except ReviewConflictError as exc:
                db.rollback()
                return exc.code
            return "saved"

    with pg_engine.connect() as first_connection:
        transaction = first_connection.begin()
        first_pid = _pid(first_connection)
        with Session(bind=first_connection, join_transaction_mode="rollback_only") as first:
            apply_review_decision(first, records.first_mention_id, _edit("先に保存した住所"))
            assert transaction.is_active
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(second_save)
                try:
                    _wait_until_blocked(first_connection, blocked_pid=pids.get(timeout=5), blocker_pid=first_pid)
                    transaction.commit()
                    assert future.result(timeout=20) == ("stale_mention" if same_mention else "stale_shop")
                finally:
                    if transaction.is_active:
                        transaction.rollback()
    with Session(pg_engine) as verified:
        shop = verified.get(Shop, records.shop_id)
        assert shop is not None and (shop.version, shop.address, shop.memo) == (2, "先に保存した住所", "保持するメモ")
        assert verified.get(ShopMention, records.first_mention_id).version == 2
        assert verified.query(ReviewEvent).count() == 1


def _csv(records: _Records, memo: str = "CSVの保存内容") -> bytes:
    return (
        "_id,shop.version,message_id,memo\n"
        f"{records.shop_id},1,{records.first_message_id},{memo}\n"
    ).encode("utf-8")


def test_postgresql_duplicate_csv_request_changes_data_and_version_once(pg_engine: Engine) -> None:
    records = _seed(pg_engine)
    csv = _csv(records)
    pids: Queue[int] = Queue()

    def replay() -> str:
        with Session(pg_engine) as db:
            pids.put(_pid(db.connection()))
            try:
                apply_csv_update(db, parse_csv_update(csv, "retry.csv"))
            except ImportValidationFailure:
                db.rollback()
                return "conflict"
            return "saved"

    with pg_engine.connect() as first_connection:
        transaction = first_connection.begin()
        first_pid = _pid(first_connection)
        with Session(bind=first_connection, join_transaction_mode="rollback_only") as first:
            apply_csv_update(first, parse_csv_update(csv, "first.csv"))
            assert transaction.is_active
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(replay)
                try:
                    _wait_until_blocked(first_connection, blocked_pid=pids.get(timeout=5), blocker_pid=first_pid)
                    transaction.commit()
                    assert future.result(timeout=20) == "conflict"
                finally:
                    if transaction.is_active:
                        transaction.rollback()
    with Session(pg_engine) as verified:
        shop = verified.get(Shop, records.shop_id)
        assert shop is not None and (shop.version, shop.memo) == (2, "CSVの保存内容")
        assert verified.query(ShopMention).count() == 2
        assert {mention.version for mention in verified.scalars(select(ShopMention))} == {1}
        assert verified.query(SourceAsset).one().url == "https://example.com/kept.pdf"
        assert verified.query(ResolutionCandidate).one().name == "保持する候補"
        assert verified.query(ReviewEvent).count() == 0


def test_postgresql_evaluation_preserves_review_committed_during_ai_wait(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _seed(pg_engine)
    extraction_started = Event()
    review_saved = Event()
    pids: Queue[int] = Queue()

    async def extract(_content: str) -> ExtractionCallResult:
        extraction_started.set()
        assert review_saved.wait(timeout=10)
        return ExtractionCallResult(
            ExtractedMessage(is_restaurant_message=True, mentions=[ExtractedMention(
                shop_name="銀座鮨はな", area="銀座", category="寿司・回転寿司",
                needs_review=False, confidence_reason="AIの古い評価結果",
            )]),
            ModelCallMetrics(model="test", input_tokens=1, output_tokens=1,
                             web_search_calls=0, image_count=0, latency_ms=1, estimated_cost_microusd=0),
        )

    def evaluate() -> evaluation_service.EvaluationResult:
        with Session(pg_engine) as db:
            pids.put(_pid(db.connection()))
            return asyncio.run(evaluation_service.evaluate_imported_message(db, records.first_message_id))

    monkeypatch.setattr(evaluation_service, "extract_restaurant_message", extract)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(evaluate)
        try:
            evaluation_pid = pids.get(timeout=5)
            assert extraction_started.wait(timeout=5)
            with Session(pg_engine) as reviewer:
                assert _pid(reviewer.connection()) != evaluation_pid
                apply_review_decision(reviewer, records.first_mention_id, DeferDecision(
                    action="defer", expected_version=1, shop_version=1, note="AI待機中の手動確認",
                ))
            review_saved.set()
            result = future.result(timeout=20)
        finally:
            review_saved.set()
    assert result.skipped and result.skip_reason == "concurrent_change"
    with Session(pg_engine) as verified:
        mention = verified.get(ShopMention, records.first_mention_id)
        assert mention is not None and (mention.version, mention.review_status) == (2, "deferred")
        assert mention.confidence_reason == "保存済み根拠"
        assert verified.query(ReviewEvent).one().note == "AI待機中の手動確認"
        assert verified.query(ResolutionCandidate).one().name == "保持する候補"
        assert verified.query(ProcessingRun).filter_by(stage="evaluation_extraction").count() == 1
        assert verified.query(ProcessingRun).filter_by(stage="evaluation_pipeline", status="ignored").count() == 1


@dataclass(frozen=True)
class _Outcome:
    status: str
    sqlstate: str | None = None


def _database_failure(error: DBAPIError) -> _Outcome:
    code = getattr(error.orig, "pgcode", None)
    return _Outcome("database_error", code if isinstance(code, str) else None)


def test_postgresql_csv_and_safe_merge_do_not_deadlock(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _seed(pg_engine, duplicate_shops=True)
    shops_locked = Event()
    release_merge = Event()
    merge_pids: Queue[int] = Queue()
    csv_pids: Queue[int] = Queue()
    original_lock = duplicate_service._lock_candidate_rows

    def pause_after_shop_locks(db: Session, shop_ids: tuple[int, ...]) -> tuple[Shop, ...]:
        shops = original_lock(db, shop_ids)
        shops_locked.set()
        assert release_merge.wait(timeout=10)
        return shops

    def merge() -> _Outcome:
        with Session(pg_engine) as db:
            merge_pids.put(_pid(db.connection()))
            try:
                result = duplicate_service.apply_safe_duplicate_merges(db)
                db.commit()
            except DBAPIError as exc:
                db.rollback()
                return _database_failure(exc)
            assert result.merged_shop_count == 1
            return _Outcome("saved")

    def import_csv() -> _Outcome:
        with Session(pg_engine) as db:
            csv_pids.put(_pid(db.connection()))
            try:
                apply_csv_update(db, parse_csv_update(_csv(records, "競合したCSV入力"), "update.csv"))
            except ImportValidationFailure:
                db.rollback()
                return _Outcome("conflict")
            except DBAPIError as exc:
                db.rollback()
                return _database_failure(exc)
            return _Outcome("saved")

    monkeypatch.setattr(duplicate_service, "_lock_candidate_rows", pause_after_shop_locks)
    with ThreadPoolExecutor(max_workers=2) as workers:
        merge_future = workers.submit(merge)
        try:
            merge_pid = merge_pids.get(timeout=5)
            assert shops_locked.wait(timeout=5)
            csv_future = workers.submit(import_csv)
            csv_pid = csv_pids.get(timeout=5)
            with pg_engine.connect() as observer:
                _wait_until_blocked(observer, blocked_pid=csv_pid, blocker_pid=merge_pid)
            release_merge.set()
            outcomes = (merge_future.result(timeout=20), csv_future.result(timeout=20))
        finally:
            release_merge.set()
    assert outcomes == (_Outcome("saved"), _Outcome("conflict")), outcomes
    with Session(pg_engine) as verified:
        keeper = verified.query(Shop).one()
        assert (keeper.id, keeper.version, keeper.memo) == (records.shop_id, 2, "保持するメモ")
        assert {mention.shop_id for mention in verified.scalars(select(ShopMention))} == {keeper.id}
        assert verified.get(ShopMention, records.first_mention_id).version == 1
        assert verified.get(ShopMention, records.second_mention_id).version == 2
        assert verified.query(ReviewEvent).one().action == "automatic_merge"
        redirect = verified.query(ShopRedirect).one()
        assert (redirect.source_shop_id, redirect.target_shop_id) == (records.second_shop_id, keeper.id)
        assert verified.query(SourceAsset).one().url == "https://example.com/kept.pdf"
