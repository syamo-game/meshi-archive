from __future__ import annotations

import logging
import os
from pathlib import Path
from sqlite3 import Connection as SQLiteConnection

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


logger = logging.getLogger(__name__)
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./meshi.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args: dict[str, bool] = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)


if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(
        connection: SQLiteConnection,
        _record: object,
    ) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
    return config


def run_migrations() -> None:
    try:
        command.upgrade(_alembic_config(), "head")
    except Exception as exc:
        raise RuntimeError(
            f"Database migration failed: url_scheme={DATABASE_URL.split(':', 1)[0]}, "
            f"error={type(exc).__name__}: {exc}"
        ) from exc


def verify_migrations() -> None:
    config = _alembic_config()
    script = ScriptDirectory.from_config(config)
    expected = script.get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != expected:
        raise RuntimeError(
            "Database schema is not current: "
            f"current={current or 'none'}, expected={expected}. Run 'alembic upgrade head'."
        )


def init_db() -> None:
    default_auto_migrate = "true" if engine.dialect.name == "sqlite" else "false"
    auto_migrate = os.getenv("AUTO_MIGRATE", default_auto_migrate).lower() == "true"
    if auto_migrate:
        run_migrations()
    else:
        verify_migrations()
