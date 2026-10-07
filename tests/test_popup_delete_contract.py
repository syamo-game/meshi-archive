from collections.abc import Generator
from datetime import datetime, timezone

import pytest
from sqlalchemy import Table, create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from db.models import Base, Message, ResolutionCandidate, ReviewEvent, Shop, ShopMention, SourceAsset
from services.review_service import RejectDecision, ReviewConflictError, apply_review_decision
from web.routers.home import shop_delete


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def _rows(db: Session, table: Table) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in db.execute(table.select().order_by(*table.primary_key.columns)))


def _mention(db: Session, shop: Shop, number: int) -> ShopMention:
    message = Message(message_id=f"9000000000000{number:04d}", content="保存する元投稿／引用")
    message.assets.append(SourceAsset(kind="image", url=f"https://images.example/{number}.jpg"))
    mention = ShopMention(
        message=message, shop=shop, occurrence_index=0, extracted_name=shop.shop_name,
        source_url=f"https://posts.example/{number}", review_status="pending",
        metadata_review_status="approved", resolution_status="ambiguous", extraction_source="test",
    )
    mention.candidates.append(ResolutionCandidate(rank=1, name="合成候補", provenance="posted_url"))
    mention.review_events.append(ReviewEvent(action="defer", note="残す履歴／メモ"))
    db.add(mention)
    return mention


def _shop() -> Shop:
    return Shop(
        shop_name="削除影響を確認する合成店", memo="店舗だけのメモ", rating=4,
        is_visited=True, visited_at=datetime(2026, 1, 2, tzinfo=timezone.utc), image_key="a" * 64,
    )


@pytest.mark.parametrize("shared", [False, True])
def test_reject_warning_matches_conditional_shop_deletion_and_preserved_sources(
    db: Session, shared: bool,
) -> None:
    shop = _shop()
    selected = _mention(db, shop, 1)
    other = _mention(db, shop, 2) if shared else None
    db.commit()
    shop_id = shop.id
    shop_before = _rows(db, Shop.__table__)
    preserved_tables = (Message.__table__, SourceAsset.__table__, ResolutionCandidate.__table__)
    preserved_before = {table.name: _rows(db, table) for table in preserved_tables}
    history_before = _rows(db, ReviewEvent.__table__)
    decision = RejectDecision(action="reject", expected_version=1, shop_version=1)

    result = apply_review_decision(db, selected.id, decision)

    assert result.shop_id is None and result.review_status == "rejected"
    assert db.get(ShopMention, selected.id) is not None
    for table in preserved_tables:
        assert _rows(db, table) == preserved_before[table.name]
    assert _rows(db, ReviewEvent.__table__)[:-1] == history_before
    latest_event = db.query(ReviewEvent).order_by(ReviewEvent.id.desc()).first()
    assert latest_event.action == "reject" and latest_event.previous_shop_id == shop_id
    if shared:
        assert _rows(db, Shop.__table__) == shop_before
        assert other is not None and other.shop_id == shop_id and other.version == 1
    else:
        assert db.get(Shop, shop_id) is None
    snapshot = {table.name: _rows(db, table) for table in Base.metadata.sorted_tables}
    with pytest.raises(ReviewConflictError):
        apply_review_decision(db, selected.id, decision)
    assert {table.name: _rows(db, table) for table in Base.metadata.sorted_tables} == snapshot


def test_direct_delete_keeps_original_posts_attachments_candidates_and_history(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_READ_ONLY", "false")
    shop = _shop()
    selected = _mention(db, shop, 1)
    other = _mention(db, shop, 2)
    db.commit()
    shop_id = shop.id
    preserved_tables = (Message.__table__, SourceAsset.__table__, ResolutionCandidate.__table__, ReviewEvent.__table__)
    before = {table.name: _rows(db, table) for table in preserved_tables}
    request = Request({
        "type": "http", "method": "POST", "path": f"/shop/{shop_id}/delete", "headers": [],
        "session": {"admin_authenticated": True, "csrf_token": "synthetic-token"},
    })

    response = shop_delete(shop_id, request, csrf_token="synthetic-token", return_to="/", db=db)

    assert response.status_code == 302
    assert db.get(Shop, shop_id) is None
    for table in preserved_tables:
        assert _rows(db, table) == before[table.name]
    for mention in (selected, other):
        db.refresh(mention)
        assert mention.shop_id is None and mention.version == 1 and mention.review_status == "pending"
