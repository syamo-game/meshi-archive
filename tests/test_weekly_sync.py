from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Generator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import discord
import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from bot import sync_logic
from bot.sync_lock import SyncAlreadyRunning, lock_channel_sync, sync_lock_key
from bot.weekly_sync import WeeklySyncSettings, next_weekly_run, weekly_slot
from db.models import Base, Message, SyncState
from tests.test_migrations import (
    TEST_POSTGRES_URL, ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST,
    alembic_config_for_url, assert_safe_postgres_test_url,
)


CHANNEL_ID = "90000000000000001"
FIRST_ID = "90000000000000002"
SECOND_ID = "90000000000000003"


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(sync_logic, "SessionLocal", factory)
    monkeypatch.setenv("APP_READ_ONLY", "false")
    yield factory
    engine.dispose()


class HistoryChannel:
    def __init__(self) -> None:
        self.id: int = int(CHANNEL_ID)
        self.messages: list[discord.Message] = []
        self.requested_after: list[int | None] = []
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def history(self, *, limit: int, after: discord.Object | None, oldest_first: bool) -> AsyncIterator[discord.Message]:
        assert oldest_first
        self.requested_after.append(after.id if after else None)
        if self.started is not None and self.release is not None:
            self.started.set()
            await self.release.wait()
        eligible = [message for message in self.messages if after is None or message.id > after.id]
        for message in eligible[:limit]:
            yield message


def client() -> discord.Client:
    return cast(discord.Client, SimpleNamespace(user=object()))


def message(message_id: str, content: str = "") -> discord.Message:
    return cast(discord.Message, SimpleNamespace(
        id=int(message_id), content=content, author=object(), embeds=[], attachments=[],
    ))


def test_weekly_schedule_uses_monday_0400_japan_time() -> None:
    before = datetime(2026, 10, 11, 18, 59, tzinfo=timezone.utc)
    at = datetime(2026, 10, 11, 19, tzinfo=timezone.utc)
    assert weekly_slot(before) == datetime(2026, 10, 4, 19, tzinfo=timezone.utc)
    assert weekly_slot(at) == at
    assert next_weekly_run(before).isoformat() == "2026-10-12T04:00:00+09:00"
    assert next_weekly_run(at).isoformat() == "2026-10-19T04:00:00+09:00"
    with pytest.raises(ValueError, match="timezone"):
        weekly_slot(datetime(2026, 10, 12))


@pytest.mark.parametrize("value", ["", "anything", "1"])
def test_weekly_settings_reject_invalid_flags(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("WEEKLY_SYNC_ENABLED", value)
    with pytest.raises(RuntimeError, match="WEEKLY_SYNC_ENABLED"):
        WeeklySyncSettings.from_env()


def test_weekly_settings_require_a_target_without_enabling_existing_installations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEEKLY_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
    assert WeeklySyncSettings.from_env() == WeeklySyncSettings(False, None)
    with pytest.raises(RuntimeError, match="DISCORD_CHANNEL_ID"):
        WeeklySyncSettings.from_env(require_channel=True)
    monkeypatch.setenv("WEEKLY_SYNC_ENABLED", "true")
    with pytest.raises(RuntimeError, match="DISCORD_CHANNEL_ID"):
        WeeklySyncSettings.from_env()
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    assert WeeklySyncSettings.from_env() == WeeklySyncSettings(True, int(CHANNEL_ID))


def test_sync_resumes_saved_cursor_and_preserves_registered_messages(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        db.add(SyncState(channel_id=CHANNEL_ID, last_contiguous_message_id=FIRST_ID))
        db.add(Message(message_id=SECOND_ID, channel_id=CHANNEL_ID, content="preserve", processing_status="succeeded"))
        db.commit()
    channel = HistoryChannel()
    channel.messages = [message(FIRST_ID), message(SECOND_ID)]
    result = asyncio.run(sync_logic.sync_channel_history(client(), cast(sync_logic.HistoryChannel, channel)))
    assert result is not None and result.fetched == 1 and result.skipped == 1 and result.processed == 0
    assert channel.requested_after == [int(FIRST_ID)]
    with factory() as db:
        assert db.get(SyncState, CHANNEL_ID).last_contiguous_message_id == SECOND_ID
        assert db.get(Message, SECOND_ID).content == "preserve"


def test_weekly_attempt_is_persistent_and_initial_run_can_be_forced(factory: sessionmaker[Session]) -> None:
    started = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
    channel = HistoryChannel()
    channel.messages = [message(FIRST_ID)]
    attempt = sync_logic.WeeklyAttempt(weekly_slot(started), started)
    result = asyncio.run(sync_logic.sync_channel_history(client(), cast(sync_logic.HistoryChannel, channel), weekly=attempt))
    assert result is not None and result.skipped == 1
    with factory() as db:
        assert db.get(SyncState, CHANNEL_ID).last_weekly_sync_started_at is not None
        assert db.get(SyncState, CHANNEL_ID).last_weekly_sync_completed_at is not None
    assert asyncio.run(sync_logic.sync_channel_history(client(), cast(sync_logic.HistoryChannel, channel), weekly=attempt)) is None
    assert len(channel.requested_after) == 1
    forced = sync_logic.WeeklyAttempt(weekly_slot(started), started, True)
    assert asyncio.run(sync_logic.sync_channel_history(client(), cast(sync_logic.HistoryChannel, channel), weekly=forced)) is not None
    assert len(channel.requested_after) == 2


def test_sync_failure_keeps_cursor_and_weekly_success_unset(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_process(db: Session, envelope: sync_logic.MessageEnvelope, *, allow_source_discovery: bool) -> None:
        raise RuntimeError("synthetic processing failure")
    monkeypatch.setattr(sync_logic, "process_message", fail_process)
    channel = HistoryChannel()
    channel.messages = [message(FIRST_ID), message(SECOND_ID, "needs AI")]
    started = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
    with pytest.raises(sync_logic.HistorySyncError) as error:
        asyncio.run(sync_logic.sync_channel_history(client(), cast(sync_logic.HistoryChannel, channel), weekly=sync_logic.WeeklyAttempt(weekly_slot(started), started)))
    assert error.value.message_id == SECOND_ID
    with factory() as db:
        state = db.get(SyncState, CHANNEL_ID)
        assert state.last_contiguous_message_id == FIRST_ID
        assert state.last_weekly_sync_started_at is not None
        assert state.last_weekly_sync_completed_at is None


def test_manual_and_weekly_sync_cannot_overlap(factory: sessionmaker[Session]) -> None:
    async def scenario() -> None:
        channel = HistoryChannel()
        channel.started = asyncio.Event()
        channel.release = asyncio.Event()
        first = asyncio.create_task(sync_logic.sync_channel_history(client(), cast(sync_logic.HistoryChannel, channel)))
        await channel.started.wait()
        with pytest.raises(SyncAlreadyRunning):
            await sync_logic.sync_channel_history(client(), cast(sync_logic.HistoryChannel, channel))
        channel.release.set()
        assert await first is not None
        assert await sync_logic.sync_channel_history(client(), cast(sync_logic.HistoryChannel, channel)) is not None
    asyncio.run(scenario())


def test_read_only_mode_prevents_cursor_changes(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "true")
    with pytest.raises(RuntimeError, match="APP_READ_ONLY"):
        asyncio.run(sync_logic.sync_channel_history(client(), cast(sync_logic.HistoryChannel, HistoryChannel())))
    with factory() as db:
        assert db.query(SyncState).count() == 0


def test_sync_preserves_batch_limit(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_logic, "SYNC_BATCH_LIMIT", 1)
    channel = HistoryChannel()
    channel.messages = [message(FIRST_ID), message(SECOND_ID)]
    result = asyncio.run(sync_logic.sync_channel_history(client(), cast(sync_logic.HistoryChannel, channel)))
    assert result is not None and result.fetched == 1 and result.limit_reached
    with factory() as db:
        assert db.get(SyncState, CHANNEL_ID).last_contiguous_message_id == FIRST_ID


def test_weekly_migration_preserves_existing_channel_cursor(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'weekly-migration.db').as_posix()}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sync_states (channel_id VARCHAR(20) PRIMARY KEY, last_contiguous_message_id VARCHAR(20), updated_at DATETIME NOT NULL)"))
        connection.execute(text("INSERT INTO sync_states VALUES (:channel, :cursor, '2026-10-07 10:00:00')"), {"channel": CHANNEL_ID, "cursor": FIRST_ID})
    config = alembic_config_for_url(url)
    command.stamp(config, "20261006_02")
    command.upgrade(config, "head")
    columns = {column["name"] for column in inspect(engine).get_columns("sync_states")}
    assert {"last_weekly_sync_started_at", "last_weekly_sync_completed_at"}.issubset(columns)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT last_contiguous_message_id FROM sync_states")) == FIRST_ID
        assert connection.scalar(text("SELECT last_weekly_sync_started_at FROM sync_states")) is None
    engine.dispose()


@pytest.mark.skipif(
    TEST_POSTGRES_URL is None or not ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST,
    reason="An explicitly isolated PostgreSQL test database is required.",
)
def test_postgres_channel_lock_excludes_other_process_connections() -> None:
    assert TEST_POSTGRES_URL is not None
    assert_safe_postgres_test_url(TEST_POSTGRES_URL)
    engine = create_engine(TEST_POSTGRES_URL)
    try:
        with Session(engine) as first, Session(engine) as second:
            with lock_channel_sync(first, CHANNEL_ID):
                key = sync_lock_key(CHANNEL_ID)
                assert second.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}) is False
                second.commit()
            assert second.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}) is True
            assert second.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key}) is True
            second.commit()
    finally:
        engine.dispose()
