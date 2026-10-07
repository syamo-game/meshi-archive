from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, get_ident

import pytest
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session

from db.models import Base, Message, ReviewEvent, Shop, ShopMention, ShopRedirect, SourceAsset
from services.duplicate_service import (
    SafeDuplicateMergeResult, _lock_candidate_rows, _merge_safe_group,
    apply_safe_duplicate_merges, build_safe_duplicate_merge_plan,
)
from services.import_service import apply_csv_update, parse_csv_update
from services.merge_history import MergeHistorySnapshot


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_engine(
        f"sqlite:///{(tmp_path / 'duplicate-concurrency.sqlite').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(database)
    with Session(database) as db:
        for shop_id in (1, 2):
            db.add(Shop(
                id=shop_id, shop_name="合成の鮨店", area="銀座", category="寿司・回転寿司",
                address="東京都中央区銀座1-2-3", phone="0312345678",
            ))
        for mention_id, shop_id in ((1, 1), (2, 2), (3, 2)):
            db.add(ShopMention(
                id=mention_id, shop_id=shop_id, occurrence_index=0,
                message=Message(message_id=f"3234567890123{mention_id:04d}", content="保持する本文"),
                extracted_name="合成の鮨店", extracted_area="銀座", extracted_category="寿司・回転寿司",
                source_url=f"https://example.com/posts/{mention_id}", review_status="approved",
                metadata_review_status="approved", resolution_status="resolved", resolution_method="manual",
            ))
        db.flush()
        db.add(SourceAsset(message_id="32345678901230002", kind="attachment", url="https://example.com/kept.png"))
        db.add(ReviewEvent(mention_id=2, action="approve_current", note="以前の履歴"))
        db.commit()
    yield database
    database.dispose()


def _csv_memo(version: int, value: str) -> bytes:
    return (
        "_id,shop.version,message_id,memo\n"
        f"1,{version},32345678901230001,{value}\n"
    ).encode("utf-8")


def test_stale_child_is_refreshed_before_moving_and_snapshotting(engine: Engine) -> None:
    with Session(engine) as merger:
        stale = merger.get(ShopMention, 2)
        assert stale is not None and stale.version == 1
        with Session(engine) as other:
            mention = other.get(ShopMention, 2)
            assert mention is not None
            mention.version = 5
            mention.review_status = "deferred"
            mention.metadata_review_status = "pending"
            other.add(ReviewEvent(mention_id=2, action="defer", note="統合直前の履歴"))
            other.commit()
        assert stale.version == 1
        result = apply_safe_duplicate_merges(merger)
        merger.commit()
        assert result.merged_shop_count == 1
    with Session(engine) as verified:
        moved = verified.get(ShopMention, 2)
        assert (moved.shop_id, moved.version, moved.review_status, moved.metadata_review_status) == (1, 6, "deferred", "pending")
        audit = verified.query(ReviewEvent).filter_by(mention_id=2, action="automatic_merge").one()
        saved = MergeHistorySnapshot.model_validate_json(audit.note)
        old = next(item for item in saved.moved_mentions if item.id == 2)
        assert (old.shop_id, old.version, old.review_status, old.metadata_review_status) == (2, 5, "deferred", "pending")
        assert {item.note for item in verified.query(ReviewEvent).filter(ReviewEvent.action != "automatic_merge")} == {"以前の履歴", "統合直前の履歴"}
        assert verified.query(SourceAsset).one().url == "https://example.com/kept.png"
        assert moved.source_url == "https://example.com/posts/2"
        assert moved.message.content == "保持する本文"


def test_merge_waits_for_csv_commit_before_validating_and_capturing_history(engine: Engine) -> None:
    merge_write_waiting = Event()
    merge_thread_id: int | None = None
    merge_future: Future[SafeDuplicateMergeResult] | None = None

    def observe_merge_write(
        connection: Connection, cursor: object, statement: str,
        parameters: object, context: object, executemany: bool,
    ) -> None:
        if get_ident() == merge_thread_id and statement.lstrip().upper().startswith("UPDATE "):
            merge_write_waiting.set()

    with Session(engine) as merger, Session(engine) as csv_session:
        cached = merger.get(Shop, 1)
        assert cached is not None and cached.version == 1
        apply_csv_update(csv_session, parse_csv_update(_csv_memo(1, "CSVの1回目"), "first.csv"))
        assert cached.version == 1

        def merge_in_other_session() -> SafeDuplicateMergeResult:
            nonlocal merge_thread_id
            merge_thread_id = get_ident()
            result = apply_safe_duplicate_merges(merger)
            merger.commit()
            return result

        with ThreadPoolExecutor(max_workers=1) as workers:
            def start_merge_before_csv_commit(session: Session) -> None:
                nonlocal merge_future
                merge_future = workers.submit(merge_in_other_session)
                assert merge_write_waiting.wait(timeout=10)

            event.listen(engine, "before_cursor_execute", observe_merge_write)
            event.listen(csv_session, "before_commit", start_merge_before_csv_commit)
            try:
                apply_csv_update(csv_session, parse_csv_update(_csv_memo(2, "CSVの最新保存"), "second.csv"))
                assert merge_future is not None
                assert merge_future.result(timeout=10).merged_shop_count == 1
            finally:
                event.remove(engine, "before_cursor_execute", observe_merge_write)
                event.remove(csv_session, "before_commit", start_merge_before_csv_commit)
    with Session(engine) as verified:
        keeper = verified.get(Shop, 1)
        assert (keeper.version, keeper.memo) == (4, "CSVの最新保存")
        audit = verified.query(ReviewEvent).filter_by(mention_id=2, action="automatic_merge").one()
        before_keeper = MergeHistorySnapshot.model_validate_json(audit.note).shops[0]
        assert (before_keeper.version, before_keeper.memo) == (3, "CSVの最新保存")


def test_two_sessions_merging_same_group_record_one_merge_only(engine: Engine) -> None:
    ready = Barrier(2, timeout=10)

    def merge_once() -> int:
        with Session(engine) as db:
            ready.wait()
            result = apply_safe_duplicate_merges(db)
            db.commit()
            return result.merged_shop_count

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(merge_once)
        second = workers.submit(merge_once)
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert sorted(results) == [0, 1]
    with Session(engine) as verified:
        assert verified.query(Shop).count() == 1
        assert verified.get(Shop, 1).version == 2
        assert verified.query(ShopRedirect).count() == 1
        assert verified.query(ReviewEvent).filter_by(action="automatic_merge").count() == 2
        assert [item.version for item in verified.query(ShopMention).order_by(ShopMention.id)] == [1, 2, 2]
        snapshots = [MergeHistorySnapshot.model_validate_json(item.note) for item in verified.query(ReviewEvent).filter_by(action="automatic_merge")]
        assert all({shop.id for shop in snapshot.shops} == {1, 2} for snapshot in snapshots)


def test_group_detached_after_plan_is_skipped_without_losing_shop_or_snapshot(engine: Engine) -> None:
    with Session(engine) as merger:
        group = build_safe_duplicate_merge_plan(merger).groups[0]
        with Session(engine) as other:
            for mention in other.query(ShopMention).filter_by(shop_id=2):
                mention.shop_id = None
                mention.review_status = "rejected"
                mention.version += 1
            other.commit()
        shops = _lock_candidate_rows(merger, group.shop_ids)
        result = _merge_safe_group(merger, group, {shop.id: shop for shop in shops})
        merger.commit()
    assert result.action == "skipped"
    assert result.reason == "approved_mentions_changed"
    with Session(engine) as verified:
        assert verified.query(Shop).count() == 2
        assert verified.query(ShopRedirect).count() == 0
        assert verified.query(ReviewEvent).filter_by(action="automatic_merge").count() == 0
        assert verified.get(ShopMention, 2).shop_id is None
        assert verified.get(ShopMention, 3).shop_id is None
