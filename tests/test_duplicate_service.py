from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base, Message, ReviewEvent, Shop, ShopMention, ShopRedirect
from scripts.duplicate_report import _parse_args
from services.duplicate_service import (
    apply_safe_duplicate_merges,
    build_duplicate_shadow_report,
    build_safe_duplicate_merge_plan,
)


def add_shop(
    db: Session,
    *,
    shop_id: int,
    message_id: str,
    branch_name: str | None = "本店",
    phone: str | None = None,
    address: str | None = None,
    memo: str | None = None,
    canonical_url: str | None = None,
    image_key: str | None = None,
    external_source: str | None = None,
    external_id: str | None = None,
) -> Shop:
    message = Message(message_id=message_id, processing_status="succeeded")
    shop = Shop(
        id=shop_id,
        shop_name="銀座 鮨はな",
        branch_name=branch_name,
        area="銀座",
        category="寿司・回転寿司",
        phone=phone,
        address=address,
        memo=memo,
        canonical_url=canonical_url,
        image_key=image_key,
        external_source=external_source,
        external_id=external_id,
    )
    db.add(
        ShopMention(
            message=message,
            shop=shop,
            occurrence_index=0,
            extracted_name=shop.shop_name,
            extracted_branch_name=branch_name,
            extracted_area=shop.area,
            extracted_category=shop.category,
            resolution_status="resolved",
            review_status="approved",
            metadata_review_status="approved",
            resolution_method="manual",
            extraction_source="test",
        )
    )
    return shop


def test_duplicate_report_requires_explicit_apply_flag() -> None:
    assert _parse_args([]).apply_safe is False
    assert _parse_args(["--apply-safe"]).apply_safe is True


def test_name_branch_area_alone_is_only_a_shadow_candidate() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(db, shop_id=1, message_id="12345678901234567")
        add_shop(db, shop_id=2, message_id="12345678901234568")
        db.commit()

        report = build_duplicate_shadow_report(db)

        assert len(report.groups) == 1
        assert report.groups[0].shop_ids == (1, 2)
        assert report.groups[0].would_auto_merge is False
        assert build_safe_duplicate_merge_plan(db).groups == ()
    finally:
        db.close()
        engine.dispose()


def test_matching_phone_is_eligible_only_without_user_data_conflicts() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            phone="03-1234-5678",
        )
        add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            phone="0312345678",
        )
        db.commit()
        report = build_duplicate_shadow_report(db)
        assert report.groups[0].evidence.phone_match is True
        assert report.groups[0].would_auto_merge is True
        assert build_safe_duplicate_merge_plan(db).groups == ()

        db.query(Shop).filter(Shop.id == 1).one().memo = "利用者メモA"
        db.query(Shop).filter(Shop.id == 2).one().memo = "利用者メモB"
        db.commit()
        conflicted = build_duplicate_shadow_report(db)
        assert conflicted.groups[0].user_data_conflicts == ("memo",)
        assert conflicted.groups[0].would_auto_merge is False
    finally:
        db.close()
        engine.dispose()


def test_different_branches_are_not_grouped() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            branch_name="本店",
        )
        add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            branch_name="新宿店",
        )
        db.commit()

        report = build_duplicate_shadow_report(db)

        assert report.groups == ()
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("keeper_key", "losing_key", "expected_key"),
    [
        (None, "a" * 64, "a" * 64),
        ("a" * 64, "a" * 64, "a" * 64),
        ("b" * 64, None, "b" * 64),
    ],
)
def test_safe_external_identity_merge_is_audited_and_can_be_rolled_back(
    keeper_key: str | None,
    losing_key: str | None,
    expected_key: str,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            external_source="食べログ",
            external_id="ABC123",
            image_key=keeper_key,
        )
        add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            phone="03-1234-5678",
            address="東京都中央区銀座1丁目2番3号",
            canonical_url="https://example.com/shops/ginza-hana",
            external_source="Tabelog.com",
            external_id="abc123",
            image_key=losing_key,
        )
        db.add(
            ShopMention(
                message=Message(
                    message_id="12345678901234569",
                    processing_status="succeeded",
                ),
                shop_id=2,
                occurrence_index=0,
                extracted_name="銀座 鮨はな",
                extracted_branch_name="本店",
                extracted_area="銀座",
                extracted_category="寿司・回転寿司",
                resolution_status="ambiguous",
                review_status="pending",
                metadata_review_status="approved",
                extraction_source="test",
            )
        )
        db.commit()

        plan = build_safe_duplicate_merge_plan(db)

        assert plan.safe_group_count == 1
        assert plan.groups[0].shop_ids == (1, 2)
        assert plan.groups[0].evidence == ("external_id",)

        result = apply_safe_duplicate_merges(db)

        assert result.merged_group_count == 1
        assert result.merged_shop_count == 1
        assert result.moved_mention_count == 2
        keeper = db.get(Shop, 1)
        assert keeper is not None
        assert keeper.phone == "03-1234-5678"
        assert keeper.address == "東京都中央区銀座1丁目2番3号"
        assert keeper.canonical_url == "https://example.com/shops/ginza-hana"
        assert keeper.image_key == expected_key
        assert ("image_key" in result.groups[0].complemented_fields) == (keeper_key is None)
        assert db.get(Shop, 2) is None
        assert {
            mention.shop_id
            for mention in db.query(ShopMention).order_by(ShopMention.id).all()
        } == {1}
        moved_mentions = (
            db.query(ShopMention)
            .filter(ShopMention.message_id.in_(("12345678901234568", "12345678901234569")))
            .order_by(ShopMention.message_id)
            .all()
        )
        assert tuple(mention.version for mention in moved_mentions) == (2, 2)
        events = db.query(ReviewEvent).order_by(ReviewEvent.mention_id).all()
        assert len(events) == 2
        assert {event.action for event in events} == {"automatic_merge"}
        assert {event.previous_shop_id for event in events} == {2}
        assert {event.selected_shop_id for event in events} == {1}
        redirect = db.get(ShopRedirect, 2)
        assert redirect is not None
        assert redirect.target_shop_id == 1

        db.rollback()

        assert db.query(Shop).count() == 2
        assert db.query(ReviewEvent).count() == 0
        assert db.query(ShopRedirect).count() == 0
        restored_keeper = db.get(Shop, 1)
        restored_source = db.get(Shop, 2)
        assert restored_keeper is not None
        assert restored_source is not None
        assert restored_keeper.image_key == keeper_key
        assert restored_source.image_key == losing_key
        original_mentions = (
            db.query(ShopMention)
            .filter(ShopMention.message_id.in_(("12345678901234568", "12345678901234569")))
            .all()
        )
        assert {mention.shop_id for mention in original_mentions} == {2}
        assert {mention.version for mention in original_mentions} == {1}
    finally:
        db.close()
        engine.dispose()


def test_safe_merge_skips_different_images_without_changing_data() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with sessionmaker(bind=engine)() as db:
            add_shop(
                db,
                shop_id=1,
                message_id="12345678901234567",
                phone="03-1234-5678",
                address="東京都中央区銀座1丁目2番3号",
                image_key="a" * 64,
            )
            add_shop(
                db,
                shop_id=2,
                message_id="12345678901234568",
                phone="03-1234-5678",
                address="東京都中央区銀座1丁目2番3号",
                image_key="b" * 64,
            )
            db.commit()
            shops_before = db.execute(select(Shop.__table__).order_by(Shop.id)).all()
            mentions_before = db.execute(select(ShopMention.__table__)).all()

            shadow = build_duplicate_shadow_report(db)
            assert shadow.groups[0].user_data_conflicts == ("image_key",)
            assert shadow.groups[0].would_auto_merge is False
            plan = build_safe_duplicate_merge_plan(db)
            assert plan.groups[0].user_data_conflicts == ("image_key",)
            assert plan.groups[0].is_safe is False
            result = apply_safe_duplicate_merges(db)
            db.commit()

            assert result.groups[0].action == "skipped"
            assert result.groups[0].reason == "user_data_conflict:image_key"
            assert result.merged_shop_count == 0
            assert db.execute(select(Shop.__table__).order_by(Shop.id)).all() == shops_before
            assert db.execute(select(ShopMention.__table__)).all() == mentions_before
            assert db.query(ReviewEvent).count() == 0
            assert db.query(ShopRedirect).count() == 0
    finally:
        engine.dispose()


def test_safe_merge_rechecks_images_changed_in_another_session() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with sessionmaker(bind=engine)() as db:
            keeper = add_shop(
                db,
                shop_id=1,
                message_id="12345678901234567",
                phone="03-1234-5678",
                address="東京都中央区銀座1丁目2番3号",
            )
            source = add_shop(
                db,
                shop_id=2,
                message_id="12345678901234568",
                phone="03-1234-5678",
                address="東京都中央区銀座1丁目2番3号",
                image_key="a" * 64,
            )
            db.commit()
            assert build_safe_duplicate_merge_plan(db).groups[0].is_safe is True
            assert keeper.image_key is None
            assert source.image_key == "a" * 64

            with Session(engine) as editor:
                updated = editor.get(Shop, 1)
                assert updated is not None
                updated.image_key = "b" * 64
                updated.version += 1
                editor.commit()

            assert keeper.image_key is None
            result = apply_safe_duplicate_merges(db)
            db.commit()

            assert result.groups[0].action == "skipped"
            assert result.groups[0].reason == "user_data_conflict:image_key"
            assert db.query(Shop).count() == 2
            assert keeper.image_key == "b" * 64
            assert source.image_key == "a" * 64
            assert db.query(ReviewEvent).count() == 0
            assert db.query(ShopRedirect).count() == 0
    finally:
        engine.dispose()


def test_phone_and_full_address_merge_complements_external_identity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            phone="03-1234-5678",
            address="東京都中央区銀座1丁目2番3号",
        )
        add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            phone="0312345678",
            address="東京都中央区銀座1-2-3",
            external_source="tabelog",
            external_id="13000001",
        )
        db.commit()

        plan = build_safe_duplicate_merge_plan(db)
        result = apply_safe_duplicate_merges(db)
        db.commit()

        assert plan.groups[0].is_safe is True
        assert plan.groups[0].evidence == ("phone_address",)
        assert result.groups[0].complemented_fields == ("external_identity",)
        keeper = db.get(Shop, 1)
        assert keeper is not None
        assert keeper.external_source == "tabelog"
        assert keeper.external_id == "13000001"
        assert db.get(Shop, 2) is None
    finally:
        db.close()
        engine.dispose()


def test_phone_and_address_merge_rejects_asymmetric_branch_presence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            branch_name="本店",
            phone="03-1234-5678",
            address="東京都中央区銀座1丁目2番3号",
        )
        add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            branch_name=None,
            phone="0312345678",
            address="東京都中央区銀座1-2-3",
        )
        db.commit()

        plan = build_safe_duplicate_merge_plan(db)
        result = apply_safe_duplicate_merges(db)

        assert plan.groups[0].is_safe is False
        assert plan.groups[0].evidence == ("phone_address",)
        assert "branch_name" in plan.groups[0].identity_conflicts
        assert result.groups[0].action == "skipped"
        assert db.query(Shop).count() == 2
    finally:
        db.close()
        engine.dispose()


def test_exact_external_id_allows_asymmetric_branch_safe_merge() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            branch_name="本店",
            phone="03-1234-5678",
            address="東京都中央区銀座1丁目2番3号",
            external_source="食べログ",
            external_id="13000001",
        )
        add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            branch_name=None,
            phone="0312345678",
            address="東京都中央区銀座1-2-3",
            external_source="Tabelog.com",
            external_id="13000001",
        )
        db.commit()

        plan = build_safe_duplicate_merge_plan(db)
        result = apply_safe_duplicate_merges(db)

        assert plan.groups[0].is_safe is True
        assert plan.groups[0].evidence == ("external_id", "phone_address")
        assert result.groups[0].action == "merged"
        assert db.query(Shop).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_different_nonempty_canonical_urls_block_safe_merge() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            canonical_url="https://example.com/shops/one",
            external_source="食べログ",
            external_id="ABC123",
        )
        add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            canonical_url="https://example.com/shops/two",
            external_source="Tabelog.com",
            external_id="abc123",
        )
        db.commit()

        plan = build_safe_duplicate_merge_plan(db)
        result = apply_safe_duplicate_merges(db)

        assert plan.groups[0].is_safe is False
        assert plan.groups[0].identity_conflicts == ("canonical_url",)
        assert result.groups[0].action == "skipped"
        assert result.groups[0].reason == "identity_conflict:canonical_url"
        assert db.query(Shop).count() == 2
        assert db.query(ShopRedirect).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_conflicting_branch_and_user_data_block_external_identity_merge() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            branch_name="本店",
            memo="また行きたい",
            external_source="食べログ",
            external_id="ABC123",
        )
        add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            branch_name="新宿店",
            memo="予約が必要",
            external_source="Tabelog.com",
            external_id="abc123",
        )
        db.commit()

        plan = build_safe_duplicate_merge_plan(db)
        result = apply_safe_duplicate_merges(db)

        assert plan.groups[0].is_safe is False
        assert "branch_name" in plan.groups[0].identity_conflicts
        assert plan.groups[0].user_data_conflicts == ("memo",)
        assert result.groups[0].action == "skipped"
        assert db.query(Shop).count() == 2
    finally:
        db.close()
        engine.dispose()


def test_inconsistent_unvisited_date_blocks_safe_merge() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            external_source="食べログ",
            external_id="ABC123",
        )
        inconsistent = add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            external_source="Tabelog.com",
            external_id="abc123",
        )
        inconsistent.visited_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
        db.commit()

        plan = build_safe_duplicate_merge_plan(db)
        result = apply_safe_duplicate_merges(db)

        assert plan.groups[0].is_safe is False
        assert plan.groups[0].user_data_conflicts == ("visit_state",)
        assert result.groups[0].action == "skipped"
        assert db.query(Shop).count() == 2
    finally:
        db.close()
        engine.dispose()


def test_canonical_url_identity_matches_explicit_external_identity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            canonical_url=(
                "https://tabelog.com/tokyo/A1301/A130101/13000001/"
            ),
        )
        add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            external_source="食べログ",
            external_id="13000001",
        )
        db.commit()

        plan = build_safe_duplicate_merge_plan(db)
        result = apply_safe_duplicate_merges(db)
        db.commit()

        assert plan.groups[0].is_safe is True
        assert plan.groups[0].evidence == ("external_id",)
        assert result.groups[0].action == "merged"
        keeper = db.get(Shop, 1)
        assert keeper is not None
        assert keeper.external_source == "食べログ"
        assert keeper.external_id == "13000001"
        assert db.get(Shop, 2) is None
    finally:
        db.close()
        engine.dispose()


def test_conflicting_url_and_explicit_ids_block_safe_merge() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            canonical_url=(
                "https://tabelog.com/tokyo/A1301/A130101/13000002/"
            ),
            external_source="食べログ",
            external_id="13000001",
        )
        add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            canonical_url=(
                "https://tabelog.com/tokyo/A1301/A130101/13000002/"
            ),
        )
        db.commit()

        plan = build_safe_duplicate_merge_plan(db)
        result = apply_safe_duplicate_merges(db)

        assert plan.groups[0].is_safe is False
        assert "external_identity" in plan.groups[0].identity_conflicts
        assert result.groups[0].action == "skipped"
        assert db.query(Shop).count() == 2
    finally:
        db.close()
        engine.dispose()


def test_safe_merge_flattens_redirects_to_the_oldest_shop() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        add_shop(
            db,
            shop_id=1,
            message_id="12345678901234567",
            external_source="食べログ",
            external_id="ABC123",
        )
        source = add_shop(
            db,
            shop_id=2,
            message_id="12345678901234568",
            external_source="Tabelog.com",
            external_id="abc123",
        )
        db.add(ShopRedirect(source_shop_id=90, target=source, reason="merge"))
        db.commit()

        result = apply_safe_duplicate_merges(db)
        db.commit()

        assert result.groups[0].keep_shop_id == 1
        assert db.get(ShopRedirect, 2).target_shop_id == 1
        assert db.get(ShopRedirect, 90).target_shop_id == 1
    finally:
        db.close()
        engine.dispose()
