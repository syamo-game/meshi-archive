from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, create_engine, inspect, select
from sqlalchemy.orm import Session

from bot.restaurant_extractor import ModelCallMetrics
from db.models import Base, LookupCache, Message, ProcessingRun, utc_now
from services.identification_pipeline import (
    _ACTIVE_OPERATIONAL_JOURNAL,
    _OperationalJournal,
    _cache_key,
    _delete_cache,
    _get_cache,
    _persist_operational_journal,
    _put_cache,
    _record_failure,
    _record_metrics,
)


MESSAGE_ID = "12345678901234567"
KIND = "url_metadata"
EXISTING = "https://example.com/existing"
REMOVED = "https://example.com/removed"
CREATED = "https://example.com/created"


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(database)
    with Session(database) as db:
        db.add(Message(message_id=MESSAGE_ID, content="Synthetic evaluation input"))
        for value in (EXISTING, REMOVED):
            db.add(LookupCache(
                cache_key=_cache_key(KIND, value), kind=KIND, payload="original",
                expires_at=utc_now() + timedelta(days=1),
            ))
        db.commit()
    try:
        yield database
    finally:
        database.dispose()


def metrics() -> ModelCallMetrics:
    return ModelCallMetrics(
        model="synthetic-model", input_tokens=12, output_tokens=4,
        web_search_calls=2, image_count=1, latency_ms=25,
        estimated_cost_microusd=10, api_attempts=2,
    )


def test_deferred_helpers_never_stage_orm_writes_or_flush(
    engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _OperationalJournal(defer_writes=True)
    with Session(engine) as db:
        original = db.get(LookupCache, _cache_key(KIND, EXISTING))
        removed = db.get(LookupCache, _cache_key(KIND, REMOVED))
        assert original is not None and removed is not None

        def forbidden_flush(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("Deferred preparation must not flush")

        with monkeypatch.context() as patched:
            patched.setattr(db, "flush", forbidden_flush)
            token = _ACTIVE_OPERATIONAL_JOURNAL.set(journal)
            try:
                assert _get_cache(db, KIND, EXISTING) is original
                _record_metrics(db, MESSAGE_ID, "evaluation_extraction", metrics())
                _record_failure(db, MESSAGE_ID, "evaluation_image", ValueError("synthetic failure"))
                _put_cache(db, KIND, EXISTING, "updated")
                _put_cache(db, KIND, CREATED, "created")
                _delete_cache(db, KIND, REMOVED, removed)
                staged = _get_cache(db, KIND, EXISTING)
                assert staged is not None and staged.payload == "updated"
                assert inspect(staged).transient
                assert _get_cache(db, KIND, REMOVED) is None
                assert not db.new and not db.dirty and not db.deleted
                assert original.payload == "original"
                assert removed.payload == "original"
            finally:
                _ACTIVE_OPERATIONAL_JOURNAL.reset(token)

        assert len(journal.runs) == 3
        assert len(journal.caches) == 2
        assert journal.invalidated_caches == {(KIND, REMOVED)}
        assert db.query(ProcessingRun).count() == 0
        assert db.query(LookupCache).count() == 2


def test_deferred_cache_overlay_handles_invalidation_and_replacement(engine: Engine) -> None:
    journal = _OperationalJournal(defer_writes=True)
    with Session(engine) as db:
        token = _ACTIVE_OPERATIONAL_JOURNAL.set(journal)
        try:
            original = _get_cache(db, KIND, EXISTING)
            assert original is not None
            _delete_cache(db, KIND, EXISTING, original)
            assert _get_cache(db, KIND, EXISTING) is None

            _put_cache(db, KIND, EXISTING, "replacement")
            replacement = _get_cache(db, KIND, EXISTING)
            assert replacement is not None and replacement.payload == "replacement"
            assert (KIND, EXISTING) in journal.invalidated_caches
            assert inspect(replacement).transient

            _delete_cache(db, KIND, EXISTING, replacement)
            assert _get_cache(db, KIND, EXISTING) is None
            assert (KIND, EXISTING) not in journal.caches
            _put_cache(db, KIND, EXISTING, "final replacement")
            final = _get_cache(db, KIND, EXISTING)
            assert final is not None and final.payload == "final replacement"
            assert not db.new and not db.dirty and not db.deleted
            assert original.payload == "original"
        finally:
            _ACTIVE_OPERATIONAL_JOURNAL.reset(token)


def test_deferred_cache_read_does_not_flush_unrelated_pending_work(engine: Engine) -> None:
    journal = _OperationalJournal(defer_writes=True)
    with Session(engine) as db:
        pending = Message(message_id="12345678901234568", content="Unrelated pending work")
        db.add(pending)
        token = _ACTIVE_OPERATIONAL_JOURNAL.set(journal)
        try:
            assert _get_cache(db, KIND, EXISTING) is not None
            assert _get_cache(db, KIND, CREATED) is None
            assert inspect(pending).pending
            assert db.connection().execute(
                select(Message.message_id).where(Message.message_id == pending.message_id)
            ).first() is None
        finally:
            _ACTIVE_OPERATIONAL_JOURNAL.reset(token)
            db.rollback()


def test_deferred_records_survive_rollback_and_replay_once(engine: Engine) -> None:
    journal = _OperationalJournal(defer_writes=True)
    with Session(engine) as preparing:
        token = _ACTIVE_OPERATIONAL_JOURNAL.set(journal)
        try:
            cached = _get_cache(preparing, KIND, EXISTING)
            removed = _get_cache(preparing, KIND, REMOVED)
            assert cached is not None and removed is not None
            _record_metrics(preparing, MESSAGE_ID, "evaluation_extraction", metrics())
            _record_failure(preparing, MESSAGE_ID, "evaluation_image", ValueError("synthetic failure"))
            _delete_cache(preparing, KIND, EXISTING, cached)
            _put_cache(preparing, KIND, EXISTING, "replacement")
            _put_cache(preparing, KIND, CREATED, "new cache")
            _delete_cache(preparing, KIND, REMOVED, removed)
            preparing.rollback()
        finally:
            _ACTIVE_OPERATIONAL_JOURNAL.reset(token)

    with Session(engine) as writer:
        assert writer.query(ProcessingRun).count() == 0
        _persist_operational_journal(writer, journal)
        writer.commit()

    with Session(engine) as verified:
        runs = verified.query(ProcessingRun).order_by(ProcessingRun.id).all()
        assert [run.stage for run in runs] == [
            "evaluation_extraction", "evaluation_extraction_retry_attempt", "evaluation_image",
        ]
        assert sum(run.estimated_cost_microusd for run in runs) == 10
        assert sum(run.input_tokens for run in runs) == 12
        assert sum(run.web_search_calls for run in runs) == 2
        assert runs[-1].error == "ValueError: synthetic failure"
        assert verified.get(LookupCache, _cache_key(KIND, EXISTING)).payload == "replacement"
        assert verified.get(LookupCache, _cache_key(KIND, CREATED)).payload == "new cache"
        assert verified.get(LookupCache, _cache_key(KIND, REMOVED)) is None
        assert verified.query(LookupCache).count() == 2
        assert len(journal.runs) == 3


@pytest.mark.parametrize("active_journal", [False, True])
def test_default_helpers_keep_normal_database_writes(engine: Engine, active_journal: bool) -> None:
    journal = _OperationalJournal()
    token = _ACTIVE_OPERATIONAL_JOURNAL.set(journal if active_journal else None)
    try:
        with Session(engine) as db:
            removed = _get_cache(db, KIND, REMOVED)
            assert removed is not None
            _record_metrics(db, MESSAGE_ID, "evaluation_extraction", metrics())
            _record_failure(db, MESSAGE_ID, "evaluation_image", ValueError("synthetic failure"))
            _put_cache(db, KIND, EXISTING, "updated normally")
            _put_cache(db, KIND, CREATED, "created normally")
            _delete_cache(db, KIND, REMOVED, removed)
            db.commit()
        with Session(engine) as verified:
            assert verified.query(ProcessingRun).count() == 3
            assert verified.get(LookupCache, _cache_key(KIND, EXISTING)).payload == "updated normally"
            assert verified.get(LookupCache, _cache_key(KIND, CREATED)).payload == "created normally"
            assert verified.get(LookupCache, _cache_key(KIND, REMOVED)) is None
        assert len(journal.runs) == (3 if active_journal else 0)
    finally:
        _ACTIVE_OPERATIONAL_JOURNAL.reset(token)
