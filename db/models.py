from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, relationship, validates


Base = declarative_base()


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    blocked_until: Mapped[int] = mapped_column(BigInteger, nullable=False)


MESSAGE_ID_RE = re.compile(r"^[0-9]{17,20}$")
IMAGE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_image_key(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not IMAGE_KEY_RE.fullmatch(value):
        raise ValueError("image_key must be an exact lowercase 64-digit SHA-256 hash")
    return value


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IGNORED = "ignored"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    INVALID = "invalid"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class MetadataReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DEFERRED = "deferred"


class ReviewScope(StrEnum):
    IDENTITY = "identity"
    METADATA = "metadata"


class CandidateProvenance(StrEnum):
    POSTED_URL = "posted_url"
    STRUCTURED_DATA = "structured_data"
    WEB_SEARCH = "web_search"
    IMAGE = "image"


class ResolutionMethod(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class ResolutionBasis(StrEnum):
    SOURCE_REUSE = "source_reuse"
    EXISTING_SHOP = "existing_shop"
    VERIFIED_CANDIDATE = "verified_candidate"
    VERIFIED_COLLISION = "verified_collision"
    IMAGE_EVIDENCE = "image_evidence"


class AssetKind(StrEnum):
    LINK = "link"
    EMBED = "embed"
    IMAGE = "image"
    ATTACHMENT = "attachment"


class FetchStatus(StrEnum):
    PENDING = "pending"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ImportStatus(StrEnum):
    VALIDATED = "validated"
    APPLIED = "applied"
    FAILED = "failed"


class Message(Base):
    __tablename__ = "messages"

    message_id = Column(String(20), primary_key=True)
    channel_id = Column(String(20), nullable=True, index=True)
    content = Column(Text, nullable=True)
    source_created_at = Column(DateTime(timezone=True), nullable=True)
    is_target = Column(Boolean, nullable=False, default=True)
    processing_status = Column(
        String(16), nullable=False, default=ProcessingStatus.PENDING.value, index=True
    )
    fetch_error = Column(Text, nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    assets = relationship("SourceAsset", back_populates="message", cascade="all, delete-orphan")
    mentions = relationship("ShopMention", back_populates="message", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("length(message_id) BETWEEN 17 AND 20", name="ck_messages_id_length"),
        CheckConstraint(
            "processing_status IN ('pending','processing','succeeded','failed','ignored')",
            name="ck_messages_processing_status",
        ),
    )

    @validates("message_id")
    def validate_message_id(self, _key: str, value: str) -> str:
        if not MESSAGE_ID_RE.fullmatch(value):
            raise ValueError("message_id must be an exact 17-20 digit string")
        return value


class Shop(Base):
    __tablename__ = "shops"

    id = Column(Integer, primary_key=True, autoincrement=True)
    shop_name = Column(String, nullable=False)
    branch_name = Column(String(255), nullable=True)
    area = Column(String, nullable=True)
    category = Column(String, nullable=True)
    address = Column(Text, nullable=True)
    phone = Column(String(32), nullable=True)
    canonical_url = Column(Text, nullable=True)
    image_key = Column(String(64), nullable=True, index=True)
    external_source = Column(String(64), nullable=True)
    external_id = Column(String(255), nullable=True)
    is_visited = Column(Boolean, nullable=False, default=False)
    visited_at = Column(DateTime(timezone=True), nullable=True)
    rating = Column(Integer, nullable=True)
    memo = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    version = Column(Integer, nullable=False, default=1)

    mentions = relationship("ShopMention", back_populates="shop")

    __table_args__ = (
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="ck_shops_rating"),
        CheckConstraint(
            "(external_source IS NULL AND external_id IS NULL) OR "
            "(external_source IS NOT NULL AND external_id IS NOT NULL)",
            name="ck_shops_external_identity_pair",
        ),
        UniqueConstraint("external_source", "external_id", name="uq_shops_external_identity"),
        Index("ix_shops_phone", "phone"),
    )

    @validates("image_key")
    def validate_stored_image_key(self, _key: str, value: object) -> str | None:
        return validate_image_key(value)

    def primary_mention(self) -> "ShopMention | None":
        approved = [
            mention
            for mention in self.mentions
            if mention.review_status == ReviewStatus.APPROVED.value
        ]
        candidates = approved or list(self.mentions)
        ordered = prefer_reparsed_duplicate(
            sorted(candidates, key=lambda mention: mention.id or 0)
        )
        return ordered[0] if ordered else None

    @property
    def message_id(self) -> str | None:
        mention = self.primary_mention()
        return mention.message_id if mention else getattr(self, "_legacy_message_id", None)

    @message_id.setter
    def message_id(self, value: str | None) -> None:
        self._legacy_message_id = value

    @property
    def url(self) -> str | None:
        mention = self.primary_mention()
        return self.canonical_url or (mention.source_url if mention else None)

    @url.setter
    def url(self, value: str | None) -> None:
        self.canonical_url = value

    @property
    def needs_review(self) -> bool:
        return any(
            mention.review_status in {ReviewStatus.PENDING.value, ReviewStatus.DEFERRED.value}
            or mention.metadata_review_status
            in {MetadataReviewStatus.PENDING.value, MetadataReviewStatus.DEFERRED.value}
            for mention in self.mentions
        )

    @property
    def extraction_source(self) -> str | None:
        mention = self.primary_mention()
        return mention.extraction_source if mention else None

    @property
    def extraction_error(self) -> str | None:
        mention = self.primary_mention()
        return mention.extraction_error if mention else None

    @property
    def confidence_reason(self) -> str | None:
        mention = self.primary_mention()
        return mention.confidence_reason if mention else None


class SourceAsset(Base):
    __tablename__ = "source_assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String(20), ForeignKey("messages.message_id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(16), nullable=False)
    url = Column(Text, nullable=False)
    title = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    mime_type = Column(String(255), nullable=True)
    extracted_text = Column(Text, nullable=True)
    source_service = Column(String(32), nullable=True)
    source_item_id = Column(String(255), nullable=True)
    normalized_url = Column(Text, nullable=True)
    content_fingerprint = Column(String(64), nullable=True)
    fetch_status = Column(String(16), nullable=False, default=FetchStatus.PENDING.value)
    fetch_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    message = relationship("Message", back_populates="assets")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('link','embed','image','attachment')", name="ck_source_assets_kind"
        ),
        CheckConstraint(
            "fetch_status IN ('pending','available','unavailable')",
            name="ck_source_assets_fetch_status",
        ),
        CheckConstraint(
            "(source_service IS NULL AND source_item_id IS NULL) OR "
            "(source_service IS NOT NULL AND source_item_id IS NOT NULL)",
            name="ck_source_assets_identity_pair",
        ),
        UniqueConstraint("message_id", "kind", "url", name="uq_source_assets_message_kind_url"),
        Index(
            "ix_source_assets_source_identity",
            "source_service",
            "source_item_id",
            "normalized_url",
        ),
        Index("ix_source_assets_normalized_url", "normalized_url"),
        Index("ix_source_assets_content_fingerprint", "content_fingerprint"),
    )


class ShopMention(Base):
    __tablename__ = "shop_mentions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String(20), ForeignKey("messages.message_id", ondelete="CASCADE"), nullable=False)
    shop_id = Column(Integer, ForeignKey("shops.id", ondelete="SET NULL"), nullable=True, index=True)
    occurrence_index = Column(Integer, nullable=False)
    extracted_name = Column(String, nullable=False)
    extracted_branch_name = Column(String(255), nullable=True)
    extracted_area = Column(String, nullable=True)
    extracted_category = Column(String, nullable=True)
    source_url = Column(Text, nullable=True)
    resolution_status = Column(
        String(16), nullable=False, default=ResolutionStatus.AMBIGUOUS.value, index=True
    )
    review_status = Column(String(16), nullable=False, default=ReviewStatus.PENDING.value, index=True)
    metadata_review_status = Column(
        String(16),
        nullable=False,
        default=MetadataReviewStatus.APPROVED.value,
        index=True,
    )
    metadata_difference_type = Column(String(64), nullable=True, index=True)
    resolution_method = Column(String(16), nullable=True)
    difference_type = Column(String(64), nullable=True, index=True)
    extraction_source = Column(String(64), nullable=False, default="legacy_import")
    extraction_error = Column(Text, nullable=True)
    confidence_reason = Column(Text, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    metadata_reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reused_from_mention_id = Column(
        Integer,
        ForeignKey(
            "shop_mentions.id",
            ondelete="SET NULL",
            name="fk_shop_mentions_reused_from",
        ),
        nullable=True,
        index=True,
    )
    resolution_basis = Column(String(32), nullable=True, index=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    message = relationship("Message", back_populates="mentions")
    shop = relationship("Shop", back_populates="mentions")
    candidates = relationship(
        "ResolutionCandidate", back_populates="mention", cascade="all, delete-orphan"
    )
    review_events = relationship("ReviewEvent", back_populates="mention", cascade="all, delete-orphan")
    reused_from_mention = relationship(
        "ShopMention",
        remote_side=[id],
        foreign_keys=[reused_from_mention_id],
    )

    __table_args__ = (
        UniqueConstraint("message_id", "occurrence_index", name="uq_mentions_message_occurrence"),
        CheckConstraint(
            "resolution_status IN ('resolved','ambiguous','not_found','invalid')",
            name="ck_mentions_resolution_status",
        ),
        CheckConstraint(
            "review_status IN ('pending','approved','rejected','deferred')",
            name="ck_mentions_review_status",
        ),
        CheckConstraint(
            "metadata_review_status IN ('pending','approved','deferred')",
            name="ck_mentions_metadata_review_status",
        ),
        CheckConstraint(
            "resolution_method IS NULL OR resolution_method IN ('automatic','manual')",
            name="ck_mentions_resolution_method",
        ),
        CheckConstraint(
            "resolution_basis IS NULL OR resolution_basis IN "
            "('source_reuse','existing_shop','verified_candidate',"
            "'verified_collision','image_evidence')",
            name="ck_mentions_resolution_basis",
        ),
    )


def prefer_reparsed_duplicate(mentions: list[ShopMention]) -> list[ShopMention]:
    """Replace an ordered legacy representative only with the same source."""
    if not mentions:
        return mentions
    first = mentions[0]
    if first.extraction_source != "legacy_import" or not first.source_url:
        return mentions
    for mention in mentions[1:]:
        if (
            mention.source_url == first.source_url
            and mention.extraction_source
            and mention.extraction_source != "legacy_import"
        ):
            return [mention, *(item for item in mentions if item is not mention)]
    return mentions


class ResolutionCandidate(Base):
    __tablename__ = "resolution_candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mention_id = Column(Integer, ForeignKey("shop_mentions.id", ondelete="CASCADE"), nullable=False)
    rank = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    area = Column(String, nullable=True)
    category = Column(String, nullable=True)
    address = Column(Text, nullable=True)
    phone = Column(String(32), nullable=True)
    canonical_url = Column(Text, nullable=True)
    external_source = Column(String(64), nullable=True)
    external_id = Column(String(255), nullable=True)
    evidence_url = Column(Text, nullable=True)
    provenance = Column(String(32), nullable=False, default=CandidateProvenance.WEB_SEARCH.value)
    is_verified = Column(Boolean, nullable=False, default=False)
    verification_reason = Column(Text, nullable=True)
    matched_fields = Column(Text, nullable=True)
    conflicting_fields = Column(Text, nullable=True)
    name_similarity_milli = Column(Integer, nullable=False, default=0)
    is_strong_match = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    mention = relationship("ShopMention", back_populates="candidates")

    __table_args__ = (
        CheckConstraint(
            "name_similarity_milli BETWEEN 0 AND 1000", name="ck_candidates_similarity"
        ),
        CheckConstraint(
            "provenance IN ('posted_url','structured_data','web_search','image')",
            name="ck_candidates_provenance",
        ),
        UniqueConstraint("mention_id", "rank", name="uq_candidates_mention_rank"),
    )


class SyncState(Base):
    __tablename__ = "sync_states"

    channel_id = Column(String(20), primary_key=True)
    last_contiguous_message_id = Column(String(20), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class ProcessingRun(Base):
    __tablename__ = "processing_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String(20), ForeignKey("messages.message_id", ondelete="SET NULL"), nullable=True)
    stage = Column(String(64), nullable=False)
    model = Column(String(128), nullable=True)
    prompt_version = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    web_search_calls = Column(Integer, nullable=False, default=0)
    image_count = Column(Integer, nullable=False, default=0)
    latency_ms = Column(Integer, nullable=False, default=0)
    estimated_cost_microusd = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','succeeded','failed','ignored')",
            name="ck_processing_runs_status",
        ),
    )


class LookupCache(Base):
    __tablename__ = "lookup_cache"

    cache_key = Column(String(64), primary_key=True)
    kind = Column(String(32), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class ReviewEvent(Base):
    __tablename__ = "review_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mention_id = Column(Integer, ForeignKey("shop_mentions.id", ondelete="CASCADE"), nullable=False)
    scope = Column(String(16), nullable=False, default=ReviewScope.IDENTITY.value)
    action = Column(String(32), nullable=False)
    previous_shop_id = Column(Integer, nullable=True)
    selected_shop_id = Column(Integer, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    mention = relationship("ShopMention", back_populates="review_events")

    __table_args__ = (
        CheckConstraint("scope IN ('identity','metadata')", name="ck_review_events_scope"),
    )


class ShopRedirect(Base):
    __tablename__ = "shop_redirects"

    source_shop_id = Column(Integer, primary_key=True)
    target_shop_id = Column(
        Integer,
        ForeignKey("shops.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reason = Column(String(64), nullable=False, default="merge")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    target = relationship("Shop")

    __table_args__ = (
        CheckConstraint("source_shop_id <> target_shop_id", name="ck_shop_redirects_distinct"),
    )


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id = Column(String(32), primary_key=True)
    filename = Column(String(255), nullable=False)
    sha256 = Column(String(64), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default=ImportStatus.VALIDATED.value)
    row_count = Column(Integer, nullable=False)
    message_count = Column(Integer, nullable=False)
    review_count = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)

    rows = relationship("ImportRow", back_populates="batch", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "status IN ('validated','applied','failed')", name="ck_import_batches_status"
        ),
    )


class ImportRow(Base):
    __tablename__ = "import_rows"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String(32), ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False)
    row_number = Column(Integer, nullable=False)
    shop_id = Column(Integer, nullable=False)
    message_id = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    shop_name = Column(String, nullable=False)
    branch_name = Column(String(255), nullable=True)
    area = Column(String, nullable=True)
    category = Column(String, nullable=True)
    address = Column(Text, nullable=True)
    phone = Column(String(32), nullable=True)
    external_source = Column(String(64), nullable=True)
    external_id = Column(String(255), nullable=True)
    is_visited = Column(Boolean, nullable=False)
    visited_at = Column(DateTime(timezone=True), nullable=True)
    rating = Column(Integer, nullable=True)
    memo = Column(Text, nullable=True)
    source_url = Column(Text, nullable=True)
    canonical_url = Column(Text, nullable=True)
    image_key = Column(String(64), nullable=True)
    needs_review = Column(Boolean, nullable=False)
    resolution_status = Column(String(16), nullable=False, default=ResolutionStatus.RESOLVED.value)
    review_status = Column(String(16), nullable=False, default=ReviewStatus.APPROVED.value)
    metadata_review_status = Column(
        String(16),
        nullable=False,
        default=MetadataReviewStatus.APPROVED.value,
    )
    resolution_method = Column(String(16), nullable=True)
    difference_type = Column(String(64), nullable=True)
    metadata_difference_type = Column(String(64), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    metadata_reviewed_at = Column(DateTime(timezone=True), nullable=True)
    extraction_source = Column(String(64), nullable=False)
    extraction_error = Column(Text, nullable=True)
    confidence_reason = Column(Text, nullable=True)

    batch = relationship("ImportBatch", back_populates="rows")

    @validates("image_key")
    def validate_stored_image_key(self, _key: str, value: object) -> str | None:
        return validate_image_key(value)

    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_import_rows_batch_row"),
        UniqueConstraint("batch_id", "shop_id", name="uq_import_rows_batch_shop"),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="ck_import_rows_rating"),
        CheckConstraint(
            "resolution_status IN ('resolved','ambiguous','not_found','invalid')",
            name="ck_import_rows_resolution_status",
        ),
        CheckConstraint(
            "review_status IN ('pending','approved','rejected','deferred')",
            name="ck_import_rows_review_status",
        ),
        CheckConstraint(
            "metadata_review_status IN ('pending','approved','deferred')",
            name="ck_import_rows_metadata_review_status",
        ),
        CheckConstraint(
            "resolution_method IS NULL OR resolution_method IN ('automatic','manual')",
            name="ck_import_rows_resolution_method",
        ),
    )
