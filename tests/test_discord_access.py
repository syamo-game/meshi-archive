"""Use synthetic IDs and isolated SQLite databases, never the application database."""

from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier, Lock, get_ident

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import DiscordViewer, DiscordViewerGrantEvent
from services.admin_sessions import AdminActor
from services.discord_access import (
    AddDiscordViewerResult,
    DiscordUserIdError,
    add_discord_viewer,
    configured_discord_ids,
    configured_web_admin_ids,
    is_discord_viewer_allowed,
    list_discord_viewers,
    normalize_discord_user_id,
)


ADMIN_ID = "12345678901234567"
CONFIGURED_ID = "22345678901234567"
DATABASE_ID = "32345678901234567"
SHARED_ID = "42345678901234567"
ACTOR = AdminActor("discord", ADMIN_ID, "a" * 64)


@pytest.fixture(autouse=True)
def isolated_web_admin_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_ADMIN_USER_IDS", raising=False)


@pytest.fixture
def engine(tmp_path: Path) -> Generator[Engine, None, None]:
    database = tmp_path / "discord-viewer-test.db"
    value = create_engine(
        f"sqlite:///{database.as_posix()}",
        connect_args={"timeout": 15, "check_same_thread": False},
    )
    DiscordViewer.__table__.create(value)
    DiscordViewerGrantEvent.__table__.create(value)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def db(engine: Engine) -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("10000000000000000", "10000000000000000"),
        ("9999999999999999999", "9999999999999999999"),
        ("10000000000000000000", "10000000000000000000"),
        ("18446744073709551615", "18446744073709551615"),
        (" \t12345678901234567\r\n", ADMIN_ID),
        ("\u300012345678901234567\u3000", ADMIN_ID),
    ),
)
def test_normalize_preserves_exact_valid_id_and_only_trims_outer_whitespace(
    raw: str, expected: str,
) -> None:
    result = normalize_discord_user_id(raw)
    assert result == expected
    assert isinstance(result, str)


@pytest.mark.parametrize(
    "value",
    (
        "", " \t\r\n", "1" * 16, "1" * 21,
        "01234567890123456", "0" * 20,
        "18446744073709551616", "99999999999999999999",
        "+12345678901234567", "-12345678901234567",
        "12345678 901234567", "12345678\t901234567", "12345678\n901234567",
        "１２３４５６７８９０１２３４５６７", "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦٧",
        "1.2345678901234567e16", "12345678901234567.0",
        "<@12345678901234567>", "<@!12345678901234567>",
        "https://discord.com/users/12345678901234567",
        "12345678901234567,22345678901234567", "synthetic-user#1234",
        "12345678901234567\x00", "12345678901234567\u200b",
    ),
)
def test_normalize_rejects_inexact_non_ascii_or_out_of_range_ids(value: str) -> None:
    with pytest.raises(DiscordUserIdError):
        normalize_discord_user_id(value)


@pytest.mark.parametrize("admin_id", (None, "", ADMIN_ID))
def test_configured_ids_combine_trimmed_environment_and_admin_without_duplicates(
    monkeypatch: pytest.MonkeyPatch, admin_id: str | None,
) -> None:
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", f" {CONFIGURED_ID},,\n{SHARED_ID}\t,{CONFIGURED_ID}, ")
    expected = {CONFIGURED_ID, SHARED_ID}
    if admin_id:
        expected.add(admin_id)
    assert configured_discord_ids(admin_id) == expected


def test_missing_environment_keeps_only_the_configured_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_ALLOWED_USER_IDS", raising=False)
    assert configured_discord_ids(None) == set()
    assert configured_discord_ids(ADMIN_ID) == {ADMIN_ID}


def test_web_admins_join_legacy_admin_and_viewers_without_promoting_viewers(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_ADMIN_USER_IDS", f" {SHARED_ID},,{ADMIN_ID},{SHARED_ID}, ")
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", CONFIGURED_ID)
    db.add(DiscordViewer(discord_user_id=DATABASE_ID))
    db.commit()

    assert configured_web_admin_ids(ADMIN_ID) == {ADMIN_ID, SHARED_ID}
    configured = configured_discord_ids(ADMIN_ID)
    assert configured == {ADMIN_ID, SHARED_ID, CONFIGURED_ID}
    entries = list_discord_viewers(db, configured, ADMIN_ID)
    assert {entry.user_id for entry in entries if entry.is_admin} == {ADMIN_ID, SHARED_ID}
    assert {entry.user_id for entry in entries if not entry.is_admin} == {CONFIGURED_ID, DATABASE_ID}


@pytest.mark.parametrize("legacy_id", (None, "", ADMIN_ID))
def test_empty_web_admin_configuration_preserves_legacy_admin(
    monkeypatch: pytest.MonkeyPatch, legacy_id: str | None,
) -> None:
    monkeypatch.setenv("WEB_ADMIN_USER_IDS", " , , \t")
    assert configured_web_admin_ids(legacy_id) == ({legacy_id} if legacy_id else set())


@pytest.mark.parametrize("invalid_id", (
    "not-a-discord-id", "123", "01234567890123456", "18446744073709551616",
    "<@12345678901234567>", "12345678 901234567",
))
def test_invalid_web_admin_entry_rejects_entire_configuration(
    monkeypatch: pytest.MonkeyPatch, invalid_id: str,
) -> None:
    monkeypatch.setenv("WEB_ADMIN_USER_IDS", f"{SHARED_ID},{invalid_id}")
    with pytest.raises(RuntimeError, match="WEB_ADMIN_USER_IDS"):
        configured_web_admin_ids(ADMIN_ID)
    with pytest.raises(RuntimeError, match="WEB_ADMIN_USER_IDS"):
        configured_discord_ids(ADMIN_ID)


def test_twenty_digit_neighbors_survive_commits_and_new_sessions_exactly(engine: Engine) -> None:
    identifiers = ("18446744073709551614", "18446744073709551615")
    with Session(engine) as writer:
        for identifier in identifiers:
            result = add_discord_viewer(writer, identifier, set(), ACTOR)
            assert result.user_id == identifier and result.created is True
    with Session(engine) as reader:
        rows = list(reader.scalars(select(DiscordViewer)))
        assert {row.discord_user_id for row in rows} == set(identifiers)
        assert all(isinstance(row.discord_user_id, str) for row in rows)
        assert all(isinstance(row.created_at, datetime) for row in rows)
        assert all(is_discord_viewer_allowed(reader, identifier, set()) for identifier in identifiers)
        events = list(reader.scalars(select(DiscordViewerGrantEvent)))
        assert {event.discord_user_id for event in events} == set(identifiers)
        assert all(event.actor_discord_user_id == ADMIN_ID for event in events)


def test_invalid_id_is_rejected_before_configured_access_can_short_circuit(db: Session) -> None:
    invalid = "<@12345678901234567>"
    with pytest.raises(DiscordUserIdError):
        add_discord_viewer(db, invalid, {invalid}, ACTOR)
    assert db.scalar(select(func.count()).select_from(DiscordViewer)) == 0


@pytest.mark.parametrize("identifier", (ADMIN_ID, CONFIGURED_ID))
def test_adding_existing_configuration_does_not_create_database_records(
    db: Session, identifier: str,
) -> None:
    result = add_discord_viewer(db, f" \t{identifier}\n", {ADMIN_ID, CONFIGURED_ID}, ACTOR)
    assert result.user_id == identifier
    assert result.created is False
    assert db.scalar(select(func.count()).select_from(DiscordViewer)) == 0


def test_repeated_addition_keeps_one_record_and_original_creation_time(engine: Engine) -> None:
    with Session(engine) as first:
        assert add_discord_viewer(first, DATABASE_ID, set(), ACTOR).created is True
        row = first.get(DiscordViewer, DATABASE_ID)
        assert row is not None
        created_at = row.created_at
    with Session(engine) as second:
        result = add_discord_viewer(second, f" {DATABASE_ID} ", set(), ACTOR)
        assert result.user_id == DATABASE_ID and result.created is False
        row = second.get(DiscordViewer, DATABASE_ID)
        assert row is not None and row.created_at == created_at
        assert second.scalar(select(func.count()).select_from(DiscordViewer)) == 1
        assert second.scalar(select(func.count()).select_from(DiscordViewerGrantEvent)) == 1


def test_access_is_configuration_or_database_membership_only(db: Session) -> None:
    db.add(DiscordViewer(discord_user_id=DATABASE_ID))
    db.commit()
    assert is_discord_viewer_allowed(db, CONFIGURED_ID, {CONFIGURED_ID}) is True
    assert is_discord_viewer_allowed(db, DATABASE_ID, {CONFIGURED_ID}) is True
    assert is_discord_viewer_allowed(db, SHARED_ID, {CONFIGURED_ID}) is False


def test_list_unions_sources_and_only_the_configured_admin_has_admin_role(db: Session) -> None:
    for identifier in (DATABASE_ID, SHARED_ID, ADMIN_ID):
        db.add(DiscordViewer(discord_user_id=identifier))
    db.commit()
    configured = {ADMIN_ID, CONFIGURED_ID, SHARED_ID}

    entries = list_discord_viewers(db, configured, ADMIN_ID)
    assert len(entries) == 4
    by_id = {entry.user_id: entry for entry in entries}
    assert set(by_id) == {ADMIN_ID, CONFIGURED_ID, DATABASE_ID, SHARED_ID}
    assert {entry.user_id for entry in entries if entry.is_admin} == {ADMIN_ID}
    for identifier in configured:
        assert by_id[identifier].source == "configuration"
        assert by_id[identifier].created_at is None
    assert by_id[DATABASE_ID].source == "database"
    assert isinstance(by_id[DATABASE_ID].created_at, datetime)
    assert db.scalar(select(func.count()).select_from(DiscordViewer)) == 3


def test_list_never_promotes_viewers_when_admin_id_is_absent(db: Session) -> None:
    db.add(DiscordViewer(discord_user_id=DATABASE_ID))
    db.commit()
    entries = list_discord_viewers(db, {CONFIGURED_ID}, None)
    assert {entry.user_id for entry in entries} == {CONFIGURED_ID, DATABASE_ID}
    assert all(entry.is_admin is False for entry in entries)


def test_unexpected_database_integrity_failure_is_not_treated_as_a_duplicate(db: Session) -> None:
    db.execute(text(
        "CREATE TRIGGER synthetic_discord_rejection BEFORE INSERT ON discord_viewers "
        f"WHEN NEW.discord_user_id = '{DATABASE_ID}' "
        "BEGIN SELECT RAISE(ABORT, 'synthetic storage rejection'); END"
    ))
    db.commit()
    with pytest.raises(IntegrityError):
        add_discord_viewer(db, DATABASE_ID, set(), ACTOR)
    assert db.get(DiscordViewer, DATABASE_ID) is None
    assert add_discord_viewer(db, SHARED_ID, set(), ACTOR).created is True


def test_audit_insert_failure_rolls_back_the_viewer_grant(db: Session) -> None:
    db.execute(text(
        "CREATE TRIGGER synthetic_audit_rejection BEFORE INSERT ON discord_viewer_grant_events "
        "BEGIN SELECT RAISE(ABORT, 'synthetic audit rejection'); END"
    ))
    db.commit()
    with pytest.raises(IntegrityError, match="synthetic audit rejection"):
        add_discord_viewer(db, DATABASE_ID, set(), ACTOR)
    db.rollback()
    assert db.get(DiscordViewer, DATABASE_ID) is None
    assert db.scalar(select(func.count()).select_from(DiscordViewerGrantEvent)) == 0


def test_simultaneous_additions_resolve_the_real_primary_key_race(engine: Engine) -> None:
    readers = Barrier(2, timeout=10)
    lock = Lock()
    synchronized: set[int] = set()
    insert_attempts: list[int] = []

    def synchronize_empty_reads(
        connection: Connection, cursor: object, statement: str, parameters: object,
        context: object, executemany: bool,
    ) -> None:
        if not statement.lstrip().upper().startswith("SELECT") or "discord_viewers" not in statement:
            return
        identifier = get_ident()
        with lock:
            if identifier in synchronized:
                return
            synchronized.add(identifier)
        # Both real lookups must finish before either writer inserts the same key.
        readers.wait()

    def record_insert_attempts(
        connection: Connection, cursor: object, statement: str, parameters: object,
        context: object, executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("INSERT") and "discord_viewers" in statement:
            with lock:
                insert_attempts.append(get_ident())

    def add_in_separate_session() -> AddDiscordViewerResult:
        with Session(engine) as session:
            return add_discord_viewer(session, DATABASE_ID, set(), ACTOR)

    event.listen(engine, "after_cursor_execute", synchronize_empty_reads)
    event.listen(engine, "before_cursor_execute", record_insert_attempts)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(add_in_separate_session) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_empty_reads)
        event.remove(engine, "before_cursor_execute", record_insert_attempts)
    assert len(insert_attempts) == 2
    assert {result.user_id for result in results} == {DATABASE_ID}
    assert sum(result.created for result in results) == 1
    with Session(engine) as observer:
        assert observer.scalar(select(func.count()).select_from(DiscordViewer)) == 1
        assert observer.scalar(select(func.count()).select_from(DiscordViewerGrantEvent)) == 1
        assert observer.get(DiscordViewer, DATABASE_ID) is not None
