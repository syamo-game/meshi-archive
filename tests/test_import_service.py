from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import StreamingResponse

from db.models import Base, ImportBatch, ImportRow, Message, Shop, ShopMention, SourceAsset
from services.import_service import (
    CsvImportRow,
    ImportValidationFailure,
    apply_import_batch,
    extract_external_identity,
    parse_csv_bytes,
    stage_import,
)
from web.routers.home import export_csv


BASE_COLUMNS = [
    "_id",
    "@timestamp",
    "message_id",
    "shop.name",
    "shop.area",
    "shop.category",
    "status.is_visited",
    "visited_at",
    "rating",
    "memo",
    "url",
    "needs_review",
]


def csv_bytes(rows: list[dict[str, str]], columns: list[str] | None = None) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns or BASE_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def row(message_id: str = "12345678901234567") -> dict[str, str]:
    return {
        "_id": "1",
        "@timestamp": "2026-01-02 03:04:05",
        "message_id": message_id,
        "shop.name": "銀座 鮨はな",
        "shop.area": "銀座",
        "shop.category": "寿司・回転寿司",
        "status.is_visited": "false",
        "visited_at": "",
        "rating": "",
        "memo": "",
        "url": "",
        "needs_review": "false",
    }


@pytest.mark.parametrize(
    "invalid_id",
    ["1.2345678901234567E+16", "12345678901234567.0", "", " 12345678901234567"],
)
def test_invalid_message_ids_are_rejected(invalid_id: str) -> None:
    with pytest.raises(ImportValidationFailure):
        parse_csv_bytes(csv_bytes([row(invalid_id)]), "test.csv")


@pytest.mark.parametrize("valid_id", ["12345678901234567", "12345678901234567890"])
def test_exact_message_ids_are_preserved(valid_id: str) -> None:
    parsed = parse_csv_bytes(csv_bytes([row(valid_id)]), "test.csv")
    assert parsed.rows[0].message_id == valid_id


@pytest.mark.parametrize(
    "invalid_key",
    [
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        " " + "a" * 64,
        "a" * 64 + "\n",
        "../" + "a" * 64,
        "a" * 64 + ".webp",
        "https://example.com/image.webp",
    ],
)
def test_invalid_uploaded_image_keys_are_rejected(invalid_key: str) -> None:
    source = {**row(), "shop.image_key": invalid_key}
    with pytest.raises(ImportValidationFailure, match="image_key"):
        parse_csv_bytes(csv_bytes([source], BASE_COLUMNS + ["shop.image_key"]), "test.csv")
    with pytest.raises(ValueError, match="image_key"):
        Shop(shop_name="店舗", image_key=invalid_key)
    with pytest.raises(ValueError, match="image_key"):
        ImportRow(image_key=invalid_key)


@pytest.mark.parametrize("invalid_key", [b"a" * 64, 123, False])
def test_uploaded_image_keys_do_not_coerce_non_strings(invalid_key: object) -> None:
    parsed = parse_csv_bytes(csv_bytes([row()]), "test.csv").rows[0]
    with pytest.raises(ValueError, match="image_key"):
        CsvImportRow.model_validate({**parsed.model_dump(), "image_key": invalid_key})
    with pytest.raises(ValueError, match="image_key"):
        Shop(shop_name="店舗", image_key=invalid_key)
    with pytest.raises(ValueError, match="image_key"):
        ImportRow(image_key=invalid_key)


@pytest.mark.parametrize("include_column", [False, True])
def test_csv_without_image_key_does_not_reuse_an_existing_key(include_column: bool) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    source = row()
    columns = BASE_COLUMNS.copy()
    if include_column:
        columns.append("shop.image_key")
        source["shop.image_key"] = ""
    parsed = parse_csv_bytes(csv_bytes([source], columns), "legacy.csv")
    assert parsed.rows[0].image_key is None
    try:
        with sessionmaker(bind=engine)() as db:
            db.add(Shop(id=1, shop_name=source["shop.name"], image_key="a" * 64))
            db.commit()
            preview = stage_import(db, parsed)
            staged_row = db.query(ImportRow).filter_by(batch_id=preview.batch_id).one()
            assert staged_row.image_key is None
            apply_import_batch(db, preview.batch_id)
            restored = db.get(Shop, 1)
            assert restored is not None
            assert restored.image_key is None
    finally:
        engine.dispose()


def test_naive_iso_datetime_is_treated_as_utc() -> None:
    source = row()
    source["@timestamp"] = "2026-01-02T03:04:05.123456"

    imported = parse_csv_bytes(csv_bytes([source]), "test.csv").rows[0]

    assert imported.created_at == datetime(
        2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc
    )


def test_shared_legacy_url_does_not_merge_or_become_canonical() -> None:
    shared = "https://tabelog.com/tokyo/A1301/A130101/13000001/"
    first = row("12345678901234567")
    second = row("12345678901234568")
    first["url"] = shared
    second.update({"_id": "2", "shop.name": "別の店", "url": shared})
    parsed = parse_csv_bytes(csv_bytes([first, second]), "test.csv")
    assert [item.shop_id for item in parsed.rows] == [1, 2]
    assert all(item.source_url == shared for item in parsed.rows)
    assert all(item.canonical_url is None for item in parsed.rows)
    assert all(item.external_source is None for item in parsed.rows)
    assert all(item.external_id is None for item in parsed.rows)


def test_lookalike_domain_is_not_treated_as_a_known_store_service() -> None:
    source = row()
    source["url"] = "https://not-tabelog.com/tokyo/A1301/A130101/13000001/"

    imported = parse_csv_bytes(csv_bytes([source]), "test.csv").rows[0]

    assert imported.source_url == source["url"]
    assert imported.canonical_url is None
    assert imported.external_source is None
    assert imported.external_id is None


def test_legacy_csv_derives_external_identity_from_canonical_url() -> None:
    source = row()
    source["url"] = "https://tabelog.com/tokyo/A1301/A130101/13000001/"

    imported = parse_csv_bytes(csv_bytes([source]), "legacy.csv").rows[0]

    assert imported.canonical_url == source["url"]
    assert imported.external_source == "tabelog"
    assert imported.external_id == "13000001"


@pytest.mark.parametrize(
    ("root_url", "subpage_url", "expected"),
    [
        (
            "https://tabelog.com/tokyo/A1301/A130101/13000001/",
            "https://s.tabelog.com/tokyo/A1301/A130101/13000001/dtlmenu/",
            ("tabelog", "13000001"),
        ),
        (
            "https://www.hotpepper.jp/strJ001234567/",
            "https://www.hotpepper.jp/strJ001234567/course/",
            ("hotpepper", "strj001234567"),
        ),
        (
            "https://r.gnavi.co.jp/abc12345/",
            "https://r.gnavi.co.jp/abc12345/menu1/",
            ("gnavi", "abc12345"),
        ),
        (
            "https://www.ikyu.com/restaurant/123456/",
            "https://www.ikyu.com/restaurant/123456/plan/",
            ("ikyu", "123456"),
        ),
        (
            "https://retty.me/area/PRE13/stores/100001234/",
            "https://retty.me/area/PRE13/stores/100001234/menu/",
            ("retty", "100001234"),
        ),
    ],
)
def test_external_identity_is_stable_across_store_subpages(
    root_url: str,
    subpage_url: str,
    expected: tuple[str | None, str | None],
) -> None:
    assert extract_external_identity(root_url) == expected
    assert extract_external_identity(subpage_url) == expected


def test_gnavi_search_route_is_not_a_store_identity() -> None:
    assert extract_external_identity("https://r.gnavi.co.jp/search/menu/") == (
        None,
        None,
    )


def test_explicit_url_columns_take_precedence_even_when_blank() -> None:
    columns = BASE_COLUMNS + ["source_url", "canonical_url"]
    source = row()
    source.update(
        {
            "url": "https://x.com/example/status/1234567890123456789",
            "source_url": "",
            "canonical_url": "https://tabelog.com/tokyo/A1301/A130101/13000001/",
        }
    )
    parsed = parse_csv_bytes(csv_bytes([source], columns), "test.csv")
    assert parsed.rows[0].source_url is None
    assert parsed.rows[0].canonical_url == source["canonical_url"]


@pytest.mark.parametrize(
    "invalid_url",
    [
        "https://user:secret@example.com/shop",
        "https://example.com:invalid/shop",
    ],
)
def test_import_rejects_unsafe_explicit_urls(invalid_url: str) -> None:
    columns = BASE_COLUMNS + ["canonical_url"]
    source = row()
    source["canonical_url"] = invalid_url

    with pytest.raises(ImportValidationFailure):
        parse_csv_bytes(csv_bytes([source], columns), "unsafe.csv")


def test_import_url_validation_does_not_echo_userinfo() -> None:
    columns = BASE_COLUMNS + ["canonical_url"]
    source = row()
    secret = "TOPSECRET"
    source["canonical_url"] = f"https://user:{secret}@example.com/shop"

    with pytest.raises(ImportValidationFailure) as raised:
        parse_csv_bytes(csv_bytes([source], columns), "unsafe.csv")

    assert secret not in str(raised.value)
    assert all(secret not in error for error in raised.value.errors)
    assert "canonical_url" in str(raised.value)
    assert "userinfo" in str(raised.value)


def test_explicit_blank_external_identity_is_not_derived_from_canonical_url() -> None:
    columns = BASE_COLUMNS + [
        "canonical_url",
        "shop.external_source",
        "shop.external_id",
    ]
    source = row()
    source.update(
        {
            "canonical_url": "https://tabelog.com/tokyo/A1301/A130101/13000001/",
            "shop.external_source": "",
            "shop.external_id": "",
        }
    )

    parsed = parse_csv_bytes(csv_bytes([source], columns), "roundtrip.csv")
    assert parsed.rows[0].external_source is None
    assert parsed.rows[0].external_id is None

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        preview = stage_import(db, parsed)
        apply_import_batch(db, preview.batch_id)
        restored = db.query(Shop).filter(Shop.id == 1).one()
        assert restored.canonical_url == source["canonical_url"]
        assert restored.external_source is None
        assert restored.external_id is None
    finally:
        db.close()
        engine.dispose()


def test_new_shop_fields_survive_import_validation() -> None:
    columns = BASE_COLUMNS + [
        "source_url",
        "canonical_url",
        "shop.address",
        "shop.phone",
        "shop.external_source",
        "shop.external_id",
    ]
    source = row()
    source.update(
        {
            "shop.address": "東京都中央区銀座1-2-3",
            "shop.phone": "03-1234-5678",
            "shop.external_source": "custom",
            "shop.external_id": "shop-1",
        }
    )
    imported = parse_csv_bytes(csv_bytes([source], columns), "test.csv").rows[0]
    assert imported.address == source["shop.address"]
    assert imported.phone == source["shop.phone"]
    assert imported.external_source == "custom"
    assert imported.external_id == "shop-1"


async def response_bytes(response: StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


def test_uploaded_image_keys_survive_export_reordered_rows_and_changed_ids() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    first = {**row(), "shop.image_key": "0123456789abcdef" * 4}
    second = {
        **row("12345678901234568"),
        "_id": "2",
        "shop.name": "別の店舗",
        "shop.image_key": "fedcba9876543210" * 4,
    }
    expected_keys = {
        first["shop.name"]: first["shop.image_key"],
        second["shop.name"]: second["shop.image_key"],
    }
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/export.csv",
            "headers": [],
            "session": {"authenticated": True, "admin_authenticated": True},
        }
    )
    try:
        with sessionmaker(bind=engine)() as db:
            original = parse_csv_bytes(
                csv_bytes([first, second], BASE_COLUMNS + ["shop.image_key"]),
                "original.csv",
            )
            preview = stage_import(db, original)
            apply_import_batch(db, preview.batch_id)
            response = export_csv(request=request, db=db)
            assert isinstance(response, StreamingResponse)
            exported = asyncio.run(response_bytes(response))
            reader = csv.DictReader(io.StringIO(exported.decode("utf-8-sig")))
            exported_rows = list(reader)
            assert reader.fieldnames is not None
            assert "shop.image_key" in reader.fieldnames
            assert {
                item["shop.name"]: item["shop.image_key"] for item in exported_rows
            } == expected_keys

            reordered_rows = list(reversed(exported_rows))
            for index, item in enumerate(reordered_rows, start=101):
                item["_id"] = str(index)
            reimport = parse_csv_bytes(
                csv_bytes(reordered_rows, reader.fieldnames), "reordered.csv"
            )
            preview = stage_import(db, reimport)
            staged = db.query(ImportRow).filter_by(batch_id=preview.batch_id).all()
            assert {item.shop_name: item.image_key for item in staged} == expected_keys
            apply_import_batch(db, preview.batch_id)

            restored = db.query(Shop).order_by(Shop.id).all()
            assert [item.id for item in restored] == [101, 102]
            assert {item.shop_name: item.image_key for item in restored} == expected_keys
            assert all(item.created_at == datetime(2026, 1, 2, 3, 4, 5) for item in restored)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("timestamp", "expected_utc"),
    [
        ("2020-01-02 03:04:05", "2020-01-02T03:04:05+00:00"),
        ("2020-01-02T03:04:05.123456", "2020-01-02T03:04:05.123456+00:00"),
        ("2020-01-02T03:04:05.123456Z", "2020-01-02T03:04:05.123456+00:00"),
        ("2020-01-02T03:04:05.123456+09:00", "2020-01-01T18:04:05.123456+00:00"),
        ("2020-01-01T23:30:00.654321-05:00", "2020-01-02T04:30:00.654321+00:00"),
    ],
)
def test_registration_time_survives_staging_export_and_reimport(
    timestamp: str,
    expected_utc: str,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    source = row()
    source["@timestamp"] = timestamp
    parsed = parse_csv_bytes(csv_bytes([source]), "original.csv")
    expected = datetime.fromisoformat(expected_utc)
    assert parsed.rows[0].created_at.isoformat() == expected_utc
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/export.csv",
            "headers": [],
            "session": {"authenticated": True, "admin_authenticated": True},
        }
    )
    try:
        with sessionmaker(bind=engine)() as db:
            started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            original_preview = stage_import(db, parsed)
            apply_import_batch(db, original_preview.batch_id)
            original_shop = db.get(Shop, 1)
            assert original_shop is not None
            assert original_shop.created_at == expected.replace(tzinfo=None)

            response = export_csv(request=request, db=db)
            assert isinstance(response, StreamingResponse)
            exported = asyncio.run(response_bytes(response))
            exported_row = next(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"))))
            assert exported_row["@timestamp"] == expected_utc
            preview = stage_import(db, parse_csv_bytes(exported, "roundtrip.csv"))
            assert preview.batch_id != original_preview.batch_id
            apply_import_batch(db, preview.batch_id)
            finished_at = datetime.now(timezone.utc).replace(tzinfo=None)

            restored_shop = db.get(Shop, 1)
            batch = db.get(ImportBatch, preview.batch_id)
            assert restored_shop is not None
            assert batch is not None
            assert restored_shop.created_at == expected.replace(tzinfo=None)
            assert started_at <= batch.created_at <= finished_at
            assert batch.applied_at is not None
            assert batch.created_at <= batch.applied_at <= finished_at
            assert restored_shop.created_at < batch.created_at
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("stored_timestamp", "expected_utc"),
    [
        ("2020-01-02T03:04:05.123456+09:00", "2020-01-01T18:04:05.123456+00:00"),
        ("2020-01-01T23:30:00.654321-05:00", "2020-01-02T04:30:00.654321+00:00"),
    ],
)
def test_export_normalizes_stored_timezone_without_changing_the_instant(
    stored_timestamp: str,
    expected_utc: str,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/export.csv",
            "headers": [],
            "session": {"authenticated": True, "admin_authenticated": True},
        }
    )
    try:
        with sessionmaker(bind=engine)() as db:
            preview = stage_import(db, parse_csv_bytes(csv_bytes([row()]), "original.csv"))
            apply_import_batch(db, preview.batch_id)
            db.execute(
                text("UPDATE shops SET created_at = :timestamp WHERE id = :shop_id"),
                {"timestamp": stored_timestamp, "shop_id": 1},
            )
            db.commit()
            shop = db.get(Shop, 1)
            assert shop is not None
            assert shop.created_at.isoformat() == stored_timestamp

            response = export_csv(request=request, db=db)
            assert isinstance(response, StreamingResponse)
            exported = asyncio.run(response_bytes(response))
            exported_row = next(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"))))
            assert exported_row["@timestamp"] == expected_utc
            parsed = parse_csv_bytes(exported, "roundtrip.csv")
            preview = stage_import(db, parsed)
            apply_import_batch(db, preview.batch_id)
            restored_shop = db.get(Shop, 1)
            assert restored_shop is not None
            assert restored_shop.created_at == datetime.fromisoformat(expected_utc).replace(
                tzinfo=None
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("external_source", "external_id"),
    [("tabelog", "13000001"), (None, None)],
)
def test_export_then_import_preserves_canonical_fields(
    external_source: str | None,
    external_id: str | None,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    message = Message(
        message_id="12345678901234567",
        source_created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    shop = Shop(
        id=42,
        shop_name="割烹みやび",
        branch_name="本店",
        area="銀座",
        category="割烹",
        address="東京都中央区銀座1-2-3",
        phone="0312345678",
        canonical_url="https://tabelog.com/tokyo/A1301/A130101/13000001/",
        external_source=external_source,
        external_id=external_id,
        is_visited=True,
        visited_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        rating=5,
        memo="=再訪候補",
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    mention = ShopMention(
        message=message,
        shop=shop,
        occurrence_index=0,
        extracted_name=shop.shop_name,
        extracted_branch_name=shop.branch_name,
        extracted_area=shop.area,
        extracted_category=shop.category,
        source_url="https://x.com/example/status/1234567890123456789",
        resolution_status="resolved",
        review_status="approved",
        metadata_review_status="approved",
        resolution_method="manual",
        extraction_source="manual",
    )
    db.add(mention)
    db.commit()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/export.csv",
            "headers": [],
            "session": {"authenticated": True, "admin_authenticated": True},
        }
    )
    try:
        response = export_csv(request=request, db=db)
        assert isinstance(response, StreamingResponse)
        exported = asyncio.run(response_bytes(response))
        imported = parse_csv_bytes(exported, "meshi_archive.csv").rows[0]
        assert imported.shop_id == 42
        assert imported.shop_name == shop.shop_name
        assert imported.branch_name == shop.branch_name
        assert imported.address == shop.address
        assert imported.phone == shop.phone
        assert imported.source_url == mention.source_url
        assert imported.canonical_url == shop.canonical_url
        assert imported.external_source == shop.external_source
        assert imported.external_id == shop.external_id
        assert imported.memo == shop.memo
        assert imported.rating == shop.rating
        assert imported.visited_at is not None
        assert imported.review_status.value == "approved"
        assert imported.metadata_review_status.value == "approved"
        assert imported.metadata_difference_type is None
        assert imported.needs_review is False

        preview = stage_import(db, parse_csv_bytes(exported, "meshi_archive.csv"))
        apply_import_batch(db, preview.batch_id)
        restored_shop = db.query(Shop).filter(Shop.id == 42).one()
        restored_mention = (
            db.query(ShopMention).filter(ShopMention.shop_id == restored_shop.id).one()
        )
        restored_asset = (
            db.query(SourceAsset)
            .filter(SourceAsset.message_id == restored_mention.message_id)
            .one()
        )
        assert restored_shop.branch_name == "本店"
        assert restored_mention.extracted_branch_name == "本店"
        assert restored_mention.review_status == "approved"
        assert restored_mention.metadata_review_status == "approved"
        assert restored_mention.metadata_difference_type is None
        assert restored_asset.source_service == "x"
        assert restored_asset.source_item_id == "1234567890123456789"
        assert (
            restored_asset.normalized_url
            == "https://x.com/i/status/1234567890123456789"
        )
        assert len(restored_asset.content_fingerprint or "") == 64
    finally:
        db.close()
        engine.dispose()


def test_legacy_needs_review_does_not_infer_metadata_review() -> None:
    source = row()
    source["needs_review"] = "true"

    imported = parse_csv_bytes(csv_bytes([source]), "legacy.csv").rows[0]

    assert imported.review_status.value == "pending"
    assert imported.metadata_review_status.value == "approved"
    assert imported.difference_type == "legacy_review"
