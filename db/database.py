import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Load environment variables for local testing (fallback outside docker)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./meshi.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False} # Needed for SQLite with multiple threads (e.g., Streamlit / asyncio)

engine = create_engine(
    DATABASE_URL, 
    connect_args=connect_args
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def _migrate_add_columns() -> None:
    """Idempotently add new columns to existing tables.

    Uses IF NOT EXISTS for PostgreSQL (safe to re-run).
    Falls back to try/except for SQLite (no IF NOT EXISTS before 3.37).
    """
    from sqlalchemy import text

    dialect = engine.dialect.name  # 'sqlite' or 'postgresql'

    if dialect == "postgresql":
        # PostgreSQL supports IF NOT EXISTS — no exception handling needed
        migrations = [
            "ALTER TABLE shops ADD COLUMN IF NOT EXISTS visited_at TIMESTAMPTZ",
            "ALTER TABLE shops ADD COLUMN IF NOT EXISTS rating INTEGER",
            "ALTER TABLE shops ADD COLUMN IF NOT EXISTS memo TEXT",
        ]
        with engine.connect() as conn:
            for sql in migrations:
                conn.execute(text(sql))
            conn.commit()
    else:
        # SQLite: try/except because IF NOT EXISTS is not reliably available
        migrations = [
            "ALTER TABLE shops ADD COLUMN visited_at DATETIME",
            "ALTER TABLE shops ADD COLUMN rating INTEGER",
            "ALTER TABLE shops ADD COLUMN memo TEXT",
        ]
        with engine.connect() as conn:
            for sql in migrations:
                try:
                    conn.execute(text(sql))
                    conn.commit()
                except Exception:
                    pass  # Column already exists — ignore


def init_db():
    from db.models import Base
    Base.metadata.create_all(bind=engine)
    _migrate_add_columns()

