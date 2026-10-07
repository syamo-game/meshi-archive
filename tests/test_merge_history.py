from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import Base, Message, ReviewEvent, Shop, ShopMention, ShopRedirect, SourceAsset
from services.duplicate_service import apply_safe_duplicate_merges
from services.merge_history import MergeHistorySnapshot
from services.review_service import MergeDecision, ReviewConflictError, apply_review_decision


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_mention(db: Session, shop: Shop | None, suffix: int) -> ShopMention:
    mention = ShopMention(
        message=Message(message_id=f"3234567890123{suffix:04d}", content="元の投稿本文"),
        shop=shop,
        occurrence_index=0,
        extracted_name="記録のある店舗",
        extracted_area="銀座",
        extracted_category="割烹",
        source_url=f"https://example.com/posts/{suffix}",
        review_status="deferred",
        metadata_review_status="pending",
        extraction_source="synthetic",
    )
    db.add(mention)
    return mention


def decision(mention: ShopMention, target: Shop) -> MergeDecision:
    return MergeDecision(
        action="merge",
        expected_version=mention.version,
        shop_version=mention.shop.version if mention.shop else None,
        target_shop_id=target.id,
        target_version=target.version,
        is_visited=True,
        visited_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        rating=4,
        memo="統合画面で明示的に選んだ値",
    )


def test_manual_merge_keeps_source_and_target_records_and_versions_for_every_post(db: Session) -> None:
    source = Shop(
        shop_name="旧表記", area="銀座", category="割烹", branch_name="本店",
        address="元住所", phone="03-0000-0001", canonical_url="https://example.com/old",
        external_source="synthetic", external_id="original", image_key="a" * 64,
        is_visited=True, visited_at=datetime(2026, 9, 1), rating=5, memo="元の大切なメモ",
    )
    target = Shop(shop_name="統合先", area="銀座", category="割烹", rating=2, memo="先のメモ")
    primary = add_mention(db, source, 1)
    related = add_mention(db, source, 2)
    unrelated = add_mention(db, target, 3)
    attachment = SourceAsset(message=related.message, kind="attachment", url="https://example.com/photo")
    history = ReviewEvent(mention=related, action="defer", note="過去の確認メモ")
    db.add_all([attachment, history])
    db.commit()
    source_id, target_id = source.id, target.id
    related_id, original_history_id = related.id, history.id
    old_request = decision(primary, target)

    apply_review_decision(db, primary.id, old_request)
    db.expire_all()

    assert db.get(Shop, source_id) is None
    assert db.get(Shop, target_id).memo == "統合画面で明示的に選んだ値"
    assert primary.version == 2
    assert related.version == 2
    assert related.review_status == "deferred"
    assert related.metadata_review_status == "pending"
    assert unrelated.version == 1
    assert related.source_url == "https://example.com/posts/2"
    assert related.message.content == "元の投稿本文"
    assert db.get(SourceAsset, attachment.id).message_id == related.message_id
    assert db.get(ReviewEvent, original_history_id).note == "過去の確認メモ"
    audit = db.query(ReviewEvent).filter_by(mention_id=related_id, action="merge_related").one()
    assert audit.previous_shop_id == source_id
    assert audit.selected_shop_id == target_id
    saved = MergeHistorySnapshot.model_validate_json(audit.note)
    before_source, before_target = saved.shops
    assert before_source.id == source_id
    assert before_source.memo == "元の大切なメモ"
    assert before_source.rating == 5
    assert before_source.visited_at.date().isoformat() == "2026-09-01"
    assert before_source.canonical_url == "https://example.com/old"
    assert before_source.external_id == "original"
    assert before_source.image_key == "a" * 64
    assert before_target.memo == "先のメモ"
    assert before_target.rating == 2
    assert {item.id for item in saved.moved_mentions} == {primary.id, related.id}
    assert all(item.version == 1 for item in saved.moved_mentions)
    assert db.query(ShopRedirect).filter_by(source_shop_id=source_id).one().target_shop_id == target_id
    with pytest.raises(ReviewConflictError):
        apply_review_decision(db, primary.id, old_request)
    db.rollback()
    assert db.query(ReviewEvent).count() == 3


def test_safe_merge_snapshots_identity_before_it_is_transferred(db: Session) -> None:
    keeper = Shop(shop_name="鮨の店舗", area="銀座", category="割烹", phone="0312345678", address="同じ住所")
    losing = Shop(
        shop_name="鮨の店舗", area="銀座", category="割烹", phone="0312345678", address="同じ住所",
        external_source="synthetic", external_id="keep-original-id", memo="片方だけの記録",
    )
    keep_mention = add_mention(db, keeper, 4)
    moved = add_mention(db, losing, 5)
    keep_mention.review_status = moved.review_status = "approved"
    db.commit()
    losing_id = losing.id

    result = apply_safe_duplicate_merges(db)
    db.commit()

    assert result.merged_shop_count == 1
    event = db.query(ReviewEvent).filter_by(mention_id=moved.id).one()
    history = MergeHistorySnapshot.model_validate_json(event.note)
    old_losing = next(item for item in history.shops if item.id == losing_id)
    assert old_losing.external_id == "keep-original-id"
    assert old_losing.memo == "片方だけの記録"
    assert moved.version == 2
    assert keeper.external_id == "keep-original-id"


def test_failed_merge_audit_rolls_back_related_versions_and_records(db: Session) -> None:
    source = Shop(shop_name="元店舗", memo="保持する値")
    target = Shop(shop_name="先店舗")
    primary = add_mention(db, source, 6)
    related = add_mention(db, source, 7)
    db.add(target)
    db.commit()
    source_id, target_id = source.id, target.id
    request = decision(primary, target)
    db.execute(text("CREATE TRIGGER refuse_audit BEFORE INSERT ON review_events BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END"))
    db.commit()

    with pytest.raises(IntegrityError, match="synthetic audit failure"):
        apply_review_decision(db, primary.id, request)
    db.rollback()
    db.expire_all()

    assert db.get(Shop, source_id).memo == "保持する値"
    assert db.get(Shop, target_id).version == 1
    assert primary.shop_id == related.shop_id == source_id
    assert primary.version == related.version == 1
    assert db.query(ShopRedirect).count() == 0
    assert db.query(ReviewEvent).count() == 0
