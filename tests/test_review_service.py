from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import pytest
from pydantic import ValidationError
from sqlalchemy import Select, create_engine, event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from db.models import (
    Base,
    Message,
    MetadataReviewStatus,
    ResolutionCandidate,
    ReviewEvent,
    ReviewScope,
    ReviewStatus,
    Shop,
    ShopMention,
    ShopRedirect,
)
from services.review_service import (
    ApproveCandidateDecision,
    ApproveCurrentDecision,
    DeferDecision,
    EditAndApproveDecision,
    EditableShop,
    MergeDecision,
    RejectDecision,
    ReviewConflictError,
    ReviewInvalidDecisionError,
    apply_review_decision,
)
from services import mention_reevaluation, review_service
from services.extraction_safety import EVENT_EXCLUDED


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: object, _record: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def create_mention(db: Session, suffix: int, shop_name: str = "仮店舗") -> ShopMention:
    message_id = f"1234567890123{suffix:04d}"
    message = Message(message_id=message_id, content="投稿本文")
    shop = Shop(shop_name=shop_name, area="銀座", category="割烹")
    mention = ShopMention(
        message=message,
        shop=shop,
        occurrence_index=0,
        extracted_name=shop_name,
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    db.add(mention)
    db.commit()
    return mention


def test_approve_defer_and_conflict(db: Session) -> None:
    approved = create_mention(db, 1)
    result = apply_review_decision(
        db,
        approved.id,
        ApproveCurrentDecision(
            action="approve_current", expected_version=1, shop_version=1
        ),
    )
    assert result.review_status == ReviewStatus.APPROVED.value
    assert result.version == 2
    with pytest.raises(ReviewConflictError):
        apply_review_decision(
            db,
            approved.id,
            ApproveCurrentDecision(
                action="approve_current", expected_version=1, shop_version=1
            ),
        )

    deferred = create_mention(db, 2)
    result = apply_review_decision(
        db,
        deferred.id,
        DeferDecision(action="defer", expected_version=1, note="根拠待ち"),
    )
    assert result.review_status == ReviewStatus.DEFERRED.value


def test_identity_approval_reuses_exact_identity_for_shopless_pending_only(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_mention(db, 27, "割烹みやび")
    duplicate_message = Message(
        message_id="22345678901230027",
        content="同じ店舗の別投稿",
    )
    duplicate = ShopMention(
        message=duplicate_message,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        metadata_review_status="pending",
        extraction_source="test",
    )
    deferred_message = Message(
        message_id="22345678901230028",
        content="人が保留した同じ店舗",
    )
    deferred = ShopMention(
        message=deferred_message,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="deferred",
        extraction_source="test",
    )
    other_message = Message(
        message_id="22345678901230029",
        content="別の仮店舗へ紐付いた投稿",
    )
    other_shop = Shop(shop_name="割烹みやび", area="銀座", category="割烹")
    attached = ShopMention(
        message=other_message,
        shop=other_shop,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    db.add_all((duplicate, deferred, attached))
    db.commit()
    locked_id_sets: list[tuple[int, ...]] = []
    original_lock_statement = mention_reevaluation._pending_lock_statement

    def capture_lock_statement(
        mention_ids: tuple[int, ...],
        *,
        dialect_name: str,
    ) -> Select[tuple[ShopMention]]:
        locked_id_sets.append(mention_ids)
        return original_lock_statement(
            mention_ids,
            dialect_name=dialect_name,
        )

    monkeypatch.setattr(
        mention_reevaluation,
        "_pending_lock_statement",
        capture_lock_statement,
    )

    result = apply_review_decision(
        db,
        source.id,
        ApproveCurrentDecision(
            action="approve_current",
            expected_version=1,
            shop_version=1,
        ),
    )

    db.refresh(duplicate)
    db.refresh(deferred)
    db.refresh(attached)
    assert result.automatically_resolved_count == 1
    assert result.automatically_resolved_mention_ids == (duplicate.id,)
    assert duplicate.shop_id == source.shop_id
    assert duplicate.review_status == ReviewStatus.APPROVED.value
    assert duplicate.metadata_review_status == MetadataReviewStatus.APPROVED.value
    assert duplicate.resolution_basis == "existing_shop"
    assert duplicate.reused_from_mention_id == source.id
    assert duplicate.version == 2
    assert deferred.review_status == ReviewStatus.DEFERRED.value
    assert attached.review_status == ReviewStatus.PENDING.value
    assert attached.shop_id == other_shop.id
    assert locked_id_sets == [(duplicate.id,)]
    event = (
        db.query(ReviewEvent)
        .filter(
            ReviewEvent.mention_id == duplicate.id,
            ReviewEvent.action == "auto_reuse_approved_identity",
        )
        .one()
    )
    assert event.selected_shop_id == source.shop_id


def test_pending_reevaluation_uses_ordered_skip_locked_for_postgresql() -> None:
    statement = mention_reevaluation._pending_lock_statement(
        (12, 4),
        dialect_name="postgresql",
    )

    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "shop_mentions.id IN (12, 4)" in sql
    assert "ORDER BY shop_mentions.id ASC" in sql
    assert sql.endswith("FOR UPDATE SKIP LOCKED")


def test_identity_approval_acquires_advisory_lock_before_mention_row_lock(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_mention(db, 35, "割烹みやび")
    calls: list[str] = []
    original_load_mention = review_service._load_mention

    def capture_advisory_lock(_db: Session) -> None:
        calls.append("advisory")

    def capture_mention_lock(
        session: Session,
        mention_id: int,
        expected_version: int,
    ) -> ShopMention:
        calls.append("mention")
        return original_load_mention(session, mention_id, expected_version)

    monkeypatch.setattr(
        review_service,
        "lock_new_shop_creation",
        capture_advisory_lock,
    )
    monkeypatch.setattr(
        review_service,
        "_load_mention",
        capture_mention_lock,
    )

    apply_review_decision(
        db,
        source.id,
        ApproveCurrentDecision(
            action="approve_current",
            expected_version=1,
            shop_version=1,
        ),
    )

    assert calls[:2] == ["advisory", "mention"]


def test_review_group_completes_area_from_safe_matching_candidate(
    db: Session,
) -> None:
    complete = ShopMention(
        message=Message(
            message_id="22345678901230041",
            content="地域が本文にある投稿",
        ),
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_branch_name="銀座店",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    complemented = ShopMention(
        message=Message(
            message_id="22345678901230042",
            content="地域が候補にだけある投稿",
        ),
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_branch_name="銀座店",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    complemented.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="割烹みやび 銀座店",
            area="銀座",
            address="東京都中央区銀座1-1-1",
            provenance="web_search",
            is_verified=False,
            name_similarity_milli=1000,
            is_strong_match=False,
        )
    )
    db.add_all((complete, complemented))
    db.commit()

    groups = mention_reevaluation.build_open_review_group_index(db)

    assert groups[complete.id].mention_ids == (complete.id, complemented.id)
    assert groups[complemented.id].mention_ids == (complete.id, complemented.id)
    assert groups[complete.id].reason == "抽出した店名・支店名・エリアが一致"


def test_review_group_rejects_unverified_non_web_candidate() -> None:
    mention = ShopMention(
        occurrence_index=0,
        extracted_name="割烹みやび",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    candidate = ResolutionCandidate(
        rank=1,
        name="割烹みやび",
        external_source="tabelog",
        external_id="13000001",
        provenance="structured_data",
        is_verified=False,
        name_similarity_milli=1000,
        is_strong_match=False,
    )

    assert mention_reevaluation._candidate_can_group(mention, candidate) is False


def test_review_group_uses_safe_unverified_candidate_evidence_only(
    db: Session,
) -> None:
    external_first = ShopMention(
        message=Message(
            message_id="22345678901230043",
            content="外部IDが一致する投稿1",
        ),
        occurrence_index=0,
        extracted_name="表記A",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    external_second = ShopMention(
        message=Message(
            message_id="22345678901230044",
            content="外部IDが一致する投稿2",
        ),
        occurrence_index=0,
        extracted_name="表記B",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    for mention, candidate_name in (
        (external_first, "表記A"),
        (external_second, "表記B"),
    ):
        mention.candidates.append(
            ResolutionCandidate(
                rank=1,
                name=candidate_name,
                external_source="食べログ",
                external_id="13000001",
                provenance="web_search",
                is_verified=False,
                name_similarity_milli=1000,
                is_strong_match=False,
            )
        )
    name_only = ShopMention(
        message=Message(
            message_id="22345678901230045",
            content="店名だけが一致する投稿",
        ),
        occurrence_index=0,
        extracted_name="店名だけ一致",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    address_only = ShopMention(
        message=Message(
            message_id="22345678901230046",
            content="住所だけが一致する投稿",
        ),
        occurrence_index=0,
        extracted_name="別名",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    address_only.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="別名",
            address="東京都中央区銀座1-1-1",
            provenance="web_search",
            is_verified=False,
            name_similarity_milli=0,
            is_strong_match=False,
        )
    )
    other_address_only = ShopMention(
        message=Message(
            message_id="22345678901230047",
            content="同じ住所だが別名の投稿",
        ),
        occurrence_index=0,
        extracted_name="さらに別名",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    other_address_only.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="さらに別名",
            address="東京都中央区銀座1-1-1",
            provenance="web_search",
            is_verified=False,
            name_similarity_milli=0,
            is_strong_match=False,
        )
    )
    low_similarity = ShopMention(
        message=Message(
            message_id="22345678901230050",
            content="候補名の類似度が低い投稿",
        ),
        occurrence_index=0,
        extracted_name="寿司青空",
        extracted_area=None,
        extracted_category="寿司",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    low_similarity.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="焼肉赤坂",
            external_source="tabelog",
            external_id="13000001",
            provenance="web_search",
            is_verified=False,
            name_similarity_milli=0,
            is_strong_match=False,
        )
    )
    branch_conflict = ShopMention(
        message=Message(
            message_id="22345678901230051",
            content="候補の支店が矛盾する投稿",
        ),
        occurrence_index=0,
        extracted_name="割烹みやび 銀座店",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    branch_conflict.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="割烹みやび 渋谷店",
            external_source="tabelog",
            external_id="13000001",
            provenance="web_search",
            is_verified=False,
            name_similarity_milli=800,
            is_strong_match=False,
        )
    )
    ambiguous_external_id = ShopMention(
        message=Message(
            message_id="22345678901230052",
            content="候補の外部IDが一意でない投稿",
        ),
        occurrence_index=0,
        extracted_name="候補が割れた店舗",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    for rank, external_id in enumerate(("13000002", "13000003"), start=1):
        ambiguous_external_id.candidates.append(
            ResolutionCandidate(
                rank=rank,
                name="候補が割れた店舗",
                external_source="tabelog",
                external_id=external_id,
                provenance="web_search",
                is_verified=False,
                name_similarity_milli=1000,
                is_strong_match=False,
            )
        )
    db.add_all(
        (
            external_first,
            external_second,
            name_only,
            address_only,
            other_address_only,
            low_similarity,
            branch_conflict,
            ambiguous_external_id,
        )
    )
    db.commit()

    groups = mention_reevaluation.build_open_review_group_index(db)

    assert groups[external_first.id].mention_ids == (
        external_first.id,
        external_second.id,
    )
    assert groups[external_first.id].reason == "候補の外部IDが一致"
    assert name_only.id not in groups
    assert groups[address_only.id].mention_ids == (address_only.id,)
    assert groups[other_address_only.id].mention_ids == (other_address_only.id,)
    assert low_similarity.id not in groups
    assert branch_conflict.id not in groups
    assert ambiguous_external_id.id not in groups


def test_review_group_assigns_strong_evidence_before_larger_name_area_group(
    db: Session,
) -> None:
    mentions: list[ShopMention] = []
    for index in range(3):
        mention = ShopMention(
            message=Message(
                message_id=f"2234567890123006{index}",
                content=f"同じ店名と地域の投稿{index}",
            ),
            occurrence_index=0,
            extracted_name="割烹みやび",
            extracted_area="銀座",
            extracted_category="割烹",
            resolution_status="ambiguous",
            review_status="pending",
            extraction_source="test",
        )
        if index < 2:
            mention.candidates.append(
                ResolutionCandidate(
                    rank=1,
                    name="割烹みやび",
                    area="銀座",
                    external_source="tabelog",
                    external_id="13000001",
                    provenance="web_search",
                    is_verified=False,
                    name_similarity_milli=1000,
                    is_strong_match=False,
                )
            )
        mentions.append(mention)
    db.add_all(mentions)
    db.commit()

    groups = mention_reevaluation.build_open_review_group_index(db)

    assert groups[mentions[0].id].mention_ids == (
        mentions[0].id,
        mentions[1].id,
    )
    assert groups[mentions[0].id].reason == "候補の外部IDが一致"
    assert groups[mentions[2].id].mention_ids == (mentions[2].id,)
    assert groups[mentions[2].id].reason == "抽出した店名・支店名・エリアが一致"


def test_review_group_uses_phone_or_address_with_normalized_candidate_name(
    db: Session,
) -> None:
    phone_mentions: list[ShopMention] = []
    for index, phone in enumerate(("03-1234-5678", "0312345678")):
        mention = ShopMention(
            message=Message(
                message_id=f"2234567890123007{index}",
                content=f"同じ電話番号の候補投稿{index}",
            ),
            occurrence_index=0,
            extracted_name="電話店舗",
            extracted_area=None,
            extracted_category="割烹",
            resolution_status="ambiguous",
            review_status="pending",
            extraction_source="test",
        )
        mention.candidates.append(
            ResolutionCandidate(
                rank=1,
                name="電話 店舗",
                phone=phone,
                provenance="web_search",
                is_verified=False,
                name_similarity_milli=1000,
                is_strong_match=False,
            )
        )
        phone_mentions.append(mention)
    address_mentions: list[ShopMention] = []
    for index, address in enumerate(
        ("東京都中央区銀座1丁目1番1号", "東京都中央区銀座1-1-1")
    ):
        mention = ShopMention(
            message=Message(
                message_id=f"2234567890123008{index}",
                content=f"同じ住所の候補投稿{index}",
            ),
            occurrence_index=0,
            extracted_name="住所店舗",
            extracted_area=None,
            extracted_category="割烹",
            resolution_status="ambiguous",
            review_status="pending",
            extraction_source="test",
        )
        mention.candidates.append(
            ResolutionCandidate(
                rank=1,
                name="住所 店舗",
                address=address,
                provenance="web_search",
                is_verified=False,
                name_similarity_milli=1000,
                is_strong_match=False,
            )
        )
        address_mentions.append(mention)
    db.add_all((*phone_mentions, *address_mentions))
    db.commit()

    groups = mention_reevaluation.build_open_review_group_index(db)

    assert groups[phone_mentions[0].id].mention_ids == (
        phone_mentions[0].id,
        phone_mentions[1].id,
    )
    assert groups[phone_mentions[0].id].reason == "候補の電話番号と店名が一致"
    assert groups[address_mentions[0].id].mention_ids == (
        address_mentions[0].id,
        address_mentions[1].id,
    )
    assert groups[address_mentions[0].id].reason == "候補の住所と店名が一致"


def test_identity_reuse_keeps_conflicting_or_non_unique_mentions_pending(
    db: Session,
) -> None:
    source = create_mention(db, 28, "割烹みやび")
    source.shop.external_source = "tabelog"
    source.shop.external_id = "13000001"
    duplicate_approved_message = Message(
        message_id="22345678901230030",
        content="既存の重複店舗",
    )
    duplicate_approved_shop = Shop(
        shop_name="割烹みやび",
        area="銀座",
        category="割烹",
    )
    duplicate_approved = ShopMention(
        message=duplicate_approved_message,
        shop=duplicate_approved_shop,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="resolved",
        review_status="approved",
        extraction_source="test",
    )
    ambiguous_message = Message(
        message_id="22345678901230031",
        content="重複店舗のどちらか不明",
    )
    ambiguous = ShopMention(
        message=ambiguous_message,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    conflicting_message = Message(
        message_id="22345678901230032",
        content="外部IDが矛盾する候補",
    )
    conflicting = ShopMention(
        message=conflicting_message,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    conflicting.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="割烹みやび",
            area="銀座",
            address="東京都中央区銀座9-9-9",
            external_source="tabelog",
            external_id="13999999",
            provenance="web_search",
            is_verified=True,
            name_similarity_milli=1000,
            is_strong_match=True,
        )
    )
    db.add_all((duplicate_approved, ambiguous, conflicting))
    db.commit()

    result = apply_review_decision(
        db,
        source.id,
        ApproveCurrentDecision(
            action="approve_current",
            expected_version=1,
            shop_version=1,
        ),
    )

    db.refresh(ambiguous)
    db.refresh(conflicting)
    assert result.automatically_resolved_count == 0
    assert ambiguous.review_status == ReviewStatus.PENDING.value
    assert conflicting.review_status == ReviewStatus.PENDING.value


def test_identity_approval_reuses_unique_verified_strong_candidate(
    db: Session,
) -> None:
    source = create_mention(db, 29, "割烹みやび")
    source.shop.external_source = "tabelog"
    source.shop.external_id = "13000001"
    candidate_message = Message(
        message_id="22345678901230033",
        content="表記が違う同じ店舗",
    )
    candidate_mention = ShopMention(
        message=candidate_message,
        occurrence_index=0,
        extracted_name="みやび銀座",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    candidate_mention.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="割烹みやび",
            area="銀座",
            external_source="tabelog",
            external_id="13000001",
            provenance="web_search",
            is_verified=True,
            name_similarity_milli=800,
            is_strong_match=True,
        )
    )
    db.add(candidate_mention)
    db.commit()

    result = apply_review_decision(
        db,
        source.id,
        ApproveCurrentDecision(
            action="approve_current",
            expected_version=1,
            shop_version=1,
        ),
    )

    db.refresh(candidate_mention)
    assert result.automatically_resolved_mention_ids == (candidate_mention.id,)
    assert candidate_mention.shop_id == source.shop_id
    assert candidate_mention.resolution_basis == "verified_candidate"


@pytest.mark.parametrize("target_has_external_id", (True, False))
def test_identity_reuse_blocks_other_approved_normalized_external_identity(
    db: Session,
    target_has_external_id: bool,
) -> None:
    source = create_mention(db, 32, "割烹みやび")
    if target_has_external_id:
        source.shop.external_source = "tabelog"
        source.shop.external_id = "13000001"
    conflicting_shop = Shop(
        shop_name="別の承認済み店舗",
        area="渋谷",
        category="割烹",
        external_source="食べログ",
        external_id="13000001",
    )
    conflicting_approved = ShopMention(
        message=Message(
            message_id="22345678901230036",
            content="競合する承認済み店舗",
        ),
        shop=conflicting_shop,
        occurrence_index=0,
        extracted_name="別の承認済み店舗",
        extracted_area="渋谷",
        extracted_category="割烹",
        resolution_status="resolved",
        review_status="approved",
        extraction_source="test",
    )
    pending = ShopMention(
        message=Message(
            message_id="22345678901230037",
            content="同じ外部IDを示す未確認投稿",
        ),
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    pending.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="割烹みやび",
            area="銀座",
            external_source="tabelog",
            external_id="13000001",
            provenance="web_search",
            is_verified=True,
            name_similarity_milli=800,
            is_strong_match=True,
        )
    )
    db.add_all((conflicting_approved, pending))
    db.commit()

    result = apply_review_decision(
        db,
        source.id,
        ApproveCurrentDecision(
            action="approve_current",
            expected_version=1,
            shop_version=1,
        ),
    )

    db.refresh(pending)
    assert result.automatically_resolved_mention_ids == ()
    assert pending.review_status == ReviewStatus.PENDING.value
    assert pending.shop_id is None


def test_identity_reuse_completes_area_from_verified_matching_candidate(
    db: Session,
) -> None:
    source = create_mention(db, 33, "割烹みやび 銀座店")
    pending = ShopMention(
        message=Message(
            message_id="22345678901230038",
            content="地域が候補にだけ含まれる同じ店舗",
        ),
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_branch_name="銀座店",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    pending.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="割烹みやび 銀座店",
            area="銀座",
            provenance="structured_data",
            is_verified=True,
            name_similarity_milli=1000,
            is_strong_match=False,
        )
    )
    db.add(pending)
    db.commit()

    result = apply_review_decision(
        db,
        source.id,
        ApproveCurrentDecision(
            action="approve_current",
            expected_version=1,
            shop_version=1,
        ),
    )

    db.refresh(pending)
    assert result.automatically_resolved_mention_ids == (pending.id,)
    assert pending.shop_id == source.shop_id
    assert pending.resolution_basis == "existing_shop"


def test_identity_reuse_does_not_use_name_only_or_address_only(
    db: Session,
) -> None:
    source = create_mention(db, 34, "割烹みやび")
    source.shop.address = "東京都中央区銀座1-1-1"
    source.shop.external_source = "tabelog"
    source.shop.external_id = "13000001"
    name_only = ShopMention(
        message=Message(
            message_id="22345678901230039",
            content="店名しか一致しない投稿",
        ),
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    address_only = ShopMention(
        message=Message(
            message_id="22345678901230040",
            content="住所しか一致しない投稿",
        ),
        occurrence_index=0,
        extracted_name="別店舗",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    address_only.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="別店舗",
            address="東京都中央区銀座1-1-1",
            provenance="structured_data",
            is_verified=True,
            name_similarity_milli=1000,
            is_strong_match=False,
        )
    )
    unknown_area = ShopMention(
        message=Message(
            message_id="22345678901230048",
            content="非正規の地域を含む投稿",
        ),
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="東京都中央区銀座一丁目",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    unknown_area.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="割烹みやび",
            area="銀座",
            provenance="structured_data",
            is_verified=True,
            name_similarity_milli=1000,
            is_strong_match=False,
        )
    )
    unverified_external = ShopMention(
        message=Message(
            message_id="22345678901230049",
            content="未検証候補だけが外部IDを示す投稿",
        ),
        occurrence_index=0,
        extracted_name="みやび別表記",
        extracted_area=None,
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    unverified_external.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="みやび別表記",
            external_source="tabelog",
            external_id="13000001",
            provenance="web_search",
            is_verified=False,
            name_similarity_milli=1000,
            is_strong_match=False,
        )
    )
    conflicting_candidate_ids = ShopMention(
        message=Message(
            message_id="22345678901230053",
            content="候補内の外部IDが矛盾する投稿",
        ),
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    for rank, external_id in enumerate(("K00001", "K00002"), start=1):
        conflicting_candidate_ids.candidates.append(
            ResolutionCandidate(
                rank=rank,
                name="割烹みやび",
                area="銀座",
                external_source="hotpepper",
                external_id=external_id,
                provenance="structured_data",
                is_verified=True,
                name_similarity_milli=1000,
                is_strong_match=False,
            )
        )
    db.add_all(
        (
            name_only,
            address_only,
            unknown_area,
            unverified_external,
            conflicting_candidate_ids,
        )
    )
    db.commit()

    result = apply_review_decision(
        db,
        source.id,
        ApproveCurrentDecision(
            action="approve_current",
            expected_version=1,
            shop_version=1,
        ),
    )

    db.refresh(name_only)
    db.refresh(address_only)
    db.refresh(unknown_area)
    db.refresh(unverified_external)
    db.refresh(conflicting_candidate_ids)
    assert result.automatically_resolved_mention_ids == ()
    assert name_only.review_status == ReviewStatus.PENDING.value
    assert address_only.review_status == ReviewStatus.PENDING.value
    assert unknown_area.review_status == ReviewStatus.PENDING.value
    assert unverified_external.review_status == ReviewStatus.PENDING.value
    assert conflicting_candidate_ids.review_status == ReviewStatus.PENDING.value


def test_identity_reuse_keeps_conflicting_candidate_pending(db: Session) -> None:
    source = create_mention(db, 30, "割烹みやび")
    source.shop.external_source = "tabelog"
    source.shop.external_id = "13000001"
    source.shop.address = "東京都中央区銀座1-1-1"
    message = Message(
        message_id="22345678901230034",
        content="外部IDと住所が矛盾する候補",
    )
    conflicting = ShopMention(
        message=message,
        occurrence_index=0,
        extracted_name="割烹みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    conflicting.candidates.append(
        ResolutionCandidate(
            rank=1,
            name="割烹みやび",
            area="銀座",
            address="東京都中央区銀座9-9-9",
            external_source="tabelog",
            external_id="13999999",
            provenance="web_search",
            is_verified=True,
            name_similarity_milli=1000,
            is_strong_match=True,
        )
    )
    db.add(conflicting)
    db.commit()

    result = apply_review_decision(
        db,
        source.id,
        ApproveCurrentDecision(
            action="approve_current",
            expected_version=1,
            shop_version=1,
        ),
    )

    db.refresh(conflicting)
    assert result.automatically_resolved_count == 0
    assert conflicting.review_status == ReviewStatus.PENDING.value
    assert conflicting.shop_id is None


def test_identity_reuse_uses_the_approved_mention_name_after_manual_edit(
    db: Session,
) -> None:
    source = create_mention(db, 31, "仮称みやび")
    message = Message(
        message_id="22345678901230035",
        content="同じ仮称で投稿された店舗",
    )
    duplicate = ShopMention(
        message=message,
        occurrence_index=0,
        extracted_name="仮称みやび",
        extracted_area="銀座",
        extracted_category="割烹",
        resolution_status="ambiguous",
        review_status="pending",
        extraction_source="test",
    )
    db.add(duplicate)
    db.commit()

    result = apply_review_decision(
        db,
        source.id,
        EditAndApproveDecision(
            action="edit_and_approve",
            expected_version=1,
            shop_version=1,
            shop=EditableShop(
                shop_name="割烹みやび",
                area="銀座",
                category="割烹",
                address=None,
                phone=None,
                canonical_url=None,
            ),
        ),
    )

    db.refresh(duplicate)
    assert result.automatically_resolved_mention_ids == (duplicate.id,)
    assert duplicate.shop_id == source.shop_id
    assert duplicate.resolution_basis == "existing_shop"


def test_approve_candidate_and_edit(db: Session) -> None:
    candidate_mention = create_mention(db, 3)
    candidate = ResolutionCandidate(
        mention=candidate_mention,
        rank=1,
        name="割烹みやび",
        area="銀座",
        category="割烹",
        address="東京都中央区銀座1-2-3",
        phone="03-1234-5678",
        canonical_url="https://tabelog.com/tokyo/A1301/A130101/13000001/",
        external_source="tabelog",
        external_id="13000001",
        name_similarity_milli=950,
        is_strong_match=True,
    )
    db.add(candidate)
    db.commit()
    result = apply_review_decision(
        db,
        candidate_mention.id,
        ApproveCandidateDecision(
            action="approve_candidate",
            expected_version=1,
            shop_version=1,
            candidate_id=candidate.id,
        ),
    )
    shop = db.query(Shop).filter(Shop.id == result.shop_id).one()
    assert shop.shop_name == "割烹みやび"
    assert shop.phone == "0312345678"

    edited = create_mention(db, 4)
    result = apply_review_decision(
        db,
        edited.id,
        EditAndApproveDecision(
            action="edit_and_approve",
            expected_version=1,
            shop_version=1,
            shop=EditableShop(
                shop_name="手修正店",
                area="月島",
                category="割烹",
                address=None,
                phone=None,
                canonical_url=None,
            ),
        ),
    )
    shop = db.query(Shop).filter(Shop.id == result.shop_id).one()
    assert shop.shop_name == "手修正店"
    assert shop.area == "月島"


@pytest.mark.parametrize("reason", [None, "", " \t\n　", "  店舗情報ではない  "])
def test_reject_normalizes_blank_reason_and_records_supplied_reason(
    db: Session, reason: str | None
) -> None:
    mention = create_mention(db, 90)
    shop_id = mention.shop_id
    message_id = mention.message_id

    result = apply_review_decision(
        db,
        mention.id,
        RejectDecision(
            action="reject", expected_version=1, shop_version=1, reason=reason
        ),
    )

    assert result.review_status == ReviewStatus.REJECTED.value
    assert result.shop_id is None
    assert result.version == 2
    assert mention.resolution_status == "invalid"
    assert mention.reviewed_at is not None
    assert db.get(Shop, shop_id) is None
    assert db.get(Message, message_id) is not None
    audit = db.query(ReviewEvent).filter_by(mention_id=mention.id).one()
    assert audit.action == "reject"
    assert audit.previous_shop_id == shop_id
    assert audit.selected_shop_id is None
    assert audit.note == (reason if reason and reason.strip() else None)


def test_reject_without_reason_preserves_shared_shop(db: Session) -> None:
    mention = create_mention(db, 91)
    shop = mention.shop
    shop.memo = "残すメモ"
    shop.rating = 4
    shop.is_visited = True
    other = ShopMention(
        message=Message(message_id="12345678901230092", content="別の投稿"),
        shop=shop,
        occurrence_index=0,
        extracted_name=shop.shop_name,
        resolution_status="resolved",
        review_status="approved",
        extraction_source="test",
    )
    db.add(other)
    db.commit()
    shop_updated_at = shop.updated_at

    result = apply_review_decision(
        db,
        mention.id,
        RejectDecision(action="reject", expected_version=1, shop_version=1),
    )

    db.refresh(shop)
    db.refresh(other)
    assert result.shop_id is None
    assert other.shop_id == shop.id
    assert other.review_status == ReviewStatus.APPROVED.value
    assert other.version == 1
    assert shop.memo == "残すメモ"
    assert shop.rating == 4
    assert shop.is_visited is True
    assert shop.version == 1
    assert shop.updated_at == shop_updated_at
    audit = db.query(ReviewEvent).filter_by(mention_id=mention.id).one()
    assert audit.note is None
    assert db.query(ReviewEvent).count() == 1


def test_reject_reason_keeps_maximum_length_validation() -> None:
    reason = "あ" * 2_000
    decision = RejectDecision(action="reject", expected_version=1, reason=reason)
    assert decision.reason == reason
    with pytest.raises(ValidationError, match="at most 2000 characters"):
        RejectDecision(action="reject", expected_version=1, reason=reason + "あ")


def test_reject_removes_orphan_and_merge_uses_explicit_user_values(db: Session) -> None:
    rejected = create_mention(db, 5)
    rejected_shop_id = rejected.shop_id
    result = apply_review_decision(
        db,
        rejected.id,
        RejectDecision(
            action="reject",
            expected_version=1,
            shop_version=1,
            reason="店舗情報ではない",
        ),
    )
    assert result.review_status == ReviewStatus.REJECTED.value
    assert result.shop_id is None
    assert db.query(Shop).filter(Shop.id == rejected_shop_id).first() is None

    source = create_mention(db, 6, "統合元")
    source_shop_id = source.shop_id
    target = Shop(
        shop_name="統合先",
        area="銀座",
        category="割烹",
        is_visited=False,
        version=1,
    )
    db.add(target)
    db.commit()
    result = apply_review_decision(
        db,
        source.id,
        MergeDecision(
            action="merge",
            expected_version=1,
            shop_version=1,
            target_shop_id=target.id,
            target_version=1,
            is_visited=True,
            visited_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
            rating=5,
            memo="再訪したい",
        ),
    )
    assert result.shop_id == target.id
    merged = db.query(Shop).filter(Shop.id == target.id).one()
    assert merged.is_visited
    assert merged.rating == 5
    assert merged.memo == "再訪したい"
    redirect = (
        db.query(ShopRedirect)
        .filter(ShopRedirect.source_shop_id == source_shop_id)
        .one()
    )
    assert redirect.target_shop_id == target.id


@pytest.mark.parametrize(
    ("source_key", "target_key", "expected_key"),
    [
        ("a" * 64, None, "a" * 64),
        (None, "b" * 64, "b" * 64),
        ("a" * 64, "a" * 64, "a" * 64),
        (None, None, None),
    ],
)
def test_manual_merge_preserves_uploaded_image(
    db: Session,
    source_key: str | None,
    target_key: str | None,
    expected_key: str | None,
) -> None:
    mention = create_mention(db, 110, "統合元")
    assert mention.shop is not None
    mention.shop.image_key = source_key
    source_shop_id = mention.shop_id
    target = Shop(shop_name="統合先", area="銀座", category="割烹", image_key=target_key)
    db.add(target)
    db.commit()

    result = apply_review_decision(
        db,
        mention.id,
        MergeDecision(
            action="merge",
            expected_version=1,
            shop_version=1,
            target_shop_id=target.id,
            target_version=1,
            is_visited=False,
            visited_at=None,
            rating=None,
            memo=None,
        ),
    )

    assert result.shop_id == target.id
    db.refresh(target)
    assert target.image_key == expected_key
    assert db.get(Shop, source_shop_id) is None
    assert db.get(ShopRedirect, source_shop_id).target_shop_id == target.id


def test_manual_merge_rejects_different_images_before_changing_data(db: Session) -> None:
    mention = create_mention(db, 111, "統合元")
    assert mention.shop is not None
    mention.shop.image_key = "a" * 64
    target = Shop(shop_name="統合先", area="銀座", category="割烹", image_key="b" * 64)
    db.add(target)
    db.flush()
    db.add(ShopRedirect(source_shop_id=999, target_shop_id=mention.shop_id))
    db.commit()
    shops_before = db.execute(select(Shop.__table__).order_by(Shop.id)).all()
    mentions_before = db.execute(select(ShopMention.__table__)).all()
    redirects_before = db.execute(select(ShopRedirect.__table__)).all()

    with pytest.raises(ReviewConflictError, match="異なる写真が登録されています"):
        apply_review_decision(
            db,
            mention.id,
            MergeDecision(
                action="merge",
                expected_version=1,
                shop_version=1,
                target_shop_id=target.id,
                target_version=1,
                is_visited=True,
                visited_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
                rating=5,
                memo="統合後の値",
            ),
        )

    db.flush()
    assert db.execute(select(Shop.__table__).order_by(Shop.id)).all() == shops_before
    assert db.execute(select(ShopMention.__table__)).all() == mentions_before
    assert db.execute(select(ShopRedirect.__table__)).all() == redirects_before
    assert db.query(ReviewEvent).count() == 0
    db.rollback()
    assert db.execute(select(Shop.__table__).order_by(Shop.id)).all() == shops_before


def test_consecutive_merges_flatten_old_shop_redirects(db: Session) -> None:
    mention = create_mention(db, 16, "First")
    first_shop_id = mention.shop_id
    middle = Shop(shop_name="Middle", area="Tokyo", category="Sushi", version=1)
    final = Shop(shop_name="Final", area="Tokyo", category="Sushi", version=1)
    db.add_all([middle, final])
    db.commit()

    first_result = apply_review_decision(
        db,
        mention.id,
        MergeDecision(
            action="merge",
            expected_version=1,
            shop_version=1,
            target_shop_id=middle.id,
            target_version=1,
            is_visited=False,
            visited_at=None,
            rating=None,
            memo=None,
        ),
    )
    assert first_result.version == 2

    apply_review_decision(
        db,
        mention.id,
        MergeDecision(
            action="merge",
            expected_version=2,
            shop_version=2,
            target_shop_id=final.id,
            target_version=1,
            is_visited=False,
            visited_at=None,
            rating=None,
            memo=None,
        ),
    )

    redirects = {
        redirect.source_shop_id: redirect.target_shop_id
        for redirect in db.query(ShopRedirect).all()
    }
    assert redirects == {first_shop_id: final.id, middle.id: final.id}


def create_shopless_mention(db: Session, suffix: int) -> ShopMention:
    message = Message(
        message_id=f"2234567890123{suffix:04d}",
        content="A restaurant recommendation",
    )
    mention = ShopMention(
        message=message,
        occurrence_index=0,
        extracted_name="Central Noodles",
        extracted_branch_name="East Exit",
        extracted_area="Tokyo",
        extracted_category="Ramen",
        resolution_status="ambiguous",
        review_status="pending",
        metadata_review_status="pending",
        metadata_difference_type="unknown_category",
        extraction_source="test",
    )
    db.add(mention)
    db.commit()
    return mention


def create_excluded_event(db: Session) -> ShopMention:
    mention = create_shopless_mention(db, 95)
    mention.extracted_name = "Temporary Venue"
    mention.extracted_branch_name = "Event hall"
    mention.extracted_area = "銀座"
    mention.extracted_category = "寿司"
    mention.review_status = "rejected"
    mention.resolution_status = "invalid"
    mention.metadata_review_status = "deferred"
    mention.difference_type = EVENT_EXCLUDED
    mention.extraction_error = 'evidence_review:{"codes":["event_excluded"],"reasons":["出店元未特定"]}'
    mention.candidates.append(ResolutionCandidate(
        name="Temporary Venue", rank=1, area="銀座", category="寿司", name_similarity_milli=1000,
    ))
    db.commit()
    return mention


@pytest.mark.parametrize("action", ["approve_current", "approve_candidate", "defer", "metadata_approve"])
def test_excluded_event_cannot_recreate_venue_by_approval_or_deferral(db: Session, action: str) -> None:
    mention = create_excluded_event(db)
    decision: ApproveCurrentDecision | ApproveCandidateDecision | DeferDecision
    if action == "approve_candidate":
        decision = ApproveCandidateDecision(action=action, expected_version=1, candidate_id=mention.candidates[0].id)
    elif action == "defer":
        decision = DeferDecision(action=action, expected_version=1)
    else:
        decision = ApproveCurrentDecision(
            action="approve_current", expected_version=1,
            scope="metadata" if action == "metadata_approve" else "identity",
        )
    with pytest.raises(ReviewConflictError) as conflict:
        apply_review_decision(db, mention.id, decision)
    assert conflict.value.code == "event_origin_required"
    db.rollback()
    assert (mention.shop_id, mention.review_status, mention.version) == (None, "rejected", 1)
    assert mention.metadata_review_status == "deferred"
    assert "出店元未特定" in mention.extraction_error
    assert len(mention.candidates) == 1
    assert db.query(Shop).count() == db.query(ReviewEvent).count() == 0


@pytest.mark.parametrize("changed_field", ["none", "area", "category", "normalization"])
def test_excluded_event_edit_requires_more_than_venue_or_classification_values(
    db: Session, changed_field: str,
) -> None:
    mention = create_excluded_event(db)
    editable = EditableShop(
        shop_name="  TEMPORARY Venue  " if changed_field == "normalization" else mention.extracted_name,
        branch_name=mention.extracted_branch_name,
        area="神田" if changed_field == "area" else "銀座",
        category="和食" if changed_field == "category" else "寿司",
    )
    with pytest.raises(ReviewConflictError) as conflict:
        apply_review_decision(db, mention.id, EditAndApproveDecision(
            action="edit_and_approve", expected_version=1, shop=editable,
        ))
    assert conflict.value.code == "event_origin_required"
    db.rollback()
    assert mention.shop_id is None and mention.review_status == "rejected"
    assert db.query(Shop).count() == db.query(ReviewEvent).count() == 0


@pytest.mark.parametrize("origin_field", ["shop_name", "branch_name", "address", "phone", "canonical_url"])
def test_excluded_event_allows_explicit_manual_origin_details(db: Session, origin_field: str) -> None:
    mention = create_excluded_event(db)
    editable = EditableShop(
        shop_name="Verified permanent shop" if origin_field == "shop_name" else mention.extracted_name,
        branch_name="本店" if origin_field == "branch_name" else mention.extracted_branch_name,
        area="銀座", category="寿司",
        address="Verified permanent address" if origin_field == "address" else None,
        phone="03-1234-5678" if origin_field == "phone" else None,
        canonical_url="https://official.example/origin" if origin_field == "canonical_url" else None,
    )
    result = apply_review_decision(db, mention.id, EditAndApproveDecision(
        action="edit_and_approve", expected_version=1, shop=editable,
    ))
    assert result.review_status == "approved"
    assert mention.shop is not None
    assert mention.message.content == "A restaurant recommendation"
    assert "出店元未特定" in mention.extraction_error
    assert db.query(ReviewEvent).count() == 1


def test_excluded_event_can_be_explicitly_linked_to_existing_permanent_shop(db: Session) -> None:
    mention = create_excluded_event(db)
    target = Shop(shop_name="Verified permanent shop", area="銀座", category="寿司", memo="Keep memo", rating=4)
    db.add(target)
    db.commit()
    result = apply_review_decision(db, mention.id, MergeDecision(
        action="merge", expected_version=1, target_shop_id=target.id, target_version=1,
        is_visited=False, memo=target.memo, rating=target.rating,
    ))
    assert result.shop_id == target.id and result.review_status == "approved"
    assert db.query(Shop).count() == 1
    assert target.memo == "Keep memo" and target.rating == 4
    assert mention.message.content == "A restaurant recommendation"


@pytest.mark.parametrize("action", ["approve_current", "approve_candidate"])
def test_event_origin_guard_does_not_block_already_linked_shops(db: Session, action: str) -> None:
    mention = create_excluded_event(db)
    mention.shop = Shop(shop_name="Existing permanent shop", area="銀座", category="寿司")
    db.commit()
    shop_id = mention.shop_id
    decision = (
        ApproveCandidateDecision(action="approve_candidate", expected_version=1, shop_version=1, candidate_id=mention.candidates[0].id)
        if action == "approve_candidate"
        else ApproveCurrentDecision(action="approve_current", expected_version=1, shop_version=1)
    )
    result = apply_review_decision(db, mention.id, decision)
    assert result.review_status == "approved" and result.shop_id == shop_id
    assert db.query(Shop).count() == 1


def test_identity_approve_current_creates_shop_from_extracted_values(db: Session) -> None:
    mention = create_shopless_mention(db, 10)

    result = apply_review_decision(
        db,
        mention.id,
        ApproveCurrentDecision(action="approve_current", expected_version=1),
    )

    assert result.scope == ReviewScope.IDENTITY.value
    assert result.review_status == ReviewStatus.APPROVED.value
    assert result.metadata_review_status == MetadataReviewStatus.PENDING.value
    shop = db.query(Shop).filter(Shop.id == result.shop_id).one()
    assert shop.shop_name == "Central Noodles"
    assert shop.branch_name == "East Exit"
    assert shop.area is None
    assert shop.category == "Ramen"
    assert mention.extracted_area == "Tokyo"
    assert mention.metadata_difference_type == "unknown_area"
    event_row = db.query(ReviewEvent).filter(ReviewEvent.mention_id == mention.id).one()
    assert event_row.scope == ReviewScope.IDENTITY.value


def test_shopless_approve_current_rejects_name_area_collision(db: Session) -> None:
    existing = Shop(
        shop_name="Central Noodles",
        branch_name="East Exit",
        area="中目黒",
        category="Ramen",
    )
    mention = create_shopless_mention(db, 30)
    mention.extracted_area = "中目黒駅"
    db.add(existing)
    db.commit()
    shop_count = db.query(Shop).count()
    event_count = db.query(ReviewEvent).count()

    with pytest.raises(ReviewConflictError, match="kind=name_area"):
        apply_review_decision(
            db,
            mention.id,
            ApproveCurrentDecision(action="approve_current", expected_version=1),
        )

    assert db.query(Shop).count() == shop_count
    assert db.query(ReviewEvent).count() == event_count
    assert mention.shop_id is None
    assert mention.review_status == ReviewStatus.PENDING.value


def test_shopless_approve_candidate_rejects_normalized_url_collision(
    db: Session,
) -> None:
    existing = Shop(
        shop_name="Existing Restaurant",
        area="銀座",
        category="Ramen",
        canonical_url="HTTPS://Example.COM:443/shops/noodles/#evidence",
    )
    mention = create_shopless_mention(db, 31)
    mention.extracted_branch_name = None
    mention.extracted_area = "銀座"
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="Candidate Noodles",
        area="銀座",
        category="Ramen",
        canonical_url="https://example.com/shops/noodles/",
        name_similarity_milli=900,
    )
    db.add_all([existing, candidate])
    db.commit()
    shop_count = db.query(Shop).count()
    event_count = db.query(ReviewEvent).count()

    with pytest.raises(ReviewConflictError, match="kind=canonical_url"):
        apply_review_decision(
            db,
            mention.id,
            ApproveCandidateDecision(
                action="approve_candidate",
                expected_version=1,
                candidate_id=candidate.id,
            ),
        )

    assert db.query(Shop).count() == shop_count
    assert db.query(ReviewEvent).count() == event_count
    assert mention.shop_id is None
    assert mention.review_status == ReviewStatus.PENDING.value


def test_shopless_approve_candidate_rejects_evidence_url_collision(
    db: Session,
) -> None:
    existing = Shop(
        shop_name="Existing Restaurant",
        area="銀座",
        canonical_url="https://official.example/shared-shop",
    )
    mention = create_shopless_mention(db, 38)
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="Candidate Restaurant",
        area="銀座",
        evidence_url="https://official.example/shared-shop/",
        name_similarity_milli=900,
    )
    db.add_all((existing, candidate))
    db.commit()

    with pytest.raises(ReviewConflictError, match="kind=canonical_url"):
        apply_review_decision(
            db,
            mention.id,
            ApproveCandidateDecision(
                action="approve_candidate",
                expected_version=1,
                candidate_id=candidate.id,
            ),
        )

    assert mention.shop_id is None
    assert mention.review_status == ReviewStatus.PENDING.value


def test_shopless_approve_candidate_rejects_conflicting_service_ids(
    db: Session,
) -> None:
    mention = create_shopless_mention(db, 39)
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="Candidate Restaurant",
        area="銀座",
        canonical_url="https://tabelog.com/tokyo/A1301/A130101/13000001/",
        evidence_url="https://tabelog.com/tokyo/A1301/A130101/13000001/dtlmenu/",
        external_source="食べログ",
        external_id="13000002",
        name_similarity_milli=900,
    )
    db.add(candidate)
    db.commit()

    with pytest.raises(ReviewConflictError, match="external IDs conflict"):
        apply_review_decision(
            db,
            mention.id,
            ApproveCandidateDecision(
                action="approve_candidate",
                expected_version=1,
                candidate_id=candidate.id,
            ),
        )

    assert mention.shop_id is None


def test_shopless_approve_candidate_stores_identity_from_evidence_url(
    db: Session,
) -> None:
    mention = create_shopless_mention(db, 40)
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="Candidate Restaurant",
        area="銀座",
        canonical_url="https://official.example/candidate",
        evidence_url="https://tabelog.com/tokyo/A1301/A130101/13000003/dtlmenu/",
        name_similarity_milli=900,
    )
    db.add(candidate)
    db.commit()

    result = apply_review_decision(
        db,
        mention.id,
        ApproveCandidateDecision(
            action="approve_candidate",
            expected_version=1,
            candidate_id=candidate.id,
        ),
    )

    shop = db.query(Shop).filter(Shop.id == result.shop_id).one()
    assert shop.external_source == "tabelog"
    assert shop.external_id == "13000003"


def test_existing_shop_approve_candidate_rejects_another_shop_url(
    db: Session,
) -> None:
    mention = create_mention(db, 41, "Source Restaurant")
    target = Shop(
        shop_name="Target Restaurant",
        area="銀座",
        canonical_url="https://official.example/target",
    )
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="Target Restaurant",
        area="銀座",
        canonical_url="https://official.example/target/",
        evidence_url="https://directory.example/target",
        name_similarity_milli=900,
    )
    db.add_all((target, candidate))
    db.commit()
    source_shop_id = mention.shop_id

    with pytest.raises(ReviewConflictError, match="kind=canonical_url"):
        apply_review_decision(
            db,
            mention.id,
            ApproveCandidateDecision(
                action="approve_candidate",
                expected_version=1,
                shop_version=1,
                candidate_id=candidate.id,
            ),
        )

    source = db.query(Shop).filter(Shop.id == source_shop_id).one()
    assert source.shop_name == "Source Restaurant"
    assert source.canonical_url is None
    assert db.query(Shop).count() == 2


def test_existing_shop_approve_candidate_checks_retained_area(db: Session) -> None:
    mention = create_mention(db, 49, "Source Restaurant")
    mention.extracted_area = "月島"
    target = Shop(shop_name="Target Restaurant", area="銀座")
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="Target Restaurant",
        area="月島",
        name_similarity_milli=900,
    )
    db.add_all((target, candidate))
    db.commit()
    source_shop_id = mention.shop_id

    with pytest.raises(ReviewConflictError, match="kind=name_area"):
        apply_review_decision(
            db,
            mention.id,
            ApproveCandidateDecision(
                action="approve_candidate",
                expected_version=1,
                shop_version=1,
                candidate_id=candidate.id,
            ),
        )

    source = db.query(Shop).filter(Shop.id == source_shop_id).one()
    assert source.shop_name == "Source Restaurant"
    assert source.area == "銀座"


def test_existing_shop_edit_rejects_another_shop_identity(db: Session) -> None:
    mention = create_mention(db, 42, "Source Restaurant")
    target = Shop(
        shop_name="Target Restaurant",
        area="銀座",
        external_source="tabelog",
        external_id="13000004",
    )
    db.add(target)
    db.commit()
    source_shop_id = mention.shop_id

    with pytest.raises(ReviewConflictError, match="kind=external_id"):
        apply_review_decision(
            db,
            mention.id,
            EditAndApproveDecision(
                action="edit_and_approve",
                expected_version=1,
                shop_version=1,
                shop=EditableShop(
                    shop_name="Target Restaurant",
                    area="銀座",
                    category="割烹",
                    canonical_url=(
                        "https://tabelog.com/tokyo/A1301/A130101/13000004/"
                    ),
                ),
            ),
        )

    source = db.query(Shop).filter(Shop.id == source_shop_id).one()
    assert source.shop_name == "Source Restaurant"
    assert source.canonical_url is None
    assert db.query(Shop).count() == 2


def test_shopless_edit_and_approve_rejects_external_id_collision(
    db: Session,
) -> None:
    existing = Shop(
        shop_name="Existing Tabelog Restaurant",
        area="銀座",
        category="Ramen",
        external_source="tabelog",
        external_id="13000001",
    )
    mention = create_shopless_mention(db, 32)
    db.add(existing)
    db.commit()
    shop_count = db.query(Shop).count()
    event_count = db.query(ReviewEvent).count()

    with pytest.raises(ReviewConflictError, match="kind=external_id"):
        apply_review_decision(
            db,
            mention.id,
            EditAndApproveDecision(
                action="edit_and_approve",
                expected_version=1,
                shop=EditableShop(
                    shop_name="Edited Noodles",
                    area="銀座",
                    category="Ramen",
                    canonical_url=(
                        "https://tabelog.com/tokyo/A1301/A130101/13000001/"
                    ),
                ),
            ),
        )

    assert db.query(Shop).count() == shop_count
    assert db.query(ReviewEvent).count() == event_count
    assert mention.shop_id is None
    assert mention.review_status == ReviewStatus.PENDING.value


def test_shopless_approve_candidate_rejects_strong_phone_collision(
    db: Session,
) -> None:
    existing = Shop(
        shop_name="abcdefghij",
        area="銀座",
        category="Ramen",
        phone="03-1234-5678",
    )
    mention = create_shopless_mention(db, 33)
    mention.extracted_branch_name = None
    mention.extracted_area = "月島"
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="abcdefghxy",
        area="月島",
        category="Ramen",
        phone="0312345678",
        name_similarity_milli=800,
    )
    db.add_all([existing, candidate])
    db.commit()
    shop_count = db.query(Shop).count()
    event_count = db.query(ReviewEvent).count()

    with pytest.raises(ReviewConflictError, match="kind=strong_identity"):
        apply_review_decision(
            db,
            mention.id,
            ApproveCandidateDecision(
                action="approve_candidate",
                expected_version=1,
                candidate_id=candidate.id,
            ),
        )

    assert db.query(Shop).count() == shop_count
    assert db.query(ReviewEvent).count() == event_count
    assert mention.shop_id is None
    assert mention.review_status == ReviewStatus.PENDING.value


def test_shopless_edit_and_approve_rejects_strong_address_collision(
    db: Session,
) -> None:
    existing = Shop(
        shop_name="abcdefghij",
        area="銀座",
        category="Ramen",
        address="東京都中央区銀座1丁目2番3号",
    )
    mention = create_shopless_mention(db, 34)
    db.add(existing)
    db.commit()
    shop_count = db.query(Shop).count()
    event_count = db.query(ReviewEvent).count()

    with pytest.raises(ReviewConflictError, match="kind=strong_identity"):
        apply_review_decision(
            db,
            mention.id,
            EditAndApproveDecision(
                action="edit_and_approve",
                expected_version=1,
                shop=EditableShop(
                    shop_name="abcdefghiX",
                    area="月島",
                    category="Ramen",
                    address="東京都中央区銀座1-2-3",
                ),
            ),
        )

    assert db.query(Shop).count() == shop_count
    assert db.query(ReviewEvent).count() == event_count
    assert mention.shop_id is None
    assert mention.review_status == ReviewStatus.PENDING.value


def test_identity_edit_and_merge_work_without_provisional_shop(db: Session) -> None:
    edited = create_shopless_mention(db, 11)
    edit_result = apply_review_decision(
        db,
        edited.id,
        EditAndApproveDecision(
            action="edit_and_approve",
            expected_version=1,
            shop=EditableShop(
                shop_name="Edited Noodles",
                area="月島",
                category="Ramen",
                address="1-2-3",
                phone="03-1234-5678",
                canonical_url=None,
            ),
        ),
    )
    edited_shop = db.query(Shop).filter(Shop.id == edit_result.shop_id).one()
    assert edited_shop.shop_name == "Edited Noodles"
    assert edited_shop.phone == "0312345678"

    merged = create_shopless_mention(db, 12)
    target = Shop(
        shop_name="Existing Noodles",
        area="Tokyo",
        category="Ramen",
        version=1,
    )
    db.add(target)
    db.commit()
    merge_result = apply_review_decision(
        db,
        merged.id,
        MergeDecision(
            action="merge",
            expected_version=1,
            target_shop_id=target.id,
            target_version=1,
            is_visited=False,
            visited_at=None,
            rating=None,
            memo=None,
        ),
    )
    assert merge_result.shop_id == target.id
    assert merge_result.review_status == ReviewStatus.APPROVED.value


def test_metadata_approve_edit_and_defer_do_not_change_identity_status(db: Session) -> None:
    approved = create_mention(db, 13, "Current Name")
    approved.metadata_review_status = MetadataReviewStatus.PENDING.value
    approved.metadata_difference_type = "unknown_category"
    db.commit()
    result = apply_review_decision(
        db,
        approved.id,
        ApproveCurrentDecision(
            action="approve_current",
            scope="metadata",
            expected_version=1,
            shop_version=1,
        ),
    )
    assert result.scope == ReviewScope.METADATA.value
    assert result.review_status == ReviewStatus.PENDING.value
    assert result.metadata_review_status == MetadataReviewStatus.APPROVED.value
    assert approved.metadata_difference_type is None
    event_row = db.query(ReviewEvent).filter(ReviewEvent.mention_id == approved.id).one()
    assert event_row.scope == ReviewScope.METADATA.value

    edited = create_mention(db, 14, "Current Name")
    edited.metadata_review_status = MetadataReviewStatus.PENDING.value
    db.commit()
    edit_result = apply_review_decision(
        db,
        edited.id,
        EditAndApproveDecision(
            action="edit_and_approve",
            scope="metadata",
            expected_version=1,
            shop_version=1,
            shop=EditableShop(
                shop_name="Current Name",
                area="月島",
                category="Sushi",
                address="4-5-6",
                phone="06-1234-5678",
                canonical_url=None,
            ),
        ),
    )
    assert edit_result.review_status == ReviewStatus.PENDING.value
    assert edit_result.metadata_review_status == MetadataReviewStatus.APPROVED.value
    assert edited.shop is not None
    assert edited.shop.area == "月島"
    assert edited.shop.phone == "0612345678"

    deferred = create_mention(db, 15)
    deferred.metadata_review_status = MetadataReviewStatus.PENDING.value
    db.commit()
    defer_result = apply_review_decision(
        db,
        deferred.id,
        DeferDecision(
            action="defer",
            scope="metadata",
            expected_version=1,
            note="Need category confirmation",
        ),
    )
    assert defer_result.review_status == ReviewStatus.PENDING.value
    assert defer_result.metadata_review_status == MetadataReviewStatus.DEFERRED.value


@pytest.mark.parametrize(
    "decision",
    [
        ApproveCandidateDecision(
            action="approve_candidate",
            scope="metadata",
            expected_version=1,
            candidate_id=1,
        ),
        MergeDecision(
            action="merge",
            scope="metadata",
            expected_version=1,
            target_shop_id=1,
            target_version=1,
            is_visited=False,
            visited_at=None,
            rating=None,
            memo=None,
        ),
        RejectDecision(
            action="reject",
            scope="metadata",
            expected_version=1,
        ),
    ],
)
def test_metadata_rejects_identity_actions(
    db: Session,
    decision: ApproveCandidateDecision | MergeDecision | RejectDecision,
) -> None:
    mention = create_mention(db, 16)

    with pytest.raises(ReviewInvalidDecisionError, match="not allowed"):
        apply_review_decision(db, mention.id, decision)

    assert mention.review_status == ReviewStatus.PENDING.value
    assert db.query(ReviewEvent).filter(ReviewEvent.mention_id == mention.id).count() == 0


def test_metadata_edit_rejects_shop_identity_changes(db: Session) -> None:
    mention = create_mention(db, 17, "Current Name")
    mention.metadata_review_status = MetadataReviewStatus.PENDING.value
    db.commit()

    with pytest.raises(ReviewInvalidDecisionError, match="field=shop_name"):
        apply_review_decision(
            db,
            mention.id,
            EditAndApproveDecision(
                action="edit_and_approve",
                scope="metadata",
                expected_version=1,
                shop_version=1,
                shop=EditableShop(
                    shop_name="Different Identity",
                    area="月島",
                    category="Sushi",
                    address=None,
                    phone=None,
                    canonical_url=None,
                ),
            ),
        )

    assert mention.shop is not None
    assert mention.shop.shop_name == "Current Name"
    assert mention.shop.area != "月島"
    assert mention.metadata_review_status == MetadataReviewStatus.PENDING.value


def test_metadata_edit_accepts_equivalent_canonical_url_identity(db: Session) -> None:
    mention = create_mention(db, 35, "Current Name")
    assert mention.shop is not None
    mention.shop.canonical_url = "HTTPS://EXAMPLE.COM:443/shop/#fragment"
    mention.metadata_review_status = MetadataReviewStatus.PENDING.value
    db.commit()

    result = apply_review_decision(
        db,
        mention.id,
        EditAndApproveDecision(
            action="edit_and_approve",
            scope="metadata",
            expected_version=1,
            shop_version=1,
            shop=EditableShop(
                shop_name="Current Name",
                area="銀座",
                category="割烹",
                address=None,
                phone=None,
                canonical_url="https://example.com/shop",
            ),
        ),
    )

    assert result.metadata_review_status == MetadataReviewStatus.APPROVED.value
    assert mention.shop.canonical_url == "HTTPS://EXAMPLE.COM:443/shop/#fragment"


def test_metadata_edit_rejects_strong_identity_collision(db: Session) -> None:
    mention = create_mention(db, 43, "Current Name")
    assert mention.shop is not None
    mention.metadata_review_status = MetadataReviewStatus.PENDING.value
    target = Shop(
        shop_name="Current Name",
        area="月島",
        phone="03-9999-9999",
    )
    db.add(target)
    db.commit()

    with pytest.raises(ReviewConflictError, match="kind=strong_identity") as conflict:
        apply_review_decision(
            db,
            mention.id,
            EditAndApproveDecision(
                action="edit_and_approve",
                scope="metadata",
                expected_version=1,
                shop_version=1,
                shop=EditableShop(
                    shop_name="Current Name",
                    area="銀座",
                    category="割烹",
                    address=None,
                    phone="03-9999-9999",
                    canonical_url=None,
                ),
            ),
        )

    assert conflict.value.code == "shop_collision"
    assert mention.shop.phone is None
    assert mention.metadata_review_status == MetadataReviewStatus.PENDING.value


def test_candidate_preserves_existing_values_and_reassesses_metadata(db: Session) -> None:
    mention = create_mention(db, 18, "Current Name")
    assert mention.shop is not None
    mention.shop.address = "Tokyo 1-2-3"
    mention.shop.phone = "0312345678"
    mention.shop.canonical_url = "https://example.com/current"
    mention.metadata_review_status = MetadataReviewStatus.PENDING.value
    mention.metadata_difference_type = "unknown_category"
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="Verified Name",
        area=None,
        category=None,
        address=None,
        phone=None,
        canonical_url=None,
        name_similarity_milli=950,
    )
    db.add(candidate)
    db.commit()

    result = apply_review_decision(
        db,
        mention.id,
        ApproveCandidateDecision(
            action="approve_candidate",
            expected_version=1,
            shop_version=1,
            candidate_id=candidate.id,
        ),
    )

    assert mention.shop.shop_name == "Verified Name"
    assert mention.shop.area == "銀座"
    assert mention.shop.category == "割烹"
    assert mention.shop.address == "Tokyo 1-2-3"
    assert mention.shop.phone == "0312345678"
    assert mention.shop.canonical_url == "https://example.com/current"
    assert result.metadata_review_status == MetadataReviewStatus.APPROVED.value
    assert mention.metadata_difference_type is None


def test_shopless_candidate_prefers_canonical_extracted_area(db: Session) -> None:
    mention = create_shopless_mention(db, 25)
    mention.extracted_name = "中目黒 焼肉はな"
    mention.extracted_branch_name = None
    mention.extracted_area = "中目黒駅"
    mention.extracted_category = "焼肉"
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="中目黒 焼肉はな",
        area="東京都目黒区上目黒",
        category="焼肉",
        address="東京都目黒区上目黒1-2-3",
        canonical_url="https://directory.example/nakameguro-yakiniku-hana",
        name_similarity_milli=1_000,
    )
    db.add(candidate)
    db.commit()

    result = apply_review_decision(
        db,
        mention.id,
        ApproveCandidateDecision(
            action="approve_candidate",
            expected_version=1,
            candidate_id=candidate.id,
        ),
    )

    assert mention.shop is not None
    assert mention.shop.area == "中目黒"
    assert result.metadata_review_status == MetadataReviewStatus.APPROVED.value
    assert mention.metadata_difference_type is None


@pytest.mark.parametrize(
    ("raw_area", "candidate_area", "expected_area"),
    [("台東区", "東京都台東区", "東京都台東区"), ("架空区", "東京都架空区", None)],
)
def test_shopless_candidate_classifies_municipalities_and_keeps_unknown_area(
    db: Session,
    raw_area: str,
    candidate_area: str,
    expected_area: str | None,
) -> None:
    mention = create_shopless_mention(db, 26)
    mention.extracted_name = "台東区 焼肉はな"
    mention.extracted_branch_name = None
    mention.extracted_area = raw_area
    mention.extracted_category = "焼肉"
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="台東区 焼肉はな",
        area=candidate_area,
        category="焼肉",
        canonical_url="https://directory.example/taito-yakiniku-hana",
        name_similarity_milli=1_000,
    )
    db.add(candidate)
    db.commit()

    result = apply_review_decision(
        db,
        mention.id,
        ApproveCandidateDecision(
            action="approve_candidate",
            expected_version=1,
            candidate_id=candidate.id,
        ),
    )

    assert mention.shop is not None
    assert mention.shop.area == expected_area
    assert mention.extracted_area == raw_area
    assert result.review_status == ReviewStatus.APPROVED.value
    assert result.metadata_review_status == (
        MetadataReviewStatus.APPROVED.value
        if expected_area is not None
        else MetadataReviewStatus.PENDING.value
    )
    assert mention.metadata_difference_type == (None if expected_area else "unknown_area")


def test_candidate_replaces_unmanaged_shop_area_with_canonical_extracted_area(
    db: Session,
) -> None:
    mention = create_mention(db, 27)
    assert mention.shop is not None
    mention.shop.area = "架空区"
    mention.extracted_area = "中目黒駅"
    mention.extracted_category = "焼肉"
    candidate = ResolutionCandidate(
        mention=mention,
        rank=1,
        name="中目黒 焼肉はな",
        area="東京都目黒区上目黒",
        category="焼肉",
        name_similarity_milli=1_000,
    )
    db.add(candidate)
    db.commit()

    result = apply_review_decision(
        db,
        mention.id,
        ApproveCandidateDecision(
            action="approve_candidate",
            expected_version=1,
            shop_version=1,
            candidate_id=candidate.id,
        ),
    )

    assert mention.shop.area == "中目黒"
    assert result.metadata_review_status == MetadataReviewStatus.APPROVED.value


def test_metadata_approval_requires_area_on_canonical_shop(db: Session) -> None:
    mention = create_mention(db, 28)
    assert mention.shop is not None
    mention.shop.area = None
    mention.extracted_area = "銀座"
    mention.metadata_review_status = MetadataReviewStatus.PENDING.value
    db.commit()

    with pytest.raises(ReviewInvalidDecisionError, match="area is missing"):
        apply_review_decision(
            db,
            mention.id,
            ApproveCurrentDecision(
                action="approve_current",
                scope="metadata",
                expected_version=1,
                shop_version=1,
            ),
        )

    assert mention.metadata_review_status == MetadataReviewStatus.PENDING.value


def test_metadata_approval_persists_canonical_area_alias(db: Session) -> None:
    mention = create_mention(db, 29)
    assert mention.shop is not None
    mention.shop.area = "中目黒駅"
    mention.extracted_area = "中目黒駅"
    mention.metadata_review_status = MetadataReviewStatus.PENDING.value
    db.commit()

    result = apply_review_decision(
        db,
        mention.id,
        ApproveCurrentDecision(
            action="approve_current",
            scope="metadata",
            expected_version=1,
            shop_version=1,
        ),
    )

    assert mention.shop.area == "中目黒"
    assert mention.shop.version == 2
    assert result.metadata_review_status == MetadataReviewStatus.APPROVED.value


@pytest.mark.parametrize(
    ("area", "category", "difference_type"),
    [
        (None, "割烹", "missing_area"),
        ("架空区", "割烹", "unknown_area"),
        ("銀座", None, "missing_category"),
        ("銀座", "Unknown", "unknown_category"),
    ],
)
def test_identity_approval_reopens_invalid_metadata(
    db: Session,
    area: str | None,
    category: str | None,
    difference_type: str,
) -> None:
    mention = create_mention(db, 19)
    assert mention.shop is not None
    mention.shop.area = area
    if area is None:
        mention.extracted_area = None
    mention.shop.category = category
    mention.metadata_review_status = MetadataReviewStatus.APPROVED.value
    db.commit()

    result = apply_review_decision(
        db,
        mention.id,
        ApproveCurrentDecision(
            action="approve_current", expected_version=1, shop_version=1
        ),
    )

    assert result.metadata_review_status == MetadataReviewStatus.PENDING.value
    assert mention.metadata_difference_type == difference_type
    assert mention.metadata_reviewed_at is None


def test_shopless_metadata_and_unresolved_identity_cannot_create_shop(
    db: Session,
) -> None:
    metadata_mention = create_shopless_mention(db, 20)
    with pytest.raises(ReviewConflictError, match="has no shop") as missing_shop:
        apply_review_decision(
            db,
            metadata_mention.id,
            ApproveCurrentDecision(
                action="approve_current",
                scope="metadata",
                expected_version=1,
            ),
        )

    assert missing_shop.value.code == "missing_shop"
    unresolved_mention = create_shopless_mention(db, 21)
    unresolved_mention.extracted_name = "（店舗名未特定）"
    unresolved_mention.difference_type = "extraction_not_found"
    db.commit()
    with pytest.raises(ReviewConflictError, match="cannot create a shop"):
        apply_review_decision(
            db,
            unresolved_mention.id,
            ApproveCurrentDecision(action="approve_current", expected_version=1),
        )

    assert db.query(Shop).count() == 0


def test_approve_current_rejects_stale_shop_version(db: Session) -> None:
    mention = create_mention(db, 22)
    assert mention.shop is not None
    mention.shop.version = 2
    db.commit()

    with pytest.raises(ReviewConflictError, match="Shop changed"):
        apply_review_decision(
            db,
            mention.id,
            ApproveCurrentDecision(
                action="approve_current",
                expected_version=1,
                shop_version=1,
            ),
        )

    assert mention.review_status == ReviewStatus.PENDING.value


def test_merge_target_version_conflict_requires_refreshing_target(db: Session) -> None:
    mention = create_mention(db, 90)
    target = Shop(shop_name="Another shop", area="神田", category="和食", version=2, memo="Keep latest memo")
    db.add(target)
    db.commit()
    source_id = mention.shop_id
    with pytest.raises(ReviewConflictError) as conflict:
        apply_review_decision(
            db, mention.id,
            MergeDecision(
                action="merge", expected_version=1, shop_version=1,
                target_shop_id=target.id, target_version=1,
                is_visited=False, memo="Stale memo",
            ),
        )
    assert conflict.value.code == "stale_merge_target"
    assert mention.shop_id == source_id
    assert target.memo == "Keep latest memo"
    assert db.query(Shop).count() == 2
    assert db.query(ReviewEvent).count() == 0


@pytest.mark.parametrize("same_mention", [True, False])
def test_simultaneous_review_saves_do_not_apply_a_stale_shared_shop_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_mention: bool,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'review-race.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as seed:
        first = create_mention(seed, 91, "Concurrent shop")
        first_id = first.id
        second = ShopMention(
            message=Message(message_id="82345678901234567", content="Another post"),
            shop=first.shop, occurrence_index=0, extracted_name="Concurrent shop",
            review_status="pending", metadata_review_status="pending", extraction_source="test",
        )
        seed.add(second)
        seed.commit()
        second_id = first_id if same_mention else second.id
        shop_id = first.shop_id

    first_loaded = Event()
    second_loaded = Event()
    original_load = review_service._load_source_shop_for_update

    def coordinated_load(session: Session, mention: ShopMention, version: int | None) -> Shop | None:
        shop = original_load(session, mention, version)
        if session.info["writer"] == "first":
            first_loaded.set()
            second_loaded.wait(timeout=1)
        else:
            second_loaded.set()
        return shop

    monkeypatch.setattr(review_service, "_load_source_shop_for_update", coordinated_load)

    def save(writer: str, mention_id: int) -> str:
        with factory(info={"writer": writer}) as session:
            try:
                apply_review_decision(
                    session, mention_id,
                    EditAndApproveDecision(
                        action="edit_and_approve", scope="metadata",
                        expected_version=1, shop_version=1,
                        shop=EditableShop(
                            shop_name="Concurrent shop", area="銀座", category="寿司", address=writer,
                        ),
                    ),
                )
            except ReviewConflictError as exc:
                session.rollback()
                return exc.code
            return "saved"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_save = pool.submit(save, "first", first_id)
            assert first_loaded.wait(timeout=5)
            second_save = pool.submit(save, "second", second_id)
            results = [first_save.result(timeout=10), second_save.result(timeout=10)]
        assert results == ["saved", "stale_mention" if same_mention else "stale_shop"]
        with factory() as verify:
            shop = verify.get(Shop, shop_id)
            assert shop is not None
            assert shop.address == "first"
            assert shop.version == 2
            assert verify.query(ReviewEvent).count() == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("changed_record", ["mention", "shop"])
def test_preloaded_review_objects_cannot_bypass_latest_database_versions(
    db: Session, changed_record: str,
) -> None:
    mention = create_mention(db, 92, "Cached shop")
    mention_id = mention.id
    shop = mention.shop
    assert shop is not None
    shop_id = shop.id
    assert mention.version == shop.version == 1
    with Session(bind=db.get_bind()) as other:
        if changed_record == "mention":
            updated_mention = other.get(ShopMention, mention_id)
            assert updated_mention is not None
            updated_mention.version = 2
            updated_mention.review_status = "deferred"
        else:
            updated_shop = other.get(Shop, shop_id)
            assert updated_shop is not None
            updated_shop.version = 2
            updated_shop.address = "Saved elsewhere"
        other.commit()

    with pytest.raises(ReviewConflictError) as conflict:
        apply_review_decision(
            db, mention_id,
            ApproveCurrentDecision(action="approve_current", expected_version=1, shop_version=1),
        )
    assert conflict.value.code == ("stale_mention" if changed_record == "mention" else "stale_shop")
    db.rollback()
    assert db.query(ReviewEvent).count() == 0
    assert mention.review_status == ("deferred" if changed_record == "mention" else "pending")
    assert shop.address == ("Saved elsewhere" if changed_record == "shop" else None)


def test_shop_mutations_reject_missing_or_stale_shop_versions(db: Session) -> None:
    candidate_mention = create_mention(db, 22)
    candidate = ResolutionCandidate(
        mention=candidate_mention,
        rank=1,
        name="Candidate",
        area="銀座",
        category="割烹",
        name_similarity_milli=950,
    )
    db.add(candidate)
    db.commit()
    with pytest.raises(ReviewConflictError, match="version is required"):
        apply_review_decision(
            db,
            candidate_mention.id,
            ApproveCandidateDecision(
                action="approve_candidate",
                expected_version=1,
                candidate_id=candidate.id,
            ),
        )

    edit_mention = create_mention(db, 23)
    with pytest.raises(ReviewConflictError, match="Shop changed"):
        apply_review_decision(
            db,
            edit_mention.id,
            EditAndApproveDecision(
                action="edit_and_approve",
                expected_version=1,
                shop_version=2,
                shop=EditableShop(
                    shop_name="Edited",
                    area="銀座",
                    category="割烹",
                ),
            ),
        )

    metadata_mention = create_mention(db, 24)
    with pytest.raises(ReviewConflictError, match="Shop changed"):
        apply_review_decision(
            db,
            metadata_mention.id,
            EditAndApproveDecision(
                action="edit_and_approve",
                scope="metadata",
                expected_version=1,
                shop_version=2,
                shop=EditableShop(
                    shop_name=metadata_mention.shop.shop_name,
                    area="銀座",
                    category="割烹",
                ),
            ),
        )

    reject_mention = create_mention(db, 25)
    with pytest.raises(ReviewConflictError, match="Shop changed"):
        apply_review_decision(
            db,
            reject_mention.id,
            RejectDecision(
                action="reject",
                expected_version=1,
                shop_version=2,
            ),
        )

    merge_mention = create_mention(db, 26)
    target = Shop(shop_name="Target", area="銀座", category="割烹")
    db.add(target)
    db.commit()
    with pytest.raises(ReviewConflictError, match="Shop changed"):
        apply_review_decision(
            db,
            merge_mention.id,
            MergeDecision(
                action="merge",
                expected_version=1,
                shop_version=2,
                target_shop_id=target.id,
                target_version=1,
                is_visited=False,
            ),
        )
