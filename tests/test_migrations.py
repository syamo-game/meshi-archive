from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from services.shop_creation_lock import lock_new_shop_creation


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST = (
    os.getenv("ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST") == "1"
)


def alembic_config_for_url(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


def alembic_config(database_path: Path) -> Config:
    return alembic_config_for_url(f"sqlite:///{database_path.as_posix()}")


def assert_safe_postgres_test_url(database_url: str) -> None:
    parsed = make_url(database_url)
    database_name = parsed.database or ""
    if not parsed.drivername.startswith("postgresql"):
        raise RuntimeError("PostgreSQL migration tests require a PostgreSQL URL.")
    if not (database_name.startswith("test_") or database_name.endswith("_test")):
        raise RuntimeError(
            "PostgreSQL migration tests require a database named test_* or *_test."
        )


def test_fresh_sqlite_migration_round_trip() -> None:
    database_path = PROJECT_ROOT / f"migration-fresh-{uuid.uuid4().hex}.db"
    try:
        config = alembic_config(database_path)
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        assert {
            "messages",
            "source_assets",
            "shops",
            "shop_mentions",
            "resolution_candidates",
            "sync_states",
            "processing_runs",
            "lookup_cache",
            "review_events",
            "shop_redirects",
            "import_batches",
            "import_rows",
        }.issubset(inspect(engine).get_table_names())
        assert {
            "address",
            "phone",
            "external_source",
            "external_id",
            "branch_name",
            "review_status",
            "metadata_review_status",
            "image_key",
        }.issubset(
            {column["name"] for column in inspect(engine).get_columns("import_rows")}
        )
        engine.dispose()

        command.downgrade(config, "base")
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        assert "shop_mentions" in inspect(engine).get_table_names()
        engine.dispose()
    finally:
        database_path.unlink(missing_ok=True)


def test_grant_audit_downgrade_requires_explicit_archive_and_cleanup(tmp_path: Path) -> None:
    database_path = tmp_path / "grant-audit-migration.db"
    config = alembic_config(database_path)
    command.upgrade(config, "20261005_01")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO discord_viewers (discord_user_id, generation, created_at) "
            "VALUES ('123456789012345678', '0123456789abcdef0123456789abcdef', CURRENT_TIMESTAMP)"
        ))
    command.upgrade(config, "head")
    assert {"admin_sessions", "discord_viewer_grant_events"}.issubset(inspect(engine).get_table_names())
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM discord_viewers")) == 1
        connection.execute(text(
            "INSERT INTO discord_viewer_grant_events "
            "(discord_user_id, actor_method, actor_session_hash, created_at) "
            "VALUES ('123456789012345678', 'password', :session_hash, CURRENT_TIMESTAMP)"
        ), {"session_hash": "a" * 64})
    with pytest.raises(RuntimeError, match="Export grant audit events"):
        command.downgrade(config, "20261005_01")
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM discord_viewer_grant_events")) == 1
        connection.execute(text("DELETE FROM discord_viewer_grant_events"))
    command.downgrade(config, "20261005_01")
    assert "discord_viewer_grant_events" not in inspect(engine).get_table_names()
    with pytest.raises(RuntimeError, match="Discord viewer grants exist"):
        command.downgrade(config, "20260928_01")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM discord_viewers"))
    command.downgrade(config, "20260928_01")
    engine.dispose()


def test_shop_image_key_migration_preserves_existing_data() -> None:
    database_path = PROJECT_ROOT / f"migration-shop-image-{uuid.uuid4().hex}.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    try:
        config = alembic_config(database_path)
        command.upgrade(config, "head")
        command.downgrade(config, "20260830_01")
        assert "image_key" not in {
            column["name"] for column in inspect(engine).get_columns("shops")
        }
        assert "image_key" not in {
            column["name"] for column in inspect(engine).get_columns("import_rows")
        }
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO shops "
                    "(id, shop_name, is_visited, created_at, updated_at, version) "
                    "VALUES (1, 'Existing shop', 0, '2020-01-02 03:04:05.123456', "
                    "'2021-01-02 03:04:05.654321', 3)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO import_batches "
                    "(id, filename, sha256, status, row_count, message_count, "
                    "review_count, created_at) VALUES "
                    "('previous', 'previous.csv', 'abc', 'validated', 1, 1, 0, "
                    "'2026-01-01 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO import_rows "
                    "(batch_id, row_number, shop_id, message_id, created_at, shop_name, "
                    "is_visited, needs_review, resolution_status, review_status, "
                    "metadata_review_status, extraction_source) VALUES "
                    "('previous', 2, 1, '12345678901234567', "
                    "'2020-01-02 03:04:05.123456', 'Existing shop', 0, 0, "
                    "'resolved', 'approved', 'approved', 'legacy_import')"
                )
            )
            shop_before = tuple(connection.execute(text("SELECT * FROM shops")).one())
            import_before = tuple(connection.execute(text("SELECT * FROM import_rows")).one())
        engine.dispose()

        command.upgrade(config, "head")
        for table_name in ("shops", "import_rows"):
            image_column = next(
                column
                for column in inspect(engine).get_columns(table_name)
                if column["name"] == "image_key"
            )
            assert image_column["nullable"] is True
            assert image_column["type"].length == 64
        image_index = next(
            index
            for index in inspect(engine).get_indexes("shops")
            if index["name"] == "ix_shops_image_key"
        )
        assert image_index["column_names"] == ["image_key"]
        assert image_index["unique"] is False or image_index["unique"] == 0
        with engine.begin() as connection:
            assert tuple(connection.execute(text("SELECT * FROM shops")).one()) == (
                *shop_before, None
            )
            assert tuple(connection.execute(text("SELECT * FROM import_rows")).one()) == (
                *import_before, None
            )
            connection.execute(
                text("UPDATE shops SET image_key = :image_key WHERE id = 1"),
                {"image_key": "a" * 64},
            )
            connection.execute(
                text("UPDATE import_rows SET image_key = :image_key WHERE shop_id = 1"),
                {"image_key": "b" * 64},
            )
        engine.dispose()

        command.downgrade(config, "20260830_01")
        with engine.connect() as connection:
            assert tuple(connection.execute(text("SELECT * FROM shops")).one()) == shop_before
            assert tuple(connection.execute(text("SELECT * FROM import_rows")).one()) == import_before
        assert "ix_shops_image_key" not in {
            index["name"] for index in inspect(engine).get_indexes("shops")
        }
    finally:
        engine.dispose()
        database_path.unlink(missing_ok=True)


def test_downgrade_refuses_to_discard_merge_redirects() -> None:
    database_path = PROJECT_ROOT / f"migration-redirect-{uuid.uuid4().hex}.db"
    try:
        config = alembic_config(database_path)
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO shops "
                    "(id, shop_name, is_visited, created_at, updated_at, version) "
                    "VALUES (2, 'Target', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO shop_redirects "
                    "(source_shop_id, target_shop_id, reason, created_at) "
                    "VALUES (1, 2, 'merge', CURRENT_TIMESTAMP)"
                )
            )
        engine.dispose()

        with pytest.raises(RuntimeError, match="Cannot downgrade while shop redirects exist"):
            command.downgrade(config, "20260713_01")
    finally:
        database_path.unlink(missing_ok=True)


def test_populated_previous_revision_backfills_import_review_state() -> None:
    database_path = PROJECT_ROOT / f"migration-import-rows-{uuid.uuid4().hex}.db"
    try:
        database_url = f"sqlite:///{database_path.as_posix()}"
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE messages ("
                    "message_id VARCHAR PRIMARY KEY, is_target BOOLEAN NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE shops ("
                    "id INTEGER PRIMARY KEY, message_id VARCHAR NOT NULL, "
                    "shop_name VARCHAR NOT NULL, area VARCHAR, category VARCHAR, url TEXT, "
                    "is_visited BOOLEAN NOT NULL, created_at DATETIME NOT NULL)"
                )
            )
        engine.dispose()

        config = alembic_config(database_path)
        command.upgrade(config, "20260713_01")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO import_batches "
                        "(id, filename, sha256, status, row_count, message_count, "
                        "review_count, created_at) VALUES "
                        "('legacy', 'legacy.csv', 'abc', 'validated', 2, 2, 1, "
                        "'2026-01-01 00:00:00')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO import_rows "
                        "(batch_id, row_number, shop_id, message_id, created_at, shop_name, "
                        "is_visited, needs_review, extraction_source) VALUES "
                        "('legacy', 2, 1, '12345678901234567', "
                        "'2026-01-02 03:04:05', 'Review shop', 0, 1, 'legacy_import'), "
                        "('legacy', 3, 2, '12345678901234568', "
                        "'2026-01-03 04:05:06', 'Approved shop', 0, 0, 'legacy_import')"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(config, "head")
        engine = create_engine(database_url)
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT shop_id, resolution_status, review_status, resolution_method, "
                    "difference_type, reviewed_at IS NULL, metadata_review_status, "
                    "metadata_reviewed_at = created_at "
                    "FROM import_rows ORDER BY shop_id"
                )
            ).all()
        engine.dispose()

        assert rows == [
            (1, "ambiguous", "pending", None, "legacy_review", 1, "approved", 1),
            (2, "resolved", "approved", "manual", None, 0, "approved", 1),
        ]
    finally:
        database_path.unlink(missing_ok=True)


def test_source_identity_migration_backfills_existing_source_assets() -> None:
    database_path = PROJECT_ROOT / f"migration-source-identity-{uuid.uuid4().hex}.db"
    try:
        database_url = f"sqlite:///{database_path.as_posix()}"
        config = alembic_config(database_path)
        command.upgrade(config, "20260805_01")
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO messages "
                    "(message_id, is_target, processing_status) VALUES "
                    "('12345678901234567', 1, 'succeeded')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO source_assets "
                    "(message_id, kind, url, title, description, fetch_status, created_at, "
                    "source_service, source_item_id, normalized_url, content_fingerprint) "
                    "VALUES "
                    "('12345678901234567', 'link', "
                    "'https://twitter.com/food/status/1924813456236278183?s=20', "
                    "'Lunch', 'Good shop', 'available', CURRENT_TIMESTAMP, NULL, NULL, NULL, NULL)"
                )
            )
        engine.dispose()

        command.upgrade(config, "head")
        engine = create_engine(database_url)
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT source_service, source_item_id, normalized_url, "
                    "length(content_fingerprint) FROM source_assets"
                )
            ).one()
        source_asset_indexes = {
            index["name"] for index in inspect(engine).get_indexes("source_assets")
        }
        mention_indexes = {
            index["name"] for index in inspect(engine).get_indexes("shop_mentions")
        }
        engine.dispose()

        assert row == (
            "x",
            "1924813456236278183",
            "https://x.com/i/status/1924813456236278183",
            64,
        )
        assert "ix_source_assets_source_identity" in source_asset_indexes
        assert "ix_source_assets_normalized_url" in source_asset_indexes
        assert "ix_source_assets_content_fingerprint" in source_asset_indexes
        assert "ix_shop_mentions_reused_from_mention_id" in mention_indexes
        assert "ix_shop_mentions_resolution_basis" in mention_indexes
    finally:
        database_path.unlink(missing_ok=True)


@pytest.mark.skipif(
    TEST_POSTGRES_URL is None or not ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST,
    reason=(
        "TEST_POSTGRES_URL and ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST=1 are "
        "required for the PostgreSQL migration test."
    ),
)
def test_fresh_postgresql_migration_round_trip() -> None:
    if TEST_POSTGRES_URL is None:
        raise RuntimeError("TEST_POSTGRES_URL is required for this test.")
    assert_safe_postgres_test_url(TEST_POSTGRES_URL)

    config = alembic_config_for_url(TEST_POSTGRES_URL)
    command.upgrade(config, "head")
    engine = create_engine(TEST_POSTGRES_URL)
    assert "shop_mentions" in inspect(engine).get_table_names()
    engine.dispose()

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(TEST_POSTGRES_URL)
    assert "shop_mentions" in inspect(engine).get_table_names()
    engine.dispose()


@pytest.mark.skipif(
    TEST_POSTGRES_URL is None or not ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST,
    reason=(
        "TEST_POSTGRES_URL and ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST=1 are "
        "required for the PostgreSQL shop identity lock test."
    ),
)
def test_postgresql_shop_identity_lock_executes() -> None:
    if TEST_POSTGRES_URL is None:
        raise RuntimeError("TEST_POSTGRES_URL is required for this test.")
    assert_safe_postgres_test_url(TEST_POSTGRES_URL)
    command.upgrade(alembic_config_for_url(TEST_POSTGRES_URL), "head")
    engine = create_engine(TEST_POSTGRES_URL)
    try:
        with Session(engine) as first, Session(engine) as second:
            lock_new_shop_creation(first)
            second.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(OperationalError):
                lock_new_shop_creation(second)
            second.rollback()
            first.rollback()
            with second.begin():
                lock_new_shop_creation(second)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite:///meshi_archive_test.db",
        "postgresql+psycopg2://user:pass@localhost/meshi_archive",
    ],
)
def test_postgres_migration_guard_rejects_unsafe_database(database_url: str) -> None:
    with pytest.raises(RuntimeError):
        assert_safe_postgres_test_url(database_url)


def test_postgres_migration_guard_accepts_test_database() -> None:
    assert_safe_postgres_test_url(
        "postgresql+psycopg2://user:pass@localhost/meshi_archive_test"
    )


def test_legacy_sqlite_data_survives_upgrade() -> None:
    database_path = PROJECT_ROOT / f"migration-legacy-{uuid.uuid4().hex}.db"
    try:
        database_url = f"sqlite:///{database_path.as_posix()}"
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE messages ("
                    "message_id VARCHAR PRIMARY KEY, is_target BOOLEAN NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE shops ("
                    "id INTEGER PRIMARY KEY, message_id VARCHAR NOT NULL, shop_name VARCHAR NOT NULL, "
                    "area VARCHAR, category VARCHAR, url TEXT, is_visited BOOLEAN NOT NULL, "
                    "created_at DATETIME NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO messages (message_id, is_target) "
                    "VALUES ('12345678901234567', 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO shops "
                    "(id, message_id, shop_name, area, category, url, is_visited, created_at) "
                    "VALUES (42, '12345678901234567', '割烹みやび', '銀座', '割烹', "
                    "'https://example.com/post', 0, '2026-01-02 03:04:05')"
                )
            )
        engine.dispose()

        config = alembic_config(database_path)
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        with engine.connect() as connection:
            mention = connection.execute(
                text(
                    "SELECT message_id, shop_id, extracted_name, source_url, review_status "
                    "FROM shop_mentions"
                )
            ).one()
            assert mention == (
                "12345678901234567",
                42,
                "割烹みやび",
                "https://example.com/post",
                "approved",
            )
            shop = connection.execute(text("SELECT id, shop_name FROM shops")).one()
            assert shop == (42, "割烹みやび")
        engine.dispose()
    finally:
        database_path.unlink(missing_ok=True)


def test_discord_only_migration_backfills_old_grants_and_preserves_change_audit(tmp_path: Path) -> None:
    database_path = tmp_path / "discord-only-old-schema.db"
    config = alembic_config(database_path)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE discord_viewers (discord_user_id VARCHAR(20) PRIMARY KEY, created_at DATETIME NOT NULL)"))
        connection.execute(text(
            "CREATE TABLE discord_viewer_grant_events (id INTEGER PRIMARY KEY, discord_user_id VARCHAR(20) NOT NULL, "
            "actor_method VARCHAR(16) NOT NULL, actor_discord_user_id VARCHAR(20), actor_session_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)"
        ))
        for user_id in ("11111111111111111", "22222222222222222"):
            connection.execute(text("INSERT INTO discord_viewers VALUES (:user_id, '2026-01-02 03:04:05')"), {"user_id": user_id})
        connection.execute(text(
            "INSERT INTO discord_viewer_grant_events VALUES (1, '11111111111111111', 'password', NULL, :session_hash, '2026-01-02 03:04:05')"
        ), {"session_hash": "a" * 64})
    command.stamp(config, "20261005_02")
    command.upgrade(config, "head")
    with engine.begin() as connection:
        rows = connection.execute(text("SELECT discord_user_id, generation, created_at FROM discord_viewers ORDER BY discord_user_id")).all()
        assert [row[0] for row in rows] == ["11111111111111111", "22222222222222222"]
        assert all(len(row[1]) == 32 and row[2] == "2026-01-02 03:04:05" for row in rows)
        assert rows[0][1] != rows[1][1]
        event = connection.execute(text("SELECT action, replacement_user_id, actor_method FROM discord_viewer_grant_events")).one()
        assert tuple(event) == ("grant", None, "password")
        connection.execute(text("UPDATE discord_viewer_grant_events SET action='revoke' WHERE id=1"))
    with pytest.raises(RuntimeError, match="preserve access change audit"):
        command.downgrade(config, "20261005_02")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT action FROM discord_viewer_grant_events WHERE id=1")) == "revoke"
        assert connection.scalar(text("SELECT COUNT(*) FROM discord_viewers")) == 2
    engine.dispose()


def test_role_migration_preserves_old_viewers_and_refuses_to_drop_role_audit(tmp_path: Path) -> None:
    path = tmp_path / "old-discord-roles.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    config = alembic_config(path)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE discord_viewers (discord_user_id VARCHAR(20) PRIMARY KEY, generation VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL)"))
        connection.execute(text("CREATE TABLE discord_viewer_grant_events (id INTEGER PRIMARY KEY, discord_user_id VARCHAR(20) NOT NULL, actor_method VARCHAR(16) NOT NULL, actor_discord_user_id VARCHAR(20), actor_session_hash VARCHAR(64) NOT NULL, action VARCHAR(16) NOT NULL, replacement_user_id VARCHAR(20), created_at DATETIME NOT NULL)"))
        connection.execute(text("INSERT INTO discord_viewers VALUES ('11111111111111111', :generation, '2026-01-02 03:04:05')"), {"generation": "a" * 32})
    command.stamp(config, "20261006_01")
    command.upgrade(config, "head")
    with engine.begin() as connection:
        assert tuple(connection.execute(text("SELECT discord_user_id, generation, is_admin, created_at FROM discord_viewers")).one()) == ("11111111111111111", "a" * 32, 0, "2026-01-02 03:04:05")
        connection.execute(text("UPDATE discord_viewers SET is_admin=1"))
    with pytest.raises(RuntimeError, match="Restore administrator access"):
        command.downgrade(config, "20261006_01")
    with engine.begin() as connection:
        connection.execute(text("UPDATE discord_viewers SET is_admin=0"))
        connection.execute(text("INSERT INTO discord_viewer_grant_events (discord_user_id, actor_method, actor_session_hash, action, previous_role, new_role, created_at) VALUES ('11111111111111111', 'discord', :hash, 'update', 'admin', 'viewer', CURRENT_TIMESTAMP)"), {"hash": "b" * 64})
    with pytest.raises(RuntimeError, match="preserve access change audit"):
        command.downgrade(config, "20261006_01")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT previous_role FROM discord_viewer_grant_events")) == "admin"
    engine.dispose()
