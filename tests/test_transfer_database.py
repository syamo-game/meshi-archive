from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from db.models import Base, Message, ReviewEvent, Shop, ShopMention, SourceAsset
from scripts.transfer_database import TransferFailure, transfer_database, verify_databases


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _build_source(path: Path) -> None:
    engine = create_engine(_database_url(path))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        message = Message(
            message_id="12345678901234567",
            processing_status="succeeded",
            source_created_at=datetime(2026, 1, 2, 3, 4, 5),
        )
        shop = Shop(
            id=42,
            shop_name="移送テスト店",
            area="東京",
            category="割烹",
            is_visited=True,
            created_at=datetime(2026, 1, 2, 3, 4, 5),
            updated_at=datetime(2026, 1, 2, 3, 4, 5),
        )
        session.add_all((message, shop))
        session.flush()
        mention = ShopMention(
            id=77,
            message_id=message.message_id,
            shop_id=shop.id,
            occurrence_index=0,
            extracted_name=shop.shop_name,
            resolution_status="resolved",
            review_status="approved",
            resolution_method="manual",
            extraction_source="legacy_import",
            reviewed_at=datetime(2026, 1, 3, 4, 5, 6),
            created_at=datetime(2026, 1, 2, 3, 4, 5),
        )
        session.add(mention)
        session.flush()
        session.add_all(
            (
                SourceAsset(
                    id=88,
                    message_id=message.message_id,
                    kind="link",
                    url="https://example.com/source",
                    fetch_status="pending",
                    created_at=datetime(2026, 1, 2, 3, 4, 5),
                ),
                ReviewEvent(
                    id=99,
                    mention_id=mention.id,
                    action="approve_current",
                    selected_shop_id=shop.id,
                    created_at=datetime(2026, 1, 3, 4, 5, 6),
                ),
            )
        )
        session.commit()
    engine.dispose()


def _build_empty_target(path: Path) -> None:
    engine = create_engine(_database_url(path))
    Base.metadata.create_all(engine)
    engine.dispose()


def test_transfer_preserves_rows_and_identifiers(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    _build_source(source_path)
    _build_empty_target(target_path)
    source_engine = create_engine(_database_url(source_path))
    target_engine = create_engine(_database_url(target_path))
    try:
        report = transfer_database(source_engine, target_engine)
        assert report.status == "transferred"
        assert {summary.table: summary.rows for summary in report.tables} == {
            "login_attempts": 0,
            "import_batches": 0,
            "import_rows": 0,
            "lookup_cache": 0,
            "messages": 1,
            "processing_runs": 0,
            "resolution_candidates": 0,
            "review_events": 1,
            "shop_redirects": 0,
            "shops": 1,
            "shop_mentions": 1,
            "source_assets": 1,
            "sync_states": 0,
        }
        with Session(target_engine) as session:
            assert session.scalar(select(Shop.id)) == 42
            assert session.scalar(select(ShopMention.id)) == 77
            assert session.scalar(select(SourceAsset.id)) == 88
            assert session.scalar(select(ReviewEvent.id)) == 99
        assert verify_databases(source_engine, target_engine).status == "verified"
    finally:
        source_engine.dispose()
        target_engine.dispose()


def test_transfer_rejects_nonempty_target(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    _build_source(source_path)
    _build_source(target_path)
    source_engine = create_engine(_database_url(source_path))
    target_engine = create_engine(_database_url(target_path))
    try:
        with pytest.raises(TransferFailure, match="Target database is not empty"):
            transfer_database(source_engine, target_engine)
    finally:
        source_engine.dispose()
        target_engine.dispose()


def test_transfer_rolls_back_when_verification_fails(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    _build_source(source_path)
    _build_empty_target(target_path)
    source_engine = create_engine(_database_url(source_path))
    target_engine = create_engine(_database_url(target_path))
    try:
        with target_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TRIGGER alter_shop_after_insert AFTER INSERT ON shops "
                    "BEGIN UPDATE shops SET shop_name='改変済み' WHERE id=NEW.id; END"
                )
            )
        with pytest.raises(TransferFailure, match="Database verification failed"):
            transfer_database(source_engine, target_engine)
        with Session(target_engine) as session:
            assert session.scalar(select(func.count()).select_from(Shop)) == 0
    finally:
        source_engine.dispose()
        target_engine.dispose()
