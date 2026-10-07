from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from enum import StrEnum
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import exists, text, tuple_, update
from sqlalchemy.orm import Session

from db.models import (
    ImportBatch,
    ImportRow,
    ImportStatus,
    Message,
    MetadataReviewStatus,
    ProcessingRun,
    ProcessingStatus,
    ResolutionCandidate,
    ResolutionMethod,
    ResolutionStatus,
    ReviewEvent,
    ReviewStatus,
    Shop,
    ShopMention,
    ShopRedirect,
    SourceAsset,
    SyncState,
    utc_now,
    validate_image_key,
)
from services.resolution import normalize_external_identity
from services.source_identity import source_asset_fingerprint, source_asset_identity
from web.area_groups import canonicalize_area


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5_000
MESSAGE_ID_RE = re.compile(r"^[0-9]{17,20}$")
_CSV_INJECT_CHARS = frozenset("=+-@\t\r")
_GNAVI_STORE_ID_RE = re.compile(r"^(?=.*[0-9])[a-z0-9_-]{5,}$", re.IGNORECASE)

_SOCIAL_HOSTS = frozenset(
    {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "instagram.com",
        "www.instagram.com",
        "facebook.com",
        "www.facebook.com",
        "note.com",
    }
)


class ImportValidationFailure(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class CsvImportRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    row_number: int = Field(ge=2)
    shop_id: int = Field(gt=0)
    created_at: datetime
    message_id: str
    shop_name: str = Field(min_length=1, max_length=500)
    branch_name: str | None = Field(default=None, max_length=255)
    area: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=2_000)
    phone: str | None = Field(default=None, max_length=64)
    external_source: str | None = Field(default=None, max_length=64)
    external_id: str | None = Field(default=None, max_length=255)
    is_visited: bool
    visited_at: datetime | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    memo: str | None = Field(default=None, max_length=20_000)
    source_url: str | None = None
    canonical_url: str | None = None
    image_key: str | None = None
    needs_review: bool
    resolution_status: ResolutionStatus
    review_status: ReviewStatus
    metadata_review_status: MetadataReviewStatus
    resolution_method: ResolutionMethod | None = None
    difference_type: str | None = Field(default=None, max_length=64)
    metadata_difference_type: str | None = Field(default=None, max_length=64)
    reviewed_at: datetime | None = None
    metadata_reviewed_at: datetime | None = None
    extraction_source: str = Field(min_length=1, max_length=64)
    extraction_error: str | None = Field(default=None, max_length=20_000)
    confidence_reason: str | None = Field(default=None, max_length=20_000)

    @field_validator("message_id", mode="before")
    @classmethod
    def validate_message_id(cls, value: object) -> str:
        if not isinstance(value, str) or not MESSAGE_ID_RE.fullmatch(value):
            raise ValueError("message_id must be an exact 17-20 digit string")
        return value

    @field_validator("image_key", mode="before")
    @classmethod
    def validate_uploaded_image_key(cls, value: object) -> str | None:
        return validate_image_key(value)

    @field_validator("source_url", "canonical_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlparse(value)
            parsed.port
        except ValueError as exc:
            raise ValueError("URL has an invalid port") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("URL must use http or https")
        if parsed.username or parsed.password:
            raise ValueError("URL userinfo is not allowed")
        return value

    @model_validator(mode="after")
    def validate_external_identity(self) -> "CsvImportRow":
        if bool(self.external_source) != bool(self.external_id):
            raise ValueError("external_source and external_id must both be set or both be empty")
        return self


@dataclass(frozen=True)
class ParsedImport:
    filename: str
    sha256: str
    rows: tuple[CsvImportRow, ...]

    @property
    def message_count(self) -> int:
        return len({row.message_id for row in self.rows})

    @property
    def review_count(self) -> int:
        return sum(1 for row in self.rows if row.needs_review)


@dataclass(frozen=True)
class CsvUpdateChange:
    shop_id: int
    column: str
    previous: str
    proposed: str


@dataclass(frozen=True)
class ImportPreview:
    batch_id: str
    filename: str
    sha256: str
    row_count: int
    message_count: int
    review_count: int
    inserted: int
    updated: int
    deleted: int
    changes: tuple[CsvUpdateChange, ...] = ()


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    is_export_escape = (
        len(value) > 1 and value[0] == "'" and value[1] in _CSV_INJECT_CHARS
    )
    decoded = value[1:] if is_export_escape else value
    stripped = decoded.strip()
    return stripped or None


def _format_validation_error(error: ValidationError) -> str:
    messages: list[str] = []
    for detail in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ".".join(str(part) for part in detail["loc"])
        messages.append(f"{location}: {detail['msg']}")
    return ", ".join(messages)


def _parse_required_bool(value: str | None, field_name: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{field_name} must be an explicit boolean")


EnumValue = TypeVar("EnumValue", bound=StrEnum)


def _parse_enum(
    value: str | None,
    field_name: str,
    enum_type: type[EnumValue],
    default: EnumValue,
) -> EnumValue:
    normalized = _blank_to_none(value)
    if normalized is None:
        return default
    try:
        return enum_type(normalized)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}") from exc


def _parse_resolution_method(value: str | None) -> ResolutionMethod | None:
    normalized = _blank_to_none(value)
    if normalized is None:
        return None
    try:
        return ResolutionMethod(normalized)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ResolutionMethod)
        raise ValueError(f"resolution_method must be one of: {allowed}") from exc


def _parse_datetime(value: str | None, field_name: str, required: bool) -> datetime | None:
    normalized = (value or "").strip()
    if not normalized:
        if required:
            raise ValueError(f"{field_name} is required")
        return None

    candidates = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    )
    for candidate in candidates:
        try:
            parsed = datetime.strptime(normalized.replace("Z", "+0000"), candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue

    try:
        parsed_date = date.fromisoformat(normalized)
        return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"{field_name} has an unsupported datetime format") from exc


def _parse_rating(value: str | None) -> int | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    if not normalized.isdigit():
        raise ValueError("rating must be an integer from 1 to 5")
    rating = int(normalized)
    if not 1 <= rating <= 5:
        raise ValueError("rating must be an integer from 1 to 5")
    return rating


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _is_known_canonical_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path.rstrip("/")
    if _host_matches(host, "tabelog.com"):
        return bool(re.search(r"/[0-9]{6,}(?:/|$)", f"{path}/"))
    if _host_matches(host, "hotpepper.jp"):
        return "/str" in path
    if _host_matches(host, "gnavi.co.jp"):
        return bool(path and path != "/")
    if _host_matches(host, "ikyu.com"):
        return "/restaurant/" in path
    if _host_matches(host, "retty.me"):
        return "/area/" in path and "/stores/" in path
    return False


def classify_legacy_url(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host in _SOCIAL_HOSTS:
        return url, None
    if _is_known_canonical_url(url):
        return None, url
    return url, None


def extract_external_identity(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path.rstrip("/")
    if _host_matches(host, "tabelog.com"):
        match = re.search(r"/([0-9]{6,})(?:/|$)", f"{path}/")
        return ("tabelog", match.group(1)) if match else (None, None)
    if _host_matches(host, "hotpepper.jp"):
        match = re.search(r"/(str[^/]+)(?:/|$)", f"{path}/", re.IGNORECASE)
        return ("hotpepper", match.group(1).lower()) if match else (None, None)
    if _host_matches(host, "gnavi.co.jp") and path:
        shop_id = path.strip("/").split("/", 1)[0]
        return (
            ("gnavi", shop_id.casefold())
            if _GNAVI_STORE_ID_RE.fullmatch(shop_id)
            else (None, None)
        )
    if _host_matches(host, "ikyu.com"):
        match = re.search(r"/restaurant/([^/]+)", path)
        return ("ikyu", match.group(1)) if match else (None, None)
    if _host_matches(host, "retty.me"):
        match = re.search(r"/stores/([^/]+)", path, re.IGNORECASE)
        return ("retty", match.group(1).casefold()) if match else (None, None)
    return None, None


def collect_external_identities(
    *,
    external_source: str | None,
    external_id: str | None,
    urls: tuple[str | None, ...] = (),
) -> frozenset[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    explicit = normalize_external_identity(external_source, external_id)
    if explicit is not None:
        identities.add(explicit)
    for url in urls:
        source, url_id = extract_external_identity(url)
        identity = normalize_external_identity(source, url_id)
        if identity is not None:
            identities.add(identity)
    return frozenset(identities)


def external_identities_conflict(
    identities: frozenset[tuple[str, str]],
) -> bool:
    ids_by_source: dict[str, set[str]] = defaultdict(set)
    for source, external_id in identities:
        ids_by_source[source].add(external_id)
    return any(len(external_ids) > 1 for external_ids in ids_by_source.values())


def preferred_external_identity(
    *,
    external_source: str | None,
    external_id: str | None,
    urls: tuple[str | None, ...] = (),
) -> tuple[str | None, str | None]:
    explicit = normalize_external_identity(external_source, external_id)
    if explicit is not None:
        return explicit
    for url in urls:
        source, url_id = extract_external_identity(url)
        identity = normalize_external_identity(source, url_id)
        if identity is not None:
            return identity
    return None, None


def parse_csv_bytes(raw: bytes, filename: str) -> ParsedImport:
    errors: list[str] = []
    if len(raw) > MAX_FILE_BYTES:
        raise ImportValidationFailure([f"ファイルサイズは最大{MAX_FILE_BYTES // 1024 // 1024}MBです。"])
    if not filename.lower().endswith(".csv"):
        raise ImportValidationFailure(["CSVファイルを指定してください。"])

    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportValidationFailure([f"UTF-8として読み込めません: {exc}"]) from exc

    reader = csv.DictReader(io.StringIO(decoded))
    fieldnames = set(reader.fieldnames or [])
    required = {
        "_id",
        "@timestamp",
        "message_id",
        "shop.name",
        "status.is_visited",
        "needs_review",
    }
    missing = sorted(required - fieldnames)
    if missing:
        raise ImportValidationFailure([f"必須列がありません: {', '.join(missing)}"])

    source_rows = list(reader)
    if not source_rows:
        raise ImportValidationFailure(["CSVにデータ行がありません。"])
    if len(source_rows) > MAX_ROWS:
        raise ImportValidationFailure([f"行数は最大{MAX_ROWS:,}行です。"])

    parsed_rows: list[CsvImportRow] = []
    legacy_urls: dict[int, str | None] = {}
    has_explicit_source = "source_url" in fieldnames
    has_explicit_canonical = bool(
        {"canonical_url", "shop.canonical_url"} & fieldnames
    )
    has_explicit_external_identity = bool(
        {"shop.external_source", "shop.external_id"} & fieldnames
    )
    has_explicit_review = "review_status" in fieldnames
    has_explicit_reviewed_at = "reviewed_at" in fieldnames
    has_explicit_metadata_reviewed_at = "metadata_reviewed_at" in fieldnames
    for row_number, source in enumerate(source_rows, start=2):
        try:
            raw_shop_id = (source.get("_id") or "").strip()
            if not raw_shop_id.isdigit():
                raise ValueError("_id must be a positive integer")

            explicit_source = _blank_to_none(source.get("source_url"))
            explicit_canonical = _blank_to_none(
                source.get("canonical_url") or source.get("shop.canonical_url")
            )
            legacy_url = _blank_to_none(source.get("url"))
            classified_source, classified_canonical = classify_legacy_url(legacy_url)
            source_url = explicit_source if has_explicit_source else classified_source
            canonical_url = (
                explicit_canonical if has_explicit_canonical else classified_canonical
            )

            extraction_source = _blank_to_none(source.get("extraction_source")) or "legacy_import"
            created_at = _parse_datetime(
                source.get("@timestamp"),
                "@timestamp",
                required=True,
            )
            if created_at is None:
                raise ValueError("@timestamp is required")
            legacy_needs_review = _parse_required_bool(
                source.get("needs_review"),
                "needs_review",
            )
            review_status = _parse_enum(
                source.get("review_status"),
                "review_status",
                ReviewStatus,
                ReviewStatus.PENDING if legacy_needs_review else ReviewStatus.APPROVED,
            )
            metadata_review_status = _parse_enum(
                source.get("metadata_review_status"),
                "metadata_review_status",
                MetadataReviewStatus,
                MetadataReviewStatus.APPROVED,
            )
            resolution_status = _parse_enum(
                source.get("resolution_status"),
                "resolution_status",
                ResolutionStatus,
                (
                    ResolutionStatus.AMBIGUOUS
                    if review_status in {ReviewStatus.PENDING, ReviewStatus.DEFERRED}
                    else ResolutionStatus.RESOLVED
                ),
            )
            resolution_method = _parse_resolution_method(source.get("resolution_method"))
            if "resolution_method" not in fieldnames:
                resolution_method = (
                    ResolutionMethod.MANUAL
                    if review_status == ReviewStatus.APPROVED
                    else None
                )
            reviewed_at = _parse_datetime(
                source.get("reviewed_at"),
                "reviewed_at",
                required=False,
            )
            if not has_explicit_reviewed_at and review_status == ReviewStatus.APPROVED:
                reviewed_at = created_at
            metadata_reviewed_at = _parse_datetime(
                source.get("metadata_reviewed_at"),
                "metadata_reviewed_at",
                required=False,
            )
            if (
                not has_explicit_metadata_reviewed_at
                and metadata_review_status == MetadataReviewStatus.APPROVED
            ):
                metadata_reviewed_at = created_at
            needs_review = (
                review_status in {ReviewStatus.PENDING, ReviewStatus.DEFERRED}
                or metadata_review_status
                in {MetadataReviewStatus.PENDING, MetadataReviewStatus.DEFERRED}
            )
            row = CsvImportRow(
                row_number=row_number,
                shop_id=int(raw_shop_id),
                created_at=created_at,
                message_id=source.get("message_id") or "",
                shop_name=_blank_to_none(source.get("shop.name")) or "",
                branch_name=_blank_to_none(source.get("shop.branch_name")),
                area=_blank_to_none(source.get("shop.area")),
                category=_blank_to_none(source.get("shop.category")),
                address=_blank_to_none(source.get("shop.address")),
                phone=_blank_to_none(source.get("shop.phone")),
                external_source=_blank_to_none(source.get("shop.external_source")),
                external_id=_blank_to_none(source.get("shop.external_id")),
                is_visited=_parse_required_bool(
                    source.get("status.is_visited"), "status.is_visited"
                ),
                visited_at=_parse_datetime(source.get("visited_at"), "visited_at", required=False),
                rating=_parse_rating(source.get("rating")),
                memo=_blank_to_none(source.get("memo")),
                source_url=source_url,
                canonical_url=canonical_url,
                image_key=source.get("shop.image_key") or None,
                needs_review=needs_review,
                resolution_status=resolution_status,
                review_status=review_status,
                metadata_review_status=metadata_review_status,
                resolution_method=resolution_method,
                difference_type=(
                    _blank_to_none(source.get("difference_type"))
                    or (
                        "legacy_review"
                        if legacy_needs_review and not has_explicit_review
                        else None
                    )
                ),
                metadata_difference_type=_blank_to_none(
                    source.get("metadata_difference_type")
                ),
                reviewed_at=reviewed_at,
                metadata_reviewed_at=metadata_reviewed_at,
                extraction_source=extraction_source,
                extraction_error=_blank_to_none(source.get("extraction_error")),
                confidence_reason=_blank_to_none(source.get("confidence_reason")),
            )
            parsed_rows.append(row)
            legacy_urls[row_number] = (
                None if has_explicit_source or has_explicit_canonical else legacy_url
            )
        except ValidationError as exc:
            errors.append(f"{row_number}行目: {_format_validation_error(exc)}")
        except ValueError as exc:
            errors.append(f"{row_number}行目: {exc}")

    if errors:
        raise ImportValidationFailure(errors[:100])

    shop_ids = [row.shop_id for row in parsed_rows]
    duplicate_ids = sorted(shop_id for shop_id, count in Counter(shop_ids).items() if count > 1)
    if duplicate_ids:
        errors.append(f"_idが重複しています: {duplicate_ids[:20]}")

    business_keys = [(row.message_id, row.shop_name) for row in parsed_rows]
    duplicate_keys = [key for key, count in Counter(business_keys).items() if count > 1]
    if duplicate_keys:
        errors.append(f"message_idと店名の組み合わせが重複しています: {duplicate_keys[:10]}")

    legacy_counts = Counter(url for url in legacy_urls.values() if url)
    adjusted_rows: list[CsvImportRow] = []
    for row in parsed_rows:
        legacy_url = legacy_urls[row.row_number]
        if legacy_url and legacy_counts[legacy_url] > 1 and row.canonical_url == legacy_url:
            adjusted_row = row.model_copy(
                update={"source_url": legacy_url, "canonical_url": None}
            )
        else:
            adjusted_row = row
        if not has_explicit_external_identity:
            external_source, external_id = extract_external_identity(
                adjusted_row.canonical_url
            )
            adjusted_row = adjusted_row.model_copy(
                update={
                    "external_source": external_source,
                    "external_id": external_id,
                }
            )
        adjusted_rows.append(adjusted_row)

    if errors:
        raise ImportValidationFailure(errors)

    return ParsedImport(
        filename=Path(filename).name,
        sha256=hashlib.sha256(raw).hexdigest().upper(),
        rows=tuple(adjusted_rows),
    )


def stage_import(db: Session, parsed: ParsedImport) -> ImportPreview:
    existing_batch = db.query(ImportBatch).filter(ImportBatch.sha256 == parsed.sha256).first()
    if existing_batch:
        return build_preview(db, existing_batch)

    batch = ImportBatch(
        id=uuid.uuid4().hex,
        filename=parsed.filename,
        sha256=parsed.sha256,
        status=ImportStatus.VALIDATED.value,
        row_count=len(parsed.rows),
        message_count=parsed.message_count,
        review_count=parsed.review_count,
    )
    db.add(batch)
    for row in parsed.rows:
        db.add(
            ImportRow(
                batch_id=batch.id,
                row_number=row.row_number,
                shop_id=row.shop_id,
                message_id=row.message_id,
                created_at=row.created_at,
                shop_name=row.shop_name,
                branch_name=row.branch_name,
                area=row.area,
                category=row.category,
                address=row.address,
                phone=row.phone,
                external_source=row.external_source,
                external_id=row.external_id,
                is_visited=row.is_visited,
                visited_at=row.visited_at,
                rating=row.rating,
                memo=row.memo,
                source_url=row.source_url,
                canonical_url=row.canonical_url,
                image_key=row.image_key,
                needs_review=row.needs_review,
                resolution_status=row.resolution_status.value,
                review_status=row.review_status.value,
                metadata_review_status=row.metadata_review_status.value,
                resolution_method=(
                    row.resolution_method.value if row.resolution_method else None
                ),
                difference_type=row.difference_type,
                metadata_difference_type=row.metadata_difference_type,
                reviewed_at=row.reviewed_at,
                metadata_reviewed_at=row.metadata_reviewed_at,
                extraction_source=row.extraction_source,
                extraction_error=row.extraction_error,
                confidence_reason=row.confidence_reason,
            )
        )
    db.commit()
    return build_preview(db, batch)


def build_preview(db: Session, batch: ImportBatch) -> ImportPreview:
    staged_ids = {row.shop_id for row in batch.rows}
    current_ids = {shop_id for (shop_id,) in db.query(Shop.id).all()}
    return ImportPreview(
        batch_id=batch.id,
        filename=batch.filename,
        sha256=batch.sha256,
        row_count=batch.row_count,
        message_count=batch.message_count,
        review_count=batch.review_count,
        inserted=len(staged_ids - current_ids),
        updated=len(staged_ids & current_ids),
        deleted=len(current_ids - staged_ids),
    )


def apply_import_batch(db: Session, batch_id: str) -> ImportPreview:
    """Replace archive tables for offline maintenance, outside the web workflow."""
    batch = db.query(ImportBatch).filter(ImportBatch.id == batch_id).first()
    if not batch:
        raise ValueError(f"Import batch not found: {batch_id}")
    if batch.status != ImportStatus.VALIDATED.value:
        raise ValueError(f"Import batch is not applicable: id={batch_id}, status={batch.status}")

    preview = build_preview(db, batch)
    staged_rows = sorted(batch.rows, key=lambda row: row.row_number)
    occurrence_by_message: defaultdict[str, int] = defaultdict(int)
    messages: dict[str, datetime] = {}

    try:
        db.query(ReviewEvent).delete(synchronize_session=False)
        db.query(ShopRedirect).delete(synchronize_session=False)
        db.query(ResolutionCandidate).delete(synchronize_session=False)
        db.query(ShopMention).delete(synchronize_session=False)
        db.query(SourceAsset).delete(synchronize_session=False)
        db.query(ProcessingRun).delete(synchronize_session=False)
        db.query(SyncState).delete(synchronize_session=False)
        db.query(Shop).delete(synchronize_session=False)
        db.query(Message).delete(synchronize_session=False)
        db.flush()
        db.expunge_all()

        for row in staged_rows:
            messages.setdefault(row.message_id, row.created_at)
        for message_id, created_at in messages.items():
            db.add(
                Message(
                    message_id=message_id,
                    source_created_at=created_at,
                    is_target=True,
                    processing_status=ProcessingStatus.SUCCEEDED.value,
                    processed_at=utc_now(),
                )
            )
        db.flush()

        asset_keys: set[tuple[str, str]] = set()
        for row in staged_rows:
            db.add(
                Shop(
                    id=row.shop_id,
                    shop_name=row.shop_name,
                    branch_name=row.branch_name,
                    area=row.area,
                    category=row.category,
                    address=row.address,
                    phone=row.phone,
                    canonical_url=row.canonical_url,
                    image_key=row.image_key,
                    external_source=row.external_source,
                    external_id=row.external_id,
                    is_visited=row.is_visited,
                    visited_at=row.visited_at,
                    rating=row.rating,
                    memo=row.memo,
                    created_at=row.created_at,
                    updated_at=row.created_at,
                )
            )
            occurrence = occurrence_by_message[row.message_id]
            occurrence_by_message[row.message_id] += 1
            db.add(
                ShopMention(
                    message_id=row.message_id,
                    shop_id=row.shop_id,
                    occurrence_index=occurrence,
                    extracted_name=row.shop_name,
                    extracted_branch_name=row.branch_name,
                    extracted_area=row.area,
                    extracted_category=row.category,
                    source_url=row.source_url,
                    resolution_status=row.resolution_status,
                    review_status=row.review_status,
                    metadata_review_status=row.metadata_review_status,
                    resolution_method=row.resolution_method,
                    difference_type=row.difference_type,
                    metadata_difference_type=row.metadata_difference_type,
                    extraction_source=row.extraction_source,
                    extraction_error=row.extraction_error,
                    confidence_reason=row.confidence_reason,
                    reviewed_at=row.reviewed_at,
                    metadata_reviewed_at=row.metadata_reviewed_at,
                    created_at=row.created_at,
                )
            )
            if row.source_url and (row.message_id, row.source_url) not in asset_keys:
                asset_keys.add((row.message_id, row.source_url))
                source_identity = source_asset_identity(row.source_url)
                normalized_url = (
                    source_identity.normalized_url
                    if source_identity is not None
                    else row.source_url
                )
                db.add(
                    SourceAsset(
                        message_id=row.message_id,
                        kind="link",
                        url=row.source_url,
                        source_service=(
                            source_identity.source_service.value
                            if source_identity is not None
                            and source_identity.source_service is not None
                            else None
                        ),
                        source_item_id=(
                            source_identity.source_item_id
                            if source_identity is not None
                            else None
                        ),
                        normalized_url=normalized_url,
                        content_fingerprint=source_asset_fingerprint(
                            kind="link",
                            normalized_url=normalized_url,
                            title=None,
                            description=None,
                        ),
                    )
                )

        db.flush()
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence('shops','id'), "
                    "COALESCE((SELECT MAX(id) FROM shops), 1), true)"
                )
            )
        applied_batch = db.query(ImportBatch).filter(ImportBatch.id == batch_id).one()
        applied_batch.status = ImportStatus.APPLIED.value
        applied_batch.applied_at = utc_now()
        db.commit()
    except Exception as exc:
        db.rollback()
        batch = db.query(ImportBatch).filter(ImportBatch.id == batch_id).first()
        if batch:
            batch.status = ImportStatus.FAILED.value
            batch.error = f"{type(exc).__name__}: {exc}"
            db.commit()
        raise RuntimeError(f"Failed to apply import batch {batch_id}: {exc}") from exc

    return preview


CSV_UPDATE_COLUMNS: dict[str, str] = {
    "shop.name": "shop_name",
    "shop.branch_name": "branch_name",
    "shop.area": "area",
    "shop.category": "category",
    "shop.address": "address",
    "shop.phone": "phone",
    "canonical_url": "canonical_url",
    "status.is_visited": "is_visited",
    "visited_at": "visited_at",
    "rating": "rating",
    "memo": "memo",
}


def escape_csv_update_value(value: str | None) -> str:
    text = value or ""
    prefix_length = len(text) - len(text.lstrip("'"))
    if prefix_length < len(text) and text[prefix_length] in _CSV_INJECT_CHARS:
        return "'" + text
    return text


def _unescape_csv_update_value(value: str) -> str:
    prefix_length = len(value) - len(value.lstrip("'"))
    if prefix_length and prefix_length < len(value) and value[prefix_length] in _CSV_INJECT_CHARS:
        return value[1:]
    return value


def _update_text_or_none(value: str) -> str | None:
    decoded = _unescape_csv_update_value(value)
    return decoded if decoded else None


class CsvShopUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    row_number: int
    shop_id: int = Field(gt=0)
    shop_version: int = Field(gt=0)
    message_id: str
    shop_name: str | None = Field(default=None, min_length=1, max_length=500)
    branch_name: str | None = Field(default=None, max_length=255)
    area: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=2_000)
    phone: str | None = Field(default=None, max_length=32)
    canonical_url: str | None = None
    is_visited: bool | None = None
    visited_at: datetime | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    memo: str | None = Field(default=None, max_length=20_000)

    @field_validator("message_id", mode="before")
    @classmethod
    def validate_message_id(cls, value: object) -> str:
        return CsvImportRow.validate_message_id(value)

    @field_validator("canonical_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return CsvImportRow.validate_url(value)

    @field_validator("shop_name")
    @classmethod
    def validate_shop_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("shop_name cannot contain only whitespace")
        return value

    @model_validator(mode="after")
    def validate_required_values(self) -> "CsvShopUpdate":
        for field in ("shop_name", "is_visited"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be empty")
        return self

    def changes(self) -> dict[str, str | bool | int | datetime | None]:
        return self.model_dump(
            exclude={"row_number", "shop_id", "shop_version", "message_id"},
            exclude_unset=True,
        )


def _changed_fields(shop: Shop, row: CsvShopUpdate) -> dict[str, str | bool | int | datetime | None]:
    changed: dict[str, str | bool | int | datetime | None] = {}
    for field, proposed in row.changes().items():
        previous = getattr(shop, field)
        if field == "area" and proposed != previous and isinstance(proposed, str):
            proposed = canonicalize_area(proposed) or proposed
        if isinstance(previous, datetime) and isinstance(proposed, datetime):
            old_utc = previous.replace(tzinfo=previous.tzinfo or timezone.utc).astimezone(timezone.utc)
            new_utc = proposed.replace(tzinfo=proposed.tzinfo or timezone.utc).astimezone(timezone.utc)
            if old_utc == new_utc:
                continue
        elif previous == proposed:
            continue
        changed[field] = proposed
    return changed


@dataclass(frozen=True)
class ParsedCsvUpdate:
    filename: str
    sha256: str
    rows: tuple[CsvShopUpdate, ...]


def parse_csv_update(raw: bytes, filename: str) -> ParsedCsvUpdate:
    if len(raw) > MAX_FILE_BYTES:
        raise ImportValidationFailure(["CSVファイルは5MB以下にしてください。"])
    if not filename.lower().endswith(".csv"):
        raise ImportValidationFailure(["CSVファイルを指定してください。"])
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportValidationFailure(["UTF-8のCSVファイルを指定してください。"]) from exc
    try:
        reader = csv.DictReader(io.StringIO(decoded), strict=True)
        headers = reader.fieldnames or []
        source_rows = list(reader)
    except csv.Error as exc:
        raise ImportValidationFailure([f"CSV形式が不正です: {exc}"]) from exc
    if len(source_rows) > MAX_ROWS:
        raise ImportValidationFailure([f"CSVは{MAX_ROWS:,}行以下にしてください。"])
    required = {"_id", "shop.version", "message_id"}
    errors: list[str] = []
    if len(headers) != len(set(headers)):
        errors.append("同じ列名が重複しています。")
    missing = required - set(headers)
    if missing:
        errors.append(f"必須列がありません: {', '.join(sorted(missing))}。最新のCSVから取得してください。")
    unsupported = set(headers) - required - set(CSV_UPDATE_COLUMNS)
    if unsupported:
        errors.append(
            f"差分更新では扱えない列です: {', '.join(sorted(unsupported))}。"
            "必須列と変更する店舗情報の列だけを残してください。審査状態は確認画面で変更してください。"
        )
    if not set(headers) & set(CSV_UPDATE_COLUMNS):
        errors.append("変更する店舗情報の列を1つ以上含めてください。")
    if errors:
        raise ImportValidationFailure(errors)
    rows: list[CsvShopUpdate] = []
    seen_ids: set[int] = set()
    for row_number, source in enumerate(source_rows, start=2):
        try:
            if None in source or any(value is None for value in source.values()):
                raise ValueError("列数がヘッダーと一致しません")
            raw_id = source["_id"].strip()
            raw_version = source["shop.version"].strip()
            if not raw_id.isdigit() or not raw_version.isdigit():
                raise ValueError("_id と shop.version は正の整数で指定してください")
            values: dict[str, str | bool | int | datetime | None] = {
                "row_number": row_number,
                "shop_id": int(raw_id),
                "shop_version": int(raw_version),
                "message_id": source["message_id"],
            }
            for column, field in CSV_UPDATE_COLUMNS.items():
                if column not in headers:
                    continue
                value = source[column]
                if field == "is_visited":
                    values[field] = _parse_required_bool(value, column)
                elif field == "rating":
                    values[field] = _parse_rating(value)
                elif field == "visited_at":
                    values[field] = _parse_datetime(value, column, required=False)
                else:
                    values[field] = _update_text_or_none(value)
            row = CsvShopUpdate.model_validate(values)
            if row.shop_id in seen_ids:
                raise ValueError(f"_id={row.shop_id} が重複しています")
            seen_ids.add(row.shop_id)
            rows.append(row)
        except ValidationError as exc:
            errors.append(f"{row_number}行目: {_format_validation_error(exc)}")
        except ValueError as exc:
            errors.append(f"{row_number}行目: {exc}")
    if not rows and not errors:
        errors.append("CSVにデータ行がありません。")
    if errors:
        raise ImportValidationFailure(errors[:100])
    return ParsedCsvUpdate(Path(filename).name, hashlib.sha256(raw).hexdigest().upper(), tuple(rows))


def preview_csv_update(db: Session, parsed: ParsedCsvUpdate) -> ImportPreview:
    errors: list[str] = []
    changes: list[CsvUpdateChange] = []
    column_by_field = {field: column for column, field in CSV_UPDATE_COLUMNS.items()}
    for row in parsed.rows:
        shop = db.query(Shop).filter(Shop.id == row.shop_id).populate_existing().first()
        if shop is None:
            errors.append(f"{row.row_number}行目: _id={row.shop_id} は存在しません。追加はできません。")
            continue
        if shop.version != row.shop_version:
            errors.append(
                f"{row.row_number}行目: _id={row.shop_id} は更新済みです"
                f"（CSV version={row.shop_version} / 最新={shop.version}）。再出力して変更内容を確認してください。"
            )
        linked = db.query(ShopMention.id).filter(
            ShopMention.shop_id == row.shop_id, ShopMention.message_id == row.message_id
        ).first()
        if linked is None:
            errors.append(f"{row.row_number}行目: _id={row.shop_id} と message_id の関連が一致しません。")
        if {"is_visited", "visited_at"} & row.model_fields_set:
            is_visited = row.is_visited if "is_visited" in row.model_fields_set else shop.is_visited
            visited_at = row.visited_at if "visited_at" in row.model_fields_set else shop.visited_at
            if not is_visited and visited_at is not None:
                errors.append(
                    f"{row.row_number}行目: status.is_visited=false の店舗に visited_at は設定できません。"
                    "訪問済みにする場合は status.is_visited=true を、未訪問にする場合は visited_at 列の空欄を指定してください。"
                )
        for field, proposed in _changed_fields(shop, row).items():
            if field == "area" and canonicalize_area(proposed) is None:
                errors.append(
                    f"{row.row_number}行目: shop.area は登録済みの市区町村名・駅名を指定してください。空欄にはできません"
                )
            if field == "category":
                from bot.restaurant_extractor import is_known_category

                if not isinstance(proposed, str) or not is_known_category(proposed):
                    errors.append(
                        f"{row.row_number}行目: shop.category は確認画面の既定分類を指定してください。空欄にはできません"
                    )
            previous = getattr(shop, field)
            changes.append(CsvUpdateChange(
                shop_id=row.shop_id, column=column_by_field[field],
                previous="" if previous is None else str(previous),
                proposed="" if proposed is None else str(proposed),
            ))
        if "canonical_url" in row.model_fields_set:
            identities = collect_external_identities(
                external_source=shop.external_source,
                external_id=shop.external_id,
                urls=(row.canonical_url,),
            )
            if external_identities_conflict(identities):
                errors.append(f"{row.row_number}行目: canonical_url が既存の店舗識別子と一致しません。")
    if errors:
        raise ImportValidationFailure(errors[:100])
    changed_shop_ids = {change.shop_id for change in changes}
    return ImportPreview(
        batch_id="update", filename=parsed.filename, sha256=parsed.sha256,
        row_count=len(parsed.rows),
        message_count=len({row.message_id for row in parsed.rows if row.shop_id in changed_shop_ids}),
        review_count=0, inserted=0, updated=len(changed_shop_ids), deleted=0, changes=tuple(changes),
    )


def apply_csv_update(db: Session, parsed: ParsedCsvUpdate) -> ImportPreview:
    # Import locally because identity helpers are used by the shared lock module.
    from services.shop_creation_lock import lock_new_shop_creation

    try:
        lock_new_shop_creation(db)
        # Shop versions alone cannot guard a link removed by a review decision.
        db.query(ShopMention).filter(
            tuple_(ShopMention.shop_id, ShopMention.message_id).in_(
                [(row.shop_id, row.message_id) for row in parsed.rows]
            )
        ).order_by(ShopMention.id).with_for_update().populate_existing().all()
        db.query(Shop).filter(
            Shop.id.in_([row.shop_id for row in parsed.rows])
        ).order_by(Shop.id).with_for_update().populate_existing().all()
        preview = preview_csv_update(db, parsed)
        for row in sorted(parsed.rows, key=lambda item: item.shop_id):
            shop = db.get(Shop, row.shop_id)
            if shop is None:
                raise ImportValidationFailure([f"{row.row_number}行目: _id={row.shop_id} は存在しません"])
            changes = _changed_fields(shop, row)
            if not changes:
                continue
            statement = (
                update(Shop)
                .where(
                    Shop.id == row.shop_id,
                    Shop.version == row.shop_version,
                    exists().where(
                        ShopMention.shop_id == Shop.id,
                        ShopMention.message_id == row.message_id,
                    ),
                )
                .values(**changes, version=row.shop_version + 1, updated_at=utc_now())
                .execution_options(synchronize_session=False)
            )
            result = db.execute(statement)
            if result.rowcount != 1:
                raise ImportValidationFailure([
                    f"{row.row_number}行目: _id={row.shop_id} が変更されたため、全行の更新を取り消しました。"
                ])
        db.commit()
        db.expire_all()
        return preview
    except Exception:
        db.rollback()
        raise
