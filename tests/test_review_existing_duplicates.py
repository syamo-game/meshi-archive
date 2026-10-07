from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import pytest
from pydantic import HttpUrl
from sqlalchemy import Table, create_engine, event, select
from sqlalchemy.orm import Session

from db.models import (
    Base,
    Message,
    ProcessingRun,
    ResolutionCandidate,
    ReviewEvent,
    Shop,
    ShopMention,
    ShopRedirect,
    SourceAsset,
)
from services.review_service import (
    EditAndApproveDecision,
    EditableShop,
    ReviewConflictError,
    apply_review_decision,
)
from services.shop_creation_lock import find_shop_creation_collision

Scope = Literal["identity", "metadata"]
RowSnapshot = tuple[tuple[str, object], ...]
TableSnapshot = tuple[tuple[object, ...], ...]
DatabaseSnapshot = tuple[tuple[str, TableSnapshot], ...]

_MESSAGE_IDS = (
    "1234567890123456000",
    "1234567890123456087",
    "1234567890123456789",
)
_OLD_CATEGORY = "カフェ・喫茶店"
_NEW_CATEGORY = "スイーツ・洋菓子"
_CANONICAL_URL = "https://example.com/synthetic-shop"


@dataclass(frozen=True)
class _Records:
    shop_id: int
    duplicate_shop_id: int
    mention_id: int
    other_mention_ids: tuple[int, int]


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: sqlite3.Connection, _record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture
def records(db: Session) -> _Records:
    shops = tuple(
        Shop(
            shop_name="合成菓子店",
            branch_name="銀座店",
            area="銀座",
            category=_OLD_CATEGORY,
            address="東京都中央区銀座1-2-3",
            phone="0312345678",
            canonical_url=_CANONICAL_URL,
            memo=f"保持する利用者メモ {index}",
            rating=index + 3,
            is_visited=True,
            visited_at=datetime(2025, 1, index + 1, tzinfo=timezone.utc),
            image_key=str(index + 1) * 64,
            version=7 if index == 0 else 4,
        )
        for index in range(2)
    )
    mentions: list[ShopMention] = []
    for index, message_id in enumerate(_MESSAGE_IDS):
        shop = shops[1] if index == 1 else shops[0]
        message = Message(
            message_id=message_id,
            content=f"合成菓子店 銀座店についての元投稿 {index}",
            channel_id="9876543210987654321",
            source_created_at=datetime(2025, 2, index + 1, tzinfo=timezone.utc),
            processing_status="succeeded",
        )
        mention = ShopMention(
            message=message,
            shop=shop,
            occurrence_index=0,
            extracted_name=shop.shop_name,
            extracted_branch_name=shop.branch_name,
            extracted_area=shop.area,
            extracted_category="ジェラート",
            source_url=f"https://example.com/posts/{message_id}",
            resolution_status="resolved",
            review_status="approved",
            metadata_review_status="pending" if index == 0 else "approved",
            metadata_difference_type="new_pipeline_metadata_difference" if index == 0 else None,
            resolution_method="manual",
            extraction_source="legacy_import",
            confidence_reason=f"保持する元の根拠 {index}",
            reviewed_at=datetime(2025, 3, index + 1, tzinfo=timezone.utc),
            version=5 if index == 0 else 3,
        )
        db.add(mention)
        db.add(SourceAsset(
            message=message,
            kind="image",
            url=f"https://example.com/assets/{message_id}.png",
            title=f"保持する添付 {index}",
            description="元投稿の画像説明",
            extracted_text="保持する添付の本文",
            mime_type="image/png",
            fetch_status="available",
        ))
        mentions.append(mention)
    db.flush()
    for index, mention in enumerate(mentions):
        db.add(ReviewEvent(
            mention=mention,
            scope="identity",
            action="approve_current",
            previous_shop_id=mention.shop_id,
            selected_shop_id=mention.shop_id,
            note=f"保持する既存の確認履歴 {index}",
        ))
        db.add(ResolutionCandidate(
            mention=mention,
            rank=1,
            name="合成菓子店 銀座店",
            provenance="posted_url",
            evidence_url=mention.source_url,
        ))
        db.add(ProcessingRun(
            message_id=mention.message_id,
            stage="synthetic_fixture",
            status="succeeded",
        ))
    db.commit()
    return _Records(shops[0].id, shops[1].id, mentions[0].id, (mentions[1].id, mentions[2].id))


def _rows(db: Session, table: Table) -> TableSnapshot:
    return tuple(tuple(row) for row in db.execute(select(table).order_by(*table.primary_key.columns)))


def _row(db: Session, table: Table, key: int) -> RowSnapshot:
    primary_key = tuple(table.primary_key.columns)[0]
    result = db.execute(select(table).where(primary_key == key)).mappings().one()
    return tuple((column.name, result[column.name]) for column in table.columns)


def _without(row: RowSnapshot, fields: frozenset[str]) -> RowSnapshot:
    return tuple((name, value) for name, value in row if name not in fields)


def _snapshot(db: Session) -> DatabaseSnapshot:
    return tuple((table.name, _rows(db, table)) for table in Base.metadata.sorted_tables)


def _editable(db: Session, records: _Records) -> EditableShop:
    shop = db.get(Shop, records.shop_id)
    assert shop is not None
    return EditableShop(
        shop_name=shop.shop_name,
        branch_name=shop.branch_name,
        area=shop.area,
        category=_NEW_CATEGORY,
        address=shop.address,
        phone=shop.phone,
        canonical_url=HttpUrl(shop.canonical_url) if shop.canonical_url else None,
    )


def _decision(shop: EditableShop, scope: Scope = "metadata") -> EditAndApproveDecision:
    return EditAndApproveDecision(
        action="edit_and_approve",
        scope=scope,
        expected_version=5,
        shop_version=7,
        shop=shop,
    )


@pytest.mark.parametrize("scope,omit_branch,clear_url", [
    ("metadata", False, False),
    ("identity", False, False),
    ("identity", True, False),
    ("metadata", False, True),
])
def test_category_only_review_preserves_existing_duplicate_records(
    db: Session, records: _Records, scope: Scope, omit_branch: bool, clear_url: bool,
) -> None:
    if clear_url:
        source = db.get(Shop, records.shop_id)
        duplicate = db.get(Shop, records.duplicate_shop_id)
        assert source is not None and duplicate is not None
        source.canonical_url = duplicate.canonical_url = None
        db.commit()
        collision = find_shop_creation_collision(
            db, shop_name=source.shop_name, branch_name=source.branch_name,
            area=source.area, address=source.address, phone=source.phone,
            canonical_url=None, evidence_url=None, external_source=None,
            external_id=None, exclude_shop_id=source.id,
        )
        assert collision is not None
        assert (collision.kind, collision.shop_id) == ("name_area", duplicate.id)
    before_shop = _row(db, Shop.__table__, records.shop_id)
    before_duplicate = _row(db, Shop.__table__, records.duplicate_shop_id)
    before_mention = _row(db, ShopMention.__table__, records.mention_id)
    before_other_mentions = tuple(_row(db, ShopMention.__table__, key) for key in records.other_mention_ids)
    unchanged_tables = (Message, SourceAsset, ResolutionCandidate, ProcessingRun, ShopRedirect)
    before_related = tuple(_rows(db, model.__table__) for model in unchanged_tables)
    before_history = _rows(db, ReviewEvent.__table__)

    editable = _editable(db, records)
    if omit_branch:
        editable = EditableShop.model_validate(editable.model_dump(exclude={"branch_name"}))
    result = apply_review_decision(db, records.mention_id, _decision(editable, scope))

    assert (result.shop_id, result.version, result.metadata_review_status) == (records.shop_id, 6, "approved")
    assert result.automatically_resolved_count == 0
    target = db.get(Shop, records.shop_id)
    mention = db.get(ShopMention, records.mention_id)
    assert target is not None and (target.category, target.version) == (_NEW_CATEGORY, 8)
    assert mention is not None and mention.metadata_difference_type is None
    assert mention.metadata_reviewed_at is not None
    assert (mention.review_status, mention.resolution_status, mention.resolution_method) == (
        "approved", "resolved", "manual",
    )
    assert db.query(Shop).count() == 2
    assert db.query(ShopMention).count() == 3
    assert tuple(db.scalars(select(Message.message_id).order_by(Message.message_id))) == _MESSAGE_IDS
    assert _without(_row(db, Shop.__table__, target.id), frozenset({"category", "version", "updated_at"})) == (
        _without(before_shop, frozenset({"category", "version", "updated_at"}))
    )
    changed_mention_fields = frozenset({
        "metadata_review_status", "metadata_difference_type", "metadata_reviewed_at", "version",
    })
    if scope == "identity":
        changed_mention_fields |= {"reviewed_at"}
    assert _without(_row(db, ShopMention.__table__, mention.id), changed_mention_fields) == (
        _without(before_mention, changed_mention_fields)
    )
    assert _row(db, Shop.__table__, records.duplicate_shop_id) == before_duplicate
    assert tuple(_row(db, ShopMention.__table__, key) for key in records.other_mention_ids) == before_other_mentions
    assert tuple(_rows(db, model.__table__) for model in unchanged_tables) == before_related
    assert _rows(db, ReviewEvent.__table__)[:-1] == before_history
    newest = db.scalars(select(ReviewEvent).order_by(ReviewEvent.id.desc())).first()
    assert newest is not None
    assert (newest.mention_id, newest.scope, newest.action, newest.previous_shop_id, newest.selected_shop_id) == (
        records.mention_id, scope, "edit_and_approve", records.shop_id, records.shop_id,
    )


@pytest.mark.parametrize("scope,field,value", [
    ("metadata", "area", "神田"),
    ("metadata", "address", "東京都千代田区神田1-2-3"),
    ("metadata", "phone", "0398765432"),
    ("identity", "area", "神田"),
    ("identity", "address", "東京都千代田区神田1-2-3"),
    ("identity", "phone", "0398765432"),
    ("identity", "canonical_url", "https://example.com/other-shop"),
    ("identity", "shop_name", "別の合成菓子店"),
    ("identity", "branch_name", "神田店"),
])
def test_identity_changes_still_check_existing_shop_collisions(
    db: Session, records: _Records, scope: Scope, field: str, value: str,
) -> None:
    editable = _editable(db, records)
    values: dict[str, object] = editable.model_dump()
    values[field] = value
    decision = _decision(EditableShop.model_validate(values), scope)
    before = _snapshot(db)

    with pytest.raises(ReviewConflictError) as raised:
        apply_review_decision(db, records.mention_id, decision)
    assert raised.value.code == "shop_collision"
    db.rollback()
    assert _snapshot(db) == before


@pytest.mark.parametrize("version_field,expected_code", [
    ("expected_version", "stale_mention"),
    ("shop_version", "stale_shop"),
])
def test_category_only_review_still_rejects_stale_versions(
    db: Session, records: _Records, version_field: str, expected_code: str,
) -> None:
    decision = _decision(_editable(db, records))
    values: dict[str, object] = decision.model_dump()
    values[version_field] = 4 if version_field == "expected_version" else 6
    stale = EditAndApproveDecision.model_validate(values)
    before = _snapshot(db)

    with pytest.raises(ReviewConflictError) as raised:
        apply_review_decision(db, records.mention_id, stale)
    assert raised.value.code == expected_code
    db.rollback()
    assert _snapshot(db) == before


def _set_conflicting_external_identity(db: Session, records: _Records, url: str) -> None:
    shop = db.get(Shop, records.shop_id)
    duplicate = db.get(Shop, records.duplicate_shop_id)
    assert shop is not None and duplicate is not None
    shop.external_source = "tabelog"
    shop.external_id = "13999991"
    shop.canonical_url = url
    duplicate.canonical_url = url
    db.commit()


@pytest.mark.parametrize("url", [
    _CANONICAL_URL,
    "https://tabelog.com/tokyo/A1301/A130101/13999992/",
], ids=["external_id_would_be_cleared", "external_id_would_change"])
def test_unchanged_url_still_checks_collisions_when_external_identity_would_change(
    db: Session, records: _Records, url: str,
) -> None:
    _set_conflicting_external_identity(db, records, url)
    decision = _decision(_editable(db, records), "identity")
    before = _snapshot(db)

    with pytest.raises(ReviewConflictError) as raised:
        apply_review_decision(db, records.mention_id, decision)
    assert raised.value.code == "shop_collision"
    db.rollback()
    assert _snapshot(db) == before


def test_category_only_metadata_review_rejects_conflicting_external_identity(
    db: Session, records: _Records,
) -> None:
    _set_conflicting_external_identity(
        db, records, "https://tabelog.com/tokyo/A1301/A130101/13999992/",
    )
    decision = _decision(_editable(db, records))
    before = _snapshot(db)

    with pytest.raises(ReviewConflictError) as raised:
        apply_review_decision(db, records.mention_id, decision)
    assert raised.value.code == "candidate_identity_conflict"
    db.rollback()
    assert _snapshot(db) == before


@pytest.mark.parametrize("old_name,old_branch,new_name,new_branch", [
    ("Alpha東京店", None, "Alpha 東京店", None),
    ("Cafe", "株式会社東京店", "Cafe", "東京店"),
], ids=["name_spacing", "branch_corporate_prefix"])
def test_name_or_branch_changes_still_reject_new_strong_collision(
    db: Session, old_name: str, old_branch: str | None,
    new_name: str, new_branch: str | None,
) -> None:
    source = Shop(
        shop_name=old_name, branch_name=old_branch, area="渋谷", category=_OLD_CATEGORY,
        phone="0312345678", version=7,
    )
    other = Shop(
        shop_name=new_name, branch_name=new_branch, area="神田", category=_OLD_CATEGORY,
        phone="0312345678", version=4,
    )
    mention = ShopMention(
        message=Message(message_id=_MESSAGE_IDS[0], content="Synthetic branch evidence"),
        shop=source, occurrence_index=0, extracted_name=source.shop_name,
        extracted_area=source.area, review_status="approved", resolution_status="resolved",
        metadata_review_status="pending", version=5,
    )
    db.add_all([mention, other])
    db.commit()

    assert find_shop_creation_collision(
        db, shop_name=source.shop_name, branch_name=source.branch_name, area=source.area,
        address=None, phone=source.phone, canonical_url=None, evidence_url=None,
        external_source=None, external_id=None, exclude_shop_id=source.id,
    ) is None
    collision = find_shop_creation_collision(
        db, shop_name=other.shop_name, branch_name=other.branch_name, area=source.area,
        address=None, phone=source.phone, canonical_url=None, evidence_url=None,
        external_source=None, external_id=None, exclude_shop_id=source.id,
    )
    assert collision is not None
    assert (collision.kind, collision.shop_id) == ("strong_identity", other.id)
    decision = _decision(EditableShop(
        shop_name=other.shop_name, branch_name=other.branch_name,
        area=source.area, category=_NEW_CATEGORY,
        phone=source.phone,
    ), "identity")
    before = _snapshot(db)

    with pytest.raises(ReviewConflictError) as raised:
        apply_review_decision(db, mention.id, decision)
    assert raised.value.code == "shop_collision"
    assert "kind=strong_identity" in str(raised.value)
    db.rollback()
    assert _snapshot(db) == before
