from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal, Sequence
from uuid import UUID

from sqlalchemy import (
    Connection,
    Engine,
    Integer,
    Select,
    Table,
    create_engine,
    func,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.models import Base


BATCH_SIZE = 500


@dataclass(frozen=True)
class TableSummary:
    table: str
    rows: int
    sha256: str


@dataclass(frozen=True)
class TransferReport:
    status: str
    tables: tuple[TableSummary, ...]
    total_rows: int


class TransferFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class TransferArgs:
    mode: Literal["copy", "verify"]


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        normalized = value
        if value.tzinfo is not None:
            normalized = value.astimezone(timezone.utc).replace(tzinfo=None)
        return normalized.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _ordered_select(table: Table) -> Select[tuple[object, ...]]:
    primary_key = list(table.primary_key.columns)
    statement = select(table)
    return statement.order_by(*primary_key) if primary_key else statement


def summarize_table(connection: Connection, table: Table) -> TableSummary:
    digest = hashlib.sha256()
    row_count = 0
    column_names = [column.name for column in table.columns]
    for row in connection.execute(_ordered_select(table)).mappings():
        values = [_canonical_value(row[column_name]) for column_name in column_names]
        encoded = json.dumps(
            values,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
        row_count += 1
    return TableSummary(table=table.name, rows=row_count, sha256=digest.hexdigest())


def summarize_database(connection: Connection) -> tuple[TableSummary, ...]:
    return tuple(summarize_table(connection, table) for table in Base.metadata.sorted_tables)


def _validate_schema(engine: Engine, label: str) -> None:
    expected_tables = set(Base.metadata.tables)
    with engine.connect() as connection:
        existing_tables = set(inspect(connection).get_table_names())
    missing_tables = sorted(expected_tables - existing_tables)
    if missing_tables:
        raise TransferFailure(f"{label} database is missing tables: {', '.join(missing_tables)}")


def _validate_empty_target(connection: Connection) -> None:
    nonempty: list[str] = []
    for table in Base.metadata.sorted_tables:
        row_count = connection.execute(select(func.count()).select_from(table)).scalar_one()
        if row_count:
            nonempty.append(f"{table.name}={row_count}")
    if nonempty:
        raise TransferFailure(f"Target database is not empty: {', '.join(nonempty)}")


def _copy_table(source: Connection, target: Connection, table: Table) -> None:
    rows: list[dict[str, object]] = []
    for row in source.execute(_ordered_select(table)).mappings():
        rows.append(dict(row))
        if len(rows) >= BATCH_SIZE:
            target.execute(insert(table), rows)
            rows.clear()
    if rows:
        target.execute(insert(table), rows)


def _synchronize_postgresql_sequences(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    for table in Base.metadata.sorted_tables:
        for column in table.primary_key.columns:
            if not isinstance(column.type, Integer):
                continue
            sequence_name = connection.execute(
                text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                {"table_name": table.name, "column_name": column.name},
            ).scalar_one_or_none()
            if sequence_name is None:
                continue
            maximum = connection.execute(select(func.max(column))).scalar_one_or_none()
            if maximum is None:
                connection.execute(
                    text("SELECT setval(CAST(:sequence_name AS regclass), 1, FALSE)"),
                    {"sequence_name": sequence_name},
                )
            else:
                connection.execute(
                    text("SELECT setval(CAST(:sequence_name AS regclass), :value, TRUE)"),
                    {"sequence_name": sequence_name, "value": int(maximum)},
                )


def verify_databases(source_engine: Engine, target_engine: Engine) -> TransferReport:
    with source_engine.connect() as source, target_engine.connect() as target:
        source_summaries = summarize_database(source)
        target_summaries = summarize_database(target)
    _verify_summaries(source_summaries, target_summaries)
    return TransferReport(
        status="verified",
        tables=source_summaries,
        total_rows=sum(summary.rows for summary in source_summaries),
    )


def _verify_summaries(
    source_summaries: tuple[TableSummary, ...],
    target_summaries: tuple[TableSummary, ...],
) -> None:
    if source_summaries != target_summaries:
        source_by_table = {summary.table: summary for summary in source_summaries}
        target_by_table = {summary.table: summary for summary in target_summaries}
        differences = [
            table_name
            for table_name in sorted(source_by_table)
            if source_by_table[table_name] != target_by_table.get(table_name)
        ]
        raise TransferFailure(f"Database verification failed for tables: {', '.join(differences)}")


def transfer_database(source_engine: Engine, target_engine: Engine) -> TransferReport:
    _validate_schema(source_engine, "Source")
    _validate_schema(target_engine, "Target")
    with source_engine.connect() as source:
        source_transaction = source.begin()
        try:
            source_summaries = summarize_database(source)
            with target_engine.begin() as target:
                _validate_empty_target(target)
                for table in Base.metadata.sorted_tables:
                    _copy_table(source, target, table)
                _synchronize_postgresql_sequences(target)
                target_summaries = summarize_database(target)
                _verify_summaries(source_summaries, target_summaries)
        finally:
            source_transaction.rollback()
    return TransferReport(
        status="transferred",
        tables=source_summaries,
        total_rows=sum(summary.rows for summary in source_summaries),
    )


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise TransferFailure(f"Required environment variable is missing: {name}")
    return value


def _validate_urls(source_url: str, target_url: str, confirmation_url: str) -> None:
    if target_url != confirmation_url:
        raise TransferFailure("CONFIRM_TARGET_DATABASE_URL must exactly match TARGET_DATABASE_URL")
    if source_url == target_url:
        raise TransferFailure("Source and target database URLs must be different")
    source = make_url(source_url)
    target = make_url(target_url)
    if source.get_backend_name() != "sqlite":
        raise TransferFailure("SOURCE_DATABASE_URL must use SQLite")
    if target.get_backend_name() != "postgresql":
        raise TransferFailure("TARGET_DATABASE_URL must use PostgreSQL")


def parse_args(argv: Sequence[str] | None = None) -> TransferArgs:
    parser = argparse.ArgumentParser(
        description="Copy and verify all Meshi v2 application tables between databases."
    )
    parser.add_argument("mode", choices=("copy", "verify"))
    parsed = parser.parse_args(argv)
    return TransferArgs(mode=parsed.mode)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    source_url = _required_environment("SOURCE_DATABASE_URL")
    target_url = _required_environment("TARGET_DATABASE_URL")
    confirmation_url = _required_environment("CONFIRM_TARGET_DATABASE_URL")
    _validate_urls(source_url, target_url, confirmation_url)

    source_engine = create_engine(source_url)
    target_engine = create_engine(target_url)
    try:
        report = (
            transfer_database(source_engine, target_engine)
            if args.mode == "copy"
            else verify_databases(source_engine, target_engine)
        )
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    finally:
        source_engine.dispose()
        target_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
