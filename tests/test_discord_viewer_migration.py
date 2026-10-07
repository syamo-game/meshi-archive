from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, String, create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import (
    Base,
    DiscordViewer,
    LoginAttempt,
    Message,
    ReviewEvent,
    Shop,
    ShopMention,
    SourceAsset,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREVIOUS_REVISION = "20260928_01"
VIEWER_REVISION = "20261005_01"
VIEWER_ID = "18446744073709551615"
FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)


@dataclass(frozen=True)
class MigrationDatabase:
    engine: Engine
    config: Config


@pytest.fixture
def database() -> Generator[MigrationDatabase, None, None]:
    path = PROJECT_ROOT.parent / f"discord-viewer-migration-{uuid4().hex}.db"
    database_url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(database_url)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    try:
        yield MigrationDatabase(engine=engine, config=config)
    finally:
        engine.dispose()
        path.unlink(missing_ok=True)


def _revision(database: MigrationDatabase) -> str:
    with database.engine.connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _domain_rows(engine: Engine) -> dict[str, tuple[tuple[object, ...], ...]]:
    with engine.connect() as connection:
        return {
            table.name: tuple(
                tuple(row)
                for row in connection.execute(select(table).order_by(*table.primary_key.columns))
            )
            for table in Base.metadata.sorted_tables
            if table.name not in {"discord_viewers", "admin_sessions", "discord_viewer_grant_events"}
        }


def _seed_domain(engine: Engine) -> None:
    with Session(engine) as session:
        message = Message(
            message_id="12345678901234567890",
            content="Synthetic original source",
            source_created_at=FIXED_TIME,
            processing_status="succeeded",
        )
        shop = Shop(
            shop_name="Synthetic existing shop",
            memo="Keep the user's note",
            is_visited=True,
            visited_at=FIXED_TIME,
            rating=4,
            image_key="a" * 64,
            canonical_url="https://example.invalid/shop",
            version=8,
        )
        mention = ShopMention(
            message=message,
            shop=shop,
            occurrence_index=0,
            extracted_name=shop.shop_name,
            source_url="https://example.invalid/post",
            review_status="approved",
            metadata_review_status="approved",
            resolution_status="resolved",
            version=5,
        )
        session.add_all([
            mention,
            SourceAsset(
                message=message,
                kind="attachment",
                url="https://example.invalid/attachment",
                extracted_text="Synthetic attachment evidence",
            ),
            ReviewEvent(mention=mention, action="approve", note="Existing history"),
            LoginAttempt(
                key="synthetic-login-key",
                failures=3,
                window_started=100,
                blocked_until=200,
            ),
        ])
        session.commit()


def _assert_viewer_schema(engine: Engine) -> None:
    columns = {column["name"]: column for column in inspect(engine).get_columns("discord_viewers")}
    assert {"discord_user_id", "created_at"} <= set(columns) <= {"discord_user_id", "created_at", "generation", "is_admin"}
    assert isinstance(columns["discord_user_id"]["type"], String)
    assert columns["discord_user_id"]["type"].length == 20
    assert columns["discord_user_id"]["nullable"] is False
    assert columns["created_at"]["nullable"] is False
    assert inspect(engine).get_pk_constraint("discord_viewers")["constrained_columns"] == [
        "discord_user_id"
    ]
    timestamp_type = DiscordViewer.__table__.c.created_at.type
    assert isinstance(timestamp_type, DateTime)
    assert timestamp_type.timezone is True


def test_fresh_migration_creates_only_the_two_viewer_fields(database: MigrationDatabase) -> None:
    command.upgrade(database.config, VIEWER_REVISION)
    _assert_viewer_schema(database.engine)
    assert _revision(database) == VIEWER_REVISION


def test_previous_revision_upgrade_preserves_domain_and_login_rows(database: MigrationDatabase) -> None:
    # The old deployed schema does not contain the table created by current metadata.
    Base.metadata.create_all(
        database.engine,
        tables=[
            table for table in Base.metadata.sorted_tables
            if table.name not in {"discord_viewers", "admin_sessions", "discord_viewer_grant_events"}
        ],
    )
    command.stamp(database.config, PREVIOUS_REVISION)
    _seed_domain(database.engine)
    before = _domain_rows(database.engine)
    assert "discord_viewers" not in inspect(database.engine).get_table_names()

    command.upgrade(database.config, VIEWER_REVISION)

    _assert_viewer_schema(database.engine)
    assert _domain_rows(database.engine) == before
    assert _revision(database) == VIEWER_REVISION
    with Session(database.engine) as session:
        assert session.execute(text("SELECT discord_user_id FROM discord_viewers")).all() == []


def test_upgrade_preserves_viewers_already_created_by_initial_metadata(database: MigrationDatabase) -> None:
    command.upgrade(database.config, PREVIOUS_REVISION)
    with Session(database.engine) as session:
        session.add(DiscordViewer(discord_user_id=VIEWER_ID, created_at=FIXED_TIME))
        session.commit()

    command.upgrade(database.config, VIEWER_REVISION)

    with Session(database.engine) as session:
        viewer = session.get(DiscordViewer, VIEWER_ID)
        assert viewer is not None
        assert viewer.created_at == FIXED_TIME.replace(tzinfo=None)
    assert _revision(database) == VIEWER_REVISION


def test_twenty_digit_viewer_id_round_trips_as_exact_text(database: MigrationDatabase) -> None:
    command.upgrade(database.config, VIEWER_REVISION)
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(database.engine) as session:
        session.add(DiscordViewer(discord_user_id=VIEWER_ID))
        session.commit()
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    with Session(database.engine) as session:
        viewer = session.get(DiscordViewer, VIEWER_ID)
        assert viewer is not None
        assert isinstance(viewer.discord_user_id, str)
        assert viewer.discord_user_id == VIEWER_ID
        assert before <= viewer.created_at <= after
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT typeof(discord_user_id) FROM discord_viewers")).scalar_one() == "text"


def test_duplicate_viewer_id_is_rejected_without_replacing_the_grant(database: MigrationDatabase) -> None:
    command.upgrade(database.config, VIEWER_REVISION)
    with Session(database.engine) as session:
        session.add(DiscordViewer(discord_user_id=VIEWER_ID, created_at=FIXED_TIME))
        session.commit()
    with Session(database.engine) as session:
        session.add(DiscordViewer(discord_user_id=VIEWER_ID))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        viewers = session.scalars(select(DiscordViewer)).all()
        assert len(viewers) == 1
        assert viewers[0].created_at == FIXED_TIME.replace(tzinfo=None)


def test_empty_viewer_downgrade_preserves_domain_and_login_rows(database: MigrationDatabase) -> None:
    command.upgrade(database.config, VIEWER_REVISION)
    _seed_domain(database.engine)
    before = _domain_rows(database.engine)

    command.downgrade(database.config, PREVIOUS_REVISION)

    assert "discord_viewers" not in inspect(database.engine).get_table_names()
    assert _revision(database) == PREVIOUS_REVISION
    assert _domain_rows(database.engine) == before
    command.upgrade(database.config, VIEWER_REVISION)
    _assert_viewer_schema(database.engine)
    assert _domain_rows(database.engine) == before


def test_populated_viewer_downgrade_refuses_and_preserves_all_rows(database: MigrationDatabase) -> None:
    command.upgrade(database.config, VIEWER_REVISION)
    _seed_domain(database.engine)
    with Session(database.engine) as session:
        session.add(DiscordViewer(discord_user_id=VIEWER_ID, created_at=FIXED_TIME))
        session.commit()
    before = _domain_rows(database.engine)

    with pytest.raises(RuntimeError, match="Cannot downgrade while Discord viewer grants exist"):
        command.downgrade(database.config, PREVIOUS_REVISION)

    assert _revision(database) == VIEWER_REVISION
    assert _domain_rows(database.engine) == before
    with Session(database.engine) as session:
        viewers = session.scalars(select(DiscordViewer)).all()
        assert [(viewer.discord_user_id, viewer.created_at) for viewer in viewers] == [
            (VIEWER_ID, FIXED_TIME.replace(tzinfo=None))
        ]
