from __future__ import annotations

import hashlib
import logging
import os
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.orm import Session

from bot.restaurant_extractor import (
    CANDIDATE_SEARCH_PROMPT_VERSION,
    EXTRACTION_MODEL,
    EXTRACTION_REASONING_EFFORT,
    RESOLUTION_MODEL,
    RESOLUTION_REASONING_EFFORT,
    CandidateSearchResult,
    ExtractedMention,
    ExtractedMessage,
    ExtractionError,
    ImageAnalysisResult,
    ModelCallMetrics,
    PROMPT_VERSION,
    SOURCE_DISCOVERY_PROMPT_VERSION,
    SearchCandidate,
    SearchCandidateSet,
    analyze_restaurant_images,
    discover_restaurant_mentions,
    extract_http_urls,
    extract_restaurant_message,
    fetch_html_document,
    fetch_structured_candidates,
    is_known_category,
    require_extraction_evidence,
    search_restaurant_candidates,
)
from db.models import (
    AssetKind,
    CandidateProvenance,
    FetchStatus,
    LookupCache,
    Message,
    MetadataReviewStatus,
    ProcessingRun,
    ProcessingStatus,
    ResolutionCandidate,
    ResolutionBasis,
    ResolutionMethod,
    ResolutionStatus,
    ReviewStatus,
    Shop,
    ShopMention,
    SourceAsset,
    utc_now,
)
from services.category_normalization import canonicalize_category
from services.import_service import (
    MESSAGE_ID_RE,
    classify_legacy_url,
    collect_external_identities,
    external_identities_conflict,
    extract_external_identity,
    preferred_external_identity,
)
from services.extraction_safety import (
    EVENT_EXCLUDED,
    EvidenceAssessment,
    assess_mention_evidence,
    evidence_review_error,
    event_exclusion_assessment,
    event_origin_quotes_match,
    image_evidence_assessment,
    input_evidence_assessment,
    operating_status_note,
    requires_manual_evidence_review,
    source_requires_fresh_extraction,
)
from services.page_evidence import (
    extract_restaurant_page_evidence,
    page_addresses_match,
    page_mentions_restaurant_name,
    page_text_mentions_restaurant_name,
    page_text_without_restaurant_name,
    prove_page_candidate,
)
from services.resolution import (
    CandidateIdentity,
    branches_conflict,
    evaluate_identity,
    extract_branch_token,
    name_similarity,
    normalize_area,
    normalize_address,
    normalize_external_identity,
    normalize_external_source,
    normalize_name,
    normalize_phone,
    normalize_url_identity,
)
from services.shop_creation_lock import (
    find_shop_creation_collision,
    find_shop_creation_collisions,
    lock_new_shop_creation,
)
from services.source_identity import (
    SourceIdentity,
    identify_source_url,
    source_asset_fingerprint,
    source_asset_identity,
)
from web.area_groups import (
    AREA_TO_GROUP,
    CANONICAL_AREAS,
    GROUPS_IN_ORDER,
    area_from_address,
    area_is_municipality,
    area_municipality,
    canonicalize_area,
    is_known_area,
    municipalities_in_text,
)
from web.region_master import PREFECTURE_NAMES


CACHE_DAYS = max(1, int(os.getenv("LOOKUP_CACHE_DAYS", "30")))
SOURCE_DISCOVERY_MAX_ASSETS = 3
SOURCE_DISCOVERY_MAX_INPUT_CHARS = 60_000
TOP_WEB_MATCHING_POLICY = "unique_top_web_matching_score_080"
TOP_WEB_MATCHING_SCORE = 0.80
PAGE_EVIDENCE_CACHE_KIND = "page_evidence_v2"
SOURCE_MENTION_CACHE_KIND = "source_mention_evidence_v2"
UNRESOLVED_SHOP_LABEL = "（店舗名未特定）"
PREFECTURES: frozenset[str] = frozenset(PREFECTURE_NAMES.values())
ADMIN_PARENTS_BY_REGION: dict[str, frozenset[str]] = {
    region: frozenset(
        group.split(" / ", 1)[1]
        for group in GROUPS_IN_ORDER
        if group.startswith(f"{region} / ")
    )
    for region in {
        group.split(" / ", 1)[0]
        for group in GROUPS_IN_ORDER
        if " / " in group
    }
}
BROAD_AREA_ADMIN_LOCALITIES: dict[str, frozenset[str]] = {
    "札幌": frozenset({"札幌市"}),
    "函館": frozenset({"函館市"}),
    "祝津": frozenset({"小樽市"}),
    "大間": frozenset({"大間町"}),
    "陸前高田": frozenset({"陸前高田市"}),
    "塩釜": frozenset({"塩竈市", "塩釜市"}),
    "仙台": frozenset({"仙台市"}),
    "富山": frozenset({"富山市"}),
    "氷見": frozenset({"氷見市"}),
    "金沢": frozenset({"金沢市"}),
    "岐阜": frozenset({"岐阜市"}),
    "伊東": frozenset({"伊東市"}),
    "名古屋": frozenset({"名古屋市"}),
    "稲沢": frozenset({"稲沢市"}),
    "豊明": frozenset({"豊明市"}),
    "安城": frozenset({"安城市"}),
    "知立": frozenset({"知立市"}),
    "りんくう常滑": frozenset({"常滑市"}),
    "清水五条": frozenset({"京都市"}),
    "祇園四条": frozenset({"京都市"}),
    "芦屋": frozenset({"芦屋市"}),
    "徳島": frozenset({"徳島市"}),
    "高知": frozenset({"高知市"}),
    "五島列島": frozenset({"五島市", "新上五島町"}),
    "那覇空港": frozenset({"那覇市"}),
}
PRECISE_AREA_ADDRESS_ALIASES: dict[str, frozenset[str]] = {
    "塩釜": frozenset({"塩釜", "塩竈"}),
}
SOURCE_EVIDENCE_HOST_ALIASES: dict[str, str] = {
    "fixupx.com": "x.com",
    "fixvx.com": "x.com",
    "fxtwitter.com": "x.com",
    "mobile.twitter.com": "x.com",
    "m.twitter.com": "x.com",
    "twitter.com": "x.com",
    "vxtwitter.com": "x.com",
    "mobile.x.com": "x.com",
    "m.x.com": "x.com",
    "music.youtube.com": "youtube.com",
    "s.tabelog.com": "tabelog.com",
    "youtube-nocookie.com": "youtube.com",
}
SOURCE_EVIDENCE_UNRESOLVED_REDIRECT_HOSTS = frozenset(
    {
        "bit.ly",
        "goo.gl",
        "maps.app.goo.gl",
        "t.co",
        "tinyurl.com",
    }
)
GEOGRAPHIC_VALUE_SEPARATOR_RE = re.compile(
    r"(?:[/／|｜,，、;；・\r\n]+|および|及び|ならびに|並びに|または|又は)"
)
BRANCH_COMPONENT_SEPARATOR_RE = re.compile(
    r"[\s　()（）\[\]［］{}｛｝「」『』【】〈〉《》・\-‐‑–—]+"
)
ADMIN_VALUE_BOUNDARY_RE = re.compile(r"[()（）\[\]［］{}\s]+")
BOUNDARY_ADMIN_TOKEN_RE = re.compile(
    r"^([^0-9０-９\-ー丁目番地号]{1,30})([市区町村])"
)
_DISCORD_IMAGE_HOSTS = frozenset(
    {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "images-ext-1.discordapp.net",
        "images-ext-2.discordapp.net",
    }
)
logger = logging.getLogger(__name__)


class SourceAssetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: str
    url: str
    title: str | None = Field(default=None, max_length=2_000)
    description: str | None = Field(default=None, max_length=20_000)
    mime_type: str | None = Field(default=None, max_length=255)
    is_embed_preview: bool = Field(default=False, exclude=True)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        allowed = {kind.value for kind in AssetKind}
        if value not in allowed:
            raise ValueError(f"unsupported asset kind: {value}")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("asset URL must use http or https")
        return value

    @model_validator(mode="after")
    def validate_embed_preview(self) -> "SourceAssetInput":
        if self.is_embed_preview and self.kind != AssetKind.IMAGE.value:
            raise ValueError("only image assets can be Discord embed previews")
        return self


class MessageEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message_id: str
    channel_id: str
    content: str = Field(min_length=1, max_length=100_000)
    created_at: datetime
    assets: tuple[SourceAssetInput, ...] = Field(default_factory=tuple)
    omitted_asset_count: int = Field(default=0, ge=0)

    @field_validator("message_id", "channel_id")
    @classmethod
    def validate_snowflake(cls, value: str) -> str:
        if not MESSAGE_ID_RE.fullmatch(value):
            raise ValueError("Discord snowflakes must be exact 17-20 digit strings")
        return value


class CandidateList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidateIdentity]


class PipelineCandidate(CandidateIdentity):
    provenance: CandidateProvenance
    is_verified: bool
    verification_reason: str | None = Field(default=None, max_length=2_000)


class CachedPageCandidateProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_url: str = Field(min_length=1, max_length=2_048)
    method: Literal["structured_data", "visible_page"]
    page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate: PipelineCandidate


class CachedSourceMentionProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_url: str = Field(min_length=1, max_length=2_048)
    page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_name: str = Field(min_length=1, max_length=500)
    canonical_area: str = Field(min_length=1, max_length=255)


class CachedSourceDiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: ExtractedMessage
    source_urls: tuple[str, ...]


@dataclass(frozen=True)
class CandidateBatch:
    candidates: tuple[PipelineCandidate, ...]
    unresolved_reason: str | None = None


@dataclass(frozen=True)
class SourceDiscoveryDocument:
    url: str
    metadata_blocks: tuple[str, ...]


@dataclass(frozen=True)
class SourceDiscoveryEvidence:
    input_text: str
    cache_value: str
    source_urls: tuple[str, ...]
    input_source_urls: tuple[str, ...]
    documents: tuple[SourceDiscoveryDocument, ...]
    input_truncated: bool = False


@dataclass(frozen=True)
class MessageProcessResult:
    message_id: str
    shop_ids: tuple[int, ...]
    pending_mention_ids: tuple[int, ...]
    ignored: bool


@dataclass(frozen=True)
class _SourceReuseMatch:
    shop: Shop
    mention: ShopMention
    identity: SourceIdentity


@dataclass(frozen=True)
class _CreationCollisionResolution:
    shop: Shop | None
    blocking_reason: str | None
    has_collision: bool


@dataclass(frozen=True)
class _ProcessingRunSnapshot:
    message_id: str
    stage: str
    model: str | None
    prompt_version: str | None
    status: str
    input_tokens: int
    output_tokens: int
    web_search_calls: int
    image_count: int
    latency_ms: int
    estimated_cost_microusd: int
    error: str | None = None


@dataclass(frozen=True)
class _CacheSnapshot:
    kind: str
    value: str
    payload: str


@dataclass
class _OperationalJournal:
    runs: list[_ProcessingRunSnapshot] = field(default_factory=list)
    caches: dict[tuple[str, str], _CacheSnapshot] = field(default_factory=dict)
    invalidated_caches: set[tuple[str, str]] = field(default_factory=set)
    defer_writes: bool = False


_ACTIVE_OPERATIONAL_JOURNAL: ContextVar[_OperationalJournal | None] = ContextVar(
    "active_operational_journal",
    default=None,
)


def _processing_run(snapshot: _ProcessingRunSnapshot) -> ProcessingRun:
    return ProcessingRun(
        message_id=snapshot.message_id,
        stage=snapshot.stage,
        model=snapshot.model,
        prompt_version=snapshot.prompt_version,
        status=snapshot.status,
        input_tokens=snapshot.input_tokens,
        output_tokens=snapshot.output_tokens,
        web_search_calls=snapshot.web_search_calls,
        image_count=snapshot.image_count,
        latency_ms=snapshot.latency_ms,
        estimated_cost_microusd=snapshot.estimated_cost_microusd,
        error=snapshot.error,
    )


def _pipeline_candidate(
    candidate: CandidateIdentity,
    provenance: CandidateProvenance,
    *,
    is_verified: bool,
    verification_reason: str | None,
) -> PipelineCandidate:
    return PipelineCandidate(
        name=candidate.name,
        area=candidate.area,
        category=candidate.category,
        address=candidate.address,
        phone=candidate.phone,
        canonical_url=candidate.canonical_url,
        external_source=candidate.external_source,
        external_id=candidate.external_id,
        evidence_url=candidate.evidence_url,
        provenance=provenance,
        is_verified=is_verified,
        verification_reason=verification_reason,
    )


def _record_metrics(
    db: Session,
    message_id: str,
    stage: str,
    metrics: ModelCallMetrics,
) -> None:
    snapshot = _ProcessingRunSnapshot(
        message_id=message_id,
        stage=stage,
        model=metrics.model,
        prompt_version=PROMPT_VERSION,
        status=ProcessingStatus.SUCCEEDED.value,
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        web_search_calls=metrics.web_search_calls,
        image_count=metrics.image_count,
        latency_ms=metrics.latency_ms,
        estimated_cost_microusd=metrics.estimated_cost_microusd,
    )
    journal = _ACTIVE_OPERATIONAL_JOURNAL.get()
    if journal is None or not journal.defer_writes:
        db.add(_processing_run(snapshot))
    if journal is not None:
        journal.runs.append(snapshot)
    for attempt in range(2, metrics.api_attempts + 1):
        retry_snapshot = _ProcessingRunSnapshot(
            message_id=message_id,
            stage=f"{stage}_retry_attempt",
            model=metrics.model,
            prompt_version=PROMPT_VERSION,
            status=ProcessingStatus.FAILED.value,
            input_tokens=0,
            output_tokens=0,
            web_search_calls=0,
            image_count=0,
            latency_ms=0,
            estimated_cost_microusd=0,
            error=f"attempt={attempt}; cost included in aggregate metrics",
        )
        if journal is None or not journal.defer_writes:
            db.add(_processing_run(retry_snapshot))
        if journal is not None:
            journal.runs.append(retry_snapshot)


def _record_failure(
    db: Session,
    message_id: str,
    stage: str,
    error: Exception,
) -> None:
    snapshot = _ProcessingRunSnapshot(
        message_id=message_id,
        stage=stage,
        model=None,
        prompt_version=PROMPT_VERSION,
        status=ProcessingStatus.FAILED.value,
        input_tokens=0,
        output_tokens=0,
        web_search_calls=0,
        image_count=0,
        latency_ms=0,
        estimated_cost_microusd=0,
        error=f"{type(error).__name__}: {error}",
    )
    journal = _ACTIVE_OPERATIONAL_JOURNAL.get()
    if journal is None or not journal.defer_writes:
        db.add(_processing_run(snapshot))
    if journal is not None:
        journal.runs.append(snapshot)


def _replace_source_assets(
    db: Session,
    message_id: str,
    assets: tuple[SourceAssetInput, ...],
) -> None:
    db.query(SourceAsset).filter(SourceAsset.message_id == message_id).delete(
        synchronize_session=False
    )
    db.flush()
    rows: list[SourceAsset] = []
    for asset in assets:
        identity = source_asset_identity(asset.url)
        normalized_url = identity.normalized_url if identity is not None else asset.url
        rows.append(
            SourceAsset(
                message_id=message_id,
                kind=asset.kind,
                url=asset.url,
                title=asset.title,
                description=asset.description,
                mime_type=asset.mime_type,
                source_service=(
                    identity.source_service.value
                    if identity is not None and identity.source_service is not None
                    else None
                ),
                source_item_id=(identity.source_item_id if identity is not None else None),
                normalized_url=normalized_url,
                content_fingerprint=source_asset_fingerprint(
                    kind=asset.kind,
                    normalized_url=normalized_url,
                    title=asset.title,
                    description=asset.description,
                ),
                fetch_status=FetchStatus.AVAILABLE.value,
            )
        )
    db.add_all(rows)


def _envelope_input_assessment(
    envelope: MessageEnvelope, *, source_input_truncated: bool = False,
) -> EvidenceAssessment:
    return input_evidence_assessment(
        envelope.content,
        omitted_asset_count=max(envelope.omitted_asset_count, len(envelope.assets) - 50),
        source_input_truncated=source_input_truncated,
    )


def _source_identity_for_reuse(envelope: MessageEnvelope) -> SourceIdentity | None:
    if _envelope_input_assessment(envelope).requires_review or source_requires_fresh_extraction(envelope.content):
        return None
    identities: dict[tuple[str, str, str], SourceIdentity] = {}
    source_asset_count = 0
    for asset in envelope.assets:
        if asset.kind == AssetKind.IMAGE.value and asset.is_embed_preview:
            continue
        if asset.kind not in {AssetKind.LINK.value, AssetKind.EMBED.value}:
            return None
        source_asset_count += 1
        identity = identify_source_url(asset.url)
        if identity is None:
            return None
        identities[
            (identity.service.value, identity.item_id, identity.normalized_url)
        ] = identity
    if source_asset_count == 0 or len(identities) != 1:
        return None
    if _has_meaningful_source_reuse_content(envelope.content):
        return None
    return next(iter(identities.values()))


def _has_meaningful_source_reuse_content(content: str) -> bool:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(
            ("[Embed URL]", "[Embed Title]", "[Embed Description]", "[Embed Field:")
        ):
            continue
        without_urls = re.sub(r"https?://[^\s<>]+", "", stripped)
        if normalize_name(without_urls):
            return True
    return False


def _find_source_reuse_match(
    db: Session,
    envelope: MessageEnvelope,
) -> _SourceReuseMatch | None:
    identity = _source_identity_for_reuse(envelope)
    if identity is None:
        return None
    message_ids = {
        message_id
        for (message_id,) in db.query(SourceAsset.message_id)
        .filter(
            SourceAsset.source_service == identity.service.value,
            SourceAsset.source_item_id == identity.item_id,
            SourceAsset.normalized_url == identity.normalized_url,
            SourceAsset.message_id != envelope.message_id,
        )
        .distinct()
        .all()
    }
    if not message_ids:
        return None
    mentions = (
        db.query(ShopMention)
        .filter(ShopMention.message_id.in_(message_ids))
        .order_by(ShopMention.id.asc())
        .all()
    )
    mentions_by_message: dict[str, list[ShopMention]] = {}
    for mention in mentions:
        mentions_by_message.setdefault(mention.message_id, []).append(mention)

    reusable_mentions: list[ShopMention] = []
    for message_id in sorted(message_ids):
        active_mentions = [
            mention
            for mention in mentions_by_message.get(message_id, [])
            if mention.review_status != ReviewStatus.REJECTED.value
        ]
        if not active_mentions:
            continue
        if len(active_mentions) != 1:
            return None
        mention = active_mentions[0]
        if (
            mention.review_status != ReviewStatus.APPROVED.value
            or mention.shop_id is None
            or requires_manual_evidence_review(mention.extraction_error)
        ):
            return None
        reusable_mentions.append(mention)
    if not reusable_mentions:
        return None
    shop_ids = {mention.shop_id for mention in reusable_mentions}
    if len(shop_ids) != 1:
        return None
    shop_id = next(iter(shop_ids))
    shop = db.query(Shop).filter(Shop.id == shop_id).one_or_none()
    if shop is None or _source_mentions_conflict_with_shop(shop, reusable_mentions):
        return None
    source_mention = max(reusable_mentions, key=lambda mention: mention.id)
    return _SourceReuseMatch(shop=shop, mention=source_mention, identity=identity)


def _source_mentions_conflict_with_shop(
    shop: Shop,
    mentions: list[ShopMention],
) -> bool:
    identities = set(_shop_external_identities(shop))
    for mention in mentions:
        for candidate in mention.candidates:
            if not candidate.is_strong_match:
                continue
            candidate_identity = CandidateIdentity(
                name=candidate.name,
                area=candidate.area,
                category=candidate.category,
                address=candidate.address,
                phone=candidate.phone,
                canonical_url=candidate.canonical_url,
                external_source=candidate.external_source,
                external_id=candidate.external_id,
                evidence_url=candidate.evidence_url,
            )
            identities.update(_candidate_external_identities(candidate_identity))
            if evaluate_identity(_shop_identity(shop), candidate_identity).conflicting_fields:
                return True
    return external_identities_conflict(frozenset(identities))


def _reuse_source_match(
    db: Session,
    message: Message,
    envelope: MessageEnvelope,
    match: _SourceReuseMatch,
    *,
    commit: bool,
) -> MessageProcessResult:
    metadata_status, metadata_difference = _metadata_review_state(
        match.shop.area,
        match.shop.category,
    )
    current_source_url = next(
        (
            asset.url
            for asset in envelope.assets
            if asset.kind in {AssetKind.LINK.value, AssetKind.EMBED.value}
            and identify_source_url(asset.url) == match.identity
        ),
        match.identity.normalized_url,
    )
    mention = ShopMention(
        message_id=envelope.message_id,
        shop_id=match.shop.id,
        occurrence_index=0,
        extracted_name=match.mention.extracted_name,
        extracted_branch_name=match.mention.extracted_branch_name,
        extracted_area=match.mention.extracted_area,
        extracted_category=match.mention.extracted_category,
        source_url=current_source_url,
        resolution_status=ResolutionStatus.RESOLVED.value,
        review_status=ReviewStatus.APPROVED.value,
        metadata_review_status=metadata_status.value,
        metadata_difference_type=metadata_difference,
        resolution_method=ResolutionMethod.AUTOMATIC.value,
        resolution_basis=ResolutionBasis.SOURCE_REUSE.value,
        reused_from_mention_id=match.mention.id,
        extraction_source="source_identity_reuse",
        confidence_reason=(
            f"source_service={match.identity.service.value}; "
            f"source_item_id={match.identity.item_id}; "
            f"reused_from_mention_id={match.mention.id}"
        ),
        reviewed_at=utc_now(),
        metadata_reviewed_at=(
            utc_now() if metadata_status == MetadataReviewStatus.APPROVED else None
        ),
    )
    db.add(mention)
    message.is_target = True
    message.processing_status = ProcessingStatus.SUCCEEDED.value
    message.processed_at = utc_now()
    if commit:
        db.commit()
    else:
        db.flush()
    return MessageProcessResult(envelope.message_id, (match.shop.id,), (), False)


def _source_urls_from_assets(
    assets: tuple[SourceAssetInput, ...],
) -> tuple[str | None, str | None, bool]:
    source_url: str | None = None
    canonical_hints: dict[str, str] = {}
    for asset in assets:
        if asset.kind not in {AssetKind.LINK.value, AssetKind.EMBED.value}:
            continue
        classified_source, classified_canonical = classify_legacy_url(asset.url)
        if source_url is None and classified_source is not None:
            source_url = classified_source
        if classified_canonical is not None:
            external_source, external_id = extract_external_identity(classified_canonical)
            external_identity = normalize_external_identity(
                external_source,
                external_id,
            )
            identity_key = (
                f"external:{external_identity[0]}:{external_identity[1]}"
                if external_identity is not None
                else f"url:{normalize_url_identity(classified_canonical)}"
            )
            canonical_hints.setdefault(identity_key, classified_canonical)
    canonical_hint = (
        next(iter(canonical_hints.values()))
        if len(canonical_hints) == 1
        else None
    )
    return source_url, canonical_hint, len(canonical_hints) > 1


def _source_discovery_evidence(
    envelope: MessageEnvelope,
) -> SourceDiscoveryEvidence | None:
    selected_by_url: dict[str, SourceAssetInput] = {}
    for asset in envelope.assets:
        if asset.kind not in {AssetKind.LINK.value, AssetKind.EMBED.value}:
            continue
        existing = selected_by_url.get(asset.url)
        if existing is None or (
            asset.kind == AssetKind.EMBED.value
            and (asset.title or asset.description)
            and not (existing.title or existing.description)
        ):
            selected_by_url[asset.url] = asset
    selected = tuple(selected_by_url.values())[:SOURCE_DISCOVERY_MAX_ASSETS]
    if not selected:
        return None

    parts: list[str] = []
    documents: list[SourceDiscoveryDocument] = []
    for asset in selected:
        parts.append(f"[Source URL] {asset.url}")
        metadata_parts: list[str] = []
        if asset.title:
            parts.append(f"[Source Title] {asset.title[:2_000]}")
            metadata_parts.append(asset.title[:2_000])
        if asset.description:
            parts.append(f"[Source Description] {asset.description[:8_000]}")
            metadata_parts.append(asset.description[:8_000])
        documents.append(
            SourceDiscoveryDocument(
                url=asset.url,
                metadata_blocks=tuple(metadata_parts),
            )
        )
    parts.append(f"[Discord Message]\n{envelope.content}")
    full_input_text = "\n".join(parts)
    input_text = full_input_text[:SOURCE_DISCOVERY_MAX_INPUT_CHARS]
    normalized = re.sub(r"\s+", " ", input_text).strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    content_urls = extract_http_urls(envelope.content)
    asset_metadata_urls = tuple(
        metadata_url
        for asset in selected_by_url.values()
        for text in (asset.title, asset.description)
        if text
        for metadata_url in extract_http_urls(text)
    )
    excluded_source_urls = tuple(
        dict.fromkeys(
            (
                *(asset.url for asset in selected_by_url.values()),
                *content_urls,
                *asset_metadata_urls,
            )
        )
    )
    return SourceDiscoveryEvidence(
        input_text=input_text,
        cache_value=f"{SOURCE_DISCOVERY_PROMPT_VERSION}:{digest}",
        source_urls=excluded_source_urls,
        input_source_urls=tuple(asset.url for asset in selected),
        documents=tuple(documents),
        input_truncated=(
            len(selected_by_url) > len(selected)
            or len(full_input_text) > SOURCE_DISCOVERY_MAX_INPUT_CHARS
            or any(len(asset.description or "") > 8_000 for asset in selected)
        ),
    )


def _cache_key(kind: str, value: str) -> str:
    namespace: str = f"{kind}:{PROMPT_VERSION}"
    if kind in {"source_discovery", SOURCE_MENTION_CACHE_KIND}:
        namespace += f":{EXTRACTION_MODEL}:{EXTRACTION_REASONING_EFFORT}"
    elif kind == "web_search":
        namespace += f":{RESOLUTION_MODEL}:{RESOLUTION_REASONING_EFFORT}"
    digest: str = hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).hexdigest()
    return digest


def _get_cache(db: Session, kind: str, value: str) -> LookupCache | None:
    key = _cache_key(kind, value)
    journal = _ACTIVE_OPERATIONAL_JOURNAL.get()
    if journal is not None and journal.defer_writes:
        staged = journal.caches.get((kind, value))
        if staged is not None:
            return LookupCache(
                cache_key=key,
                kind=kind,
                payload=staged.payload,
                expires_at=utc_now() + timedelta(days=CACHE_DAYS),
                created_at=utc_now(),
            )
        if (kind, value) in journal.invalidated_caches:
            return None
        # Evidence reads must not flush staged work before an external await.
        with db.no_autoflush:
            return (
                db.query(LookupCache)
                .filter(LookupCache.cache_key == key, LookupCache.expires_at > utc_now())
                .first()
            )
    return (
        db.query(LookupCache)
        .filter(LookupCache.cache_key == key, LookupCache.expires_at > utc_now())
        .first()
    )


def _put_cache(db: Session, kind: str, value: str, payload: str) -> None:
    journal = _ACTIVE_OPERATIONAL_JOURNAL.get()
    if journal is not None and journal.defer_writes:
        journal.caches[(kind, value)] = _CacheSnapshot(kind=kind, value=value, payload=payload)
        return
    key = _cache_key(kind, value)
    existing = db.query(LookupCache).filter(LookupCache.cache_key == key).first()
    expires_at = utc_now() + timedelta(days=CACHE_DAYS)
    if existing:
        existing.payload = payload
        existing.expires_at = expires_at
        existing.created_at = utc_now()
    else:
        db.add(
            LookupCache(
                cache_key=key,
                kind=kind,
                payload=payload,
                expires_at=expires_at,
            )
        )
    if journal is not None:
        snapshot = _CacheSnapshot(kind=kind, value=value, payload=payload)
        journal.caches[(kind, value)] = snapshot


def _delete_cache(db: Session, kind: str, value: str, cached: LookupCache) -> None:
    journal = _ACTIVE_OPERATIONAL_JOURNAL.get()
    if journal is not None and journal.defer_writes:
        key = (kind, value)
        journal.invalidated_caches.add(key)
        journal.caches.pop(key, None)
        return
    db.delete(cached)
    db.flush()
    if journal is not None:
        key = (kind, value)
        journal.invalidated_caches.add(key)
        journal.caches.pop(key, None)


def _shop_identity(shop: Shop) -> CandidateIdentity:
    return CandidateIdentity(
        name=_name_with_branch(shop.shop_name, shop.branch_name),
        area=shop.area,
        category=shop.category,
        address=shop.address,
        phone=shop.phone,
        canonical_url=shop.canonical_url,
        external_source=shop.external_source,
        external_id=shop.external_id,
        evidence_url=shop.canonical_url,
    )


def _dedupe_candidates(candidates: list[PipelineCandidate]) -> list[PipelineCandidate]:
    deduped: list[PipelineCandidate] = []
    indexes: dict[tuple[str, ...], int] = {}
    for candidate in candidates:
        identity_key = "|".join(
            f"{source}:{external_id}"
            for source, external_id in sorted(_candidate_external_identities(candidate))
        )
        key = (
            candidate.provenance.value,
            identity_key,
            normalize_name(candidate.name),
            normalize_area(candidate.area),
            normalize_name(candidate.category or ""),
            normalize_address(candidate.address) or "",
            normalize_phone(candidate.phone) or "",
            (
                _citation_url_identity(candidate.canonical_url)
                if candidate.canonical_url
                else ""
            ),
            (
                _citation_url_identity(candidate.evidence_url)
                if candidate.evidence_url
                else ""
            ),
        )
        existing_index = indexes.get(key)
        if existing_index is None:
            indexes[key] = len(deduped)
            deduped.append(candidate)
        elif candidate.is_verified and not deduped[existing_index].is_verified:
            deduped[existing_index] = candidate
    return sorted(deduped, key=lambda candidate: not candidate.is_verified)


def _replace_web_candidate_with_verified_page(
    candidates: list[PipelineCandidate],
    verified: PipelineCandidate,
) -> list[PipelineCandidate]:
    verified_keys = _candidate_source_identity_keys(verified)
    retained = [
        candidate
        for candidate in candidates
        if not (
            candidate.provenance == CandidateProvenance.WEB_SEARCH
            and not candidate.is_verified
            and normalize_name(candidate.name) == normalize_name(verified.name)
            and page_addresses_match(candidate.address, verified.address)
            and bool(
                verified_keys & _candidate_source_identity_keys(candidate)
            )
        )
    ]
    retained.append(verified)
    return _dedupe_candidates(retained)


async def _structured_candidates(
    db: Session,
    message_id: str,
    url: str,
) -> list[PipelineCandidate]:
    cached = _get_cache(db, "url_metadata", url)
    if cached:
        try:
            cached_candidates = CandidateList.model_validate_json(cached.payload).candidates
        except ValidationError as exc:
            _record_failure(
                db,
                message_id,
                "url_metadata_cache",
                ExtractionError(f"url={url}; invalid cached payload: {exc}"),
            )
            _delete_cache(db, "url_metadata", url, cached)
        else:
            return [
                _pipeline_candidate(
                    candidate,
                    CandidateProvenance.STRUCTURED_DATA,
                    is_verified=True,
                    verification_reason=f"サーバーが取得した構造化データ: {url}",
                )
                for candidate in cached_candidates
            ]
    try:
        fetched = await fetch_structured_candidates(url)
    except (ExtractionError, ValidationError) as exc:
        _record_failure(
            db,
            message_id,
            "url_metadata",
            ExtractionError(f"url={url}; {type(exc).__name__}: {exc}"),
        )
        return []
    pipeline_candidates = _dedupe_candidates(
        [
            _pipeline_candidate(
                candidate,
                CandidateProvenance.STRUCTURED_DATA,
                is_verified=True,
                verification_reason=f"サーバーが取得した構造化データ: {url}",
            )
            for candidate in fetched
        ]
    )
    _put_cache(
        db,
        "url_metadata",
        url,
        CandidateList(candidates=pipeline_candidates).model_dump_json(),
    )
    return pipeline_candidates


def _web_search_query_key(mention: ExtractedMention) -> str:
    return "|".join(
        [
            CANDIDATE_SEARCH_PROMPT_VERSION,
            normalize_name(mention.shop_name),
            normalize_name(mention.branch_name or ""),
            normalize_area(mention.area),
        ]
    )


async def _web_candidates(
    db: Session,
    message_id: str,
    mention: ExtractedMention,
) -> CandidateBatch:
    query_key = _web_search_query_key(mention)
    cached = _get_cache(db, "web_search", query_key)
    if cached:
        try:
            candidate_set = SearchCandidateSet.model_validate_json(cached.payload)
        except ValidationError as exc:
            _record_failure(
                db,
                message_id,
                "candidate_search_cache",
                ExtractionError(
                    f"query={query_key}; invalid cached payload: {exc}"
                ),
            )
            _delete_cache(db, "web_search", query_key, cached)
            candidate_set = None
    else:
        candidate_set = None
    if candidate_set is None:
        try:
            result: CandidateSearchResult = await search_restaurant_candidates(mention)
        except (ExtractionError, ValidationError) as exc:
            _record_failure(db, message_id, "candidate_search", exc)
            raise
        _record_metrics(db, message_id, "candidate_search", result.metrics)
        if result.metrics.web_search_calls != result.metrics.api_attempts:
            error = ExtractionError(
                "Candidate search must use one web search per API attempt: "
                f"message_id={message_id}; calls={result.metrics.web_search_calls}; "
                f"attempts={result.metrics.api_attempts}"
            )
            _record_failure(db, message_id, "candidate_search_validation", error)
            raise error
        cited_candidates = [
            sanitized
            for candidate in result.candidates.candidates
            if (
                sanitized := _sanitize_cited_web_candidate(
                    candidate,
                    result.source_urls,
                )
            )
            is not None
        ]
        unresolved_reason = result.candidates.unresolved_reason
        if len(cited_candidates) != len(result.candidates.candidates):
            grounding_reason = "検索元に含まれない候補URLを除外しました。"
            unresolved_reason = (
                f"{unresolved_reason} / {grounding_reason}"
                if unresolved_reason
                else grounding_reason
            )
        candidate_set = result.candidates.model_copy(
            update={
                "candidates": cited_candidates,
                "unresolved_reason": unresolved_reason,
            }
        )
        _put_cache(db, "web_search", query_key, candidate_set.model_dump_json())
    return CandidateBatch(
        candidates=tuple(
            _pipeline_candidate(
                candidate,
                CandidateProvenance.WEB_SEARCH,
                is_verified=False,
                verification_reason="候補URLは検索元と一致、店舗情報はサーバー検証前",
            )
            for candidate in candidate_set.candidates
        ),
        unresolved_reason=candidate_set.unresolved_reason,
    )


async def _source_mention_is_grounded(
    db: Session,
    message_id: str,
    mention: ExtractedMention,
    evidence: SourceDiscoveryEvidence,
    search_source_urls: tuple[str, ...],
) -> tuple[bool, str | None]:
    if evidence.input_truncated:
        return False, "検索入力で省略された本文・出典があり、根拠の全体を確認できない"
    if mention.source_url is None:
        return False, "店舗ごとの元出典URLがない"
    mention_source_identity = _citation_url_identity(mention.source_url)
    input_source_identities = frozenset(
        _citation_url_identity(url) for url in evidence.input_source_urls
    )
    search_source_identities = frozenset(
        _citation_url_identity(url) for url in search_source_urls
    )
    if mention_source_identity not in input_source_identities:
        return False, "店舗の元出典URLが検索入力に含まれない"
    if mention_source_identity not in search_source_identities:
        return False, "店舗の元出典URLがWeb検索の引用元に含まれない"
    source_document = next(
        (
            document
            for document in evidence.documents
            if _citation_url_identity(document.url) == mention_source_identity
        ),
        None,
    )
    if source_document is None:
        return False, "店舗の元出典を入力資料へ対応付けられない"
    source_name = _name_with_branch(mention.shop_name, mention.branch_name)
    normalized_name = normalize_name(source_name)
    canonical_area = canonicalize_area(mention.area)
    if canonical_area is None:
        return False, "店舗の地域をアプリの粒度へ正規化できない"
    normalized_source_area = normalize_name(canonical_area)
    metadata_matches = any(
        _source_block_matches_name_and_area(
            source_name,
            canonical_area,
            block,
        )
        for block in source_document.metadata_blocks
    )
    if normalized_name and metadata_matches:
        return True, None

    cache_value = (
        f"{mention_source_identity}|{normalized_name}|{normalized_source_area}"
    )
    cached = _get_cache(db, SOURCE_MENTION_CACHE_KIND, cache_value)
    if cached is not None:
        try:
            cached_proof = CachedSourceMentionProof.model_validate_json(cached.payload)
        except ValidationError as exc:
            _record_failure(
                db,
                message_id,
                "source_mention_evidence_cache",
                ExtractionError(
                    f"url={source_document.url}; invalid cached payload: {exc}"
                ),
            )
            _delete_cache(db, SOURCE_MENTION_CACHE_KIND, cache_value, cached)
        else:
            if (
                cached_proof.normalized_name == normalized_name
                and cached_proof.canonical_area == canonical_area
                and _citation_url_identity(cached_proof.final_url)
                == mention_source_identity
            ):
                return True, None
            _delete_cache(db, SOURCE_MENTION_CACHE_KIND, cache_value, cached)

    try:
        document = await fetch_html_document(source_document.url)
    except (ExtractionError, httpx.HTTPError) as exc:
        error = ExtractionError(
            f"url={source_document.url}; shop={mention.shop_name}; "
            f"{type(exc).__name__}: {exc}"
        )
        _record_failure(db, message_id, "source_mention_evidence", error)
        return False, "元出典ページをサーバーから取得できない"
    if _citation_url_identity(document.final_url) != mention_source_identity:
        return False, "元出典ページの転送先が検索入力URLと一致しない"
    page_evidence = extract_restaurant_page_evidence(
        document.html,
        document.final_url,
    )
    page_has_name = page_mentions_restaurant_name(source_name, page_evidence)
    named_structured_candidates = tuple(
        candidate
        for candidate in page_evidence.structured_candidates
        if normalize_name(candidate.name) == normalized_name
    )
    if named_structured_candidates:
        page_has_area = bool(
            len(named_structured_candidates) == 1
            and named_structured_candidates[0].address is not None
            and _candidate_address_precisely_matches_area(
                canonical_area,
                named_structured_candidates[0].address,
            )
        )
    else:
        page_has_area = any(
            _source_block_matches_name_and_area(
                source_name,
                canonical_area,
                block,
            )
            for block in page_evidence.text_blocks
        )
    if not page_has_name or not page_has_area:
        return False, "元出典ページ上で店舗名と地域を同時に確認できない"
    _put_cache(
        db,
        SOURCE_MENTION_CACHE_KIND,
        cache_value,
        CachedSourceMentionProof(
            final_url=document.final_url,
            page_sha256=page_evidence.page_sha256,
            normalized_name=normalized_name,
            canonical_area=canonical_area,
        ).model_dump_json(),
    )
    return True, None


def _source_block_matches_name_and_area(
    source_name: str,
    canonical_area: str,
    block: str,
) -> bool:
    text_without_name = page_text_without_restaurant_name(source_name, block)
    if text_without_name is None:
        return False
    return _canonical_areas_in_text(text_without_name) == frozenset(
        {canonical_area}
    )


async def _source_discovery_message(
    db: Session,
    message_id: str,
    evidence: SourceDiscoveryEvidence,
) -> ExtractedMessage:
    cached = _get_cache(db, "source_discovery", evidence.cache_value)
    cached_result: CachedSourceDiscoveryResult | None = None
    if cached:
        try:
            cached_result = CachedSourceDiscoveryResult.model_validate_json(
                cached.payload
            )
        except ValidationError as exc:
            _record_failure(
                db,
                message_id,
                "source_discovery_cache",
                ExtractionError(
                    f"source_digest={evidence.cache_value}; invalid cached payload: {exc}"
                ),
            )
            _delete_cache(
                db,
                "source_discovery",
                evidence.cache_value,
                cached,
            )

    if cached_result is None:
        try:
            result = await discover_restaurant_mentions(evidence.input_text)
        except (ExtractionError, ValidationError) as exc:
            _record_failure(db, message_id, "source_discovery", exc)
            raise
        _record_metrics(db, message_id, "source_discovery", result.metrics)
        if result.metrics.web_search_calls != result.metrics.api_attempts:
            error = ExtractionError(
                "Source discovery must use one web search per API attempt: "
                f"message_id={message_id}; calls={result.metrics.web_search_calls}; "
                f"attempts={result.metrics.api_attempts}"
            )
            _record_failure(db, message_id, "source_discovery_validation", error)
            raise error
        cached_result = CachedSourceDiscoveryResult(
            message=result.message,
            source_urls=result.source_urls,
        )
        _put_cache(
            db,
            "source_discovery",
            evidence.cache_value,
            cached_result.model_dump_json(),
        )
    discovered_message = require_extraction_evidence(cached_result.message)
    validated_mentions: list[ExtractedMention] = []
    grounding_reasons: list[str] = []
    for mention in discovered_message.mentions:
        grounded, reason = await _source_mention_is_grounded(
            db,
            message_id,
            mention,
            evidence,
            cached_result.source_urls,
        )
        if grounded:
            validated_mentions.append(mention)
            continue
        grounding_reason = reason or "店舗ごとの元出典を確認できない"
        grounding_reasons.append(f"{mention.shop_name}: {grounding_reason}")
        prior_reason = mention.confidence_reason
        validated_mentions.append(
            mention.model_copy(
                update={
                    "source_url": None,
                    "needs_review": True,
                    "confidence_reason": (
                        f"{prior_reason} / 元出典未確認: {grounding_reason}"
                    )[:2_000],
                }
            )
        )
        _record_failure(
            db,
            message_id,
            "source_discovery_grounding",
            ExtractionError(
                f"message_id={message_id}; shop={mention.shop_name}; "
                f"reason={grounding_reason}"
            ),
        )
    if grounding_reasons:
        discovered_message = discovered_message.model_copy(
            update={
                "mentions": validated_mentions,
                "unresolved_reason": None,
            }
        )
    return discovered_message


def _find_existing_shop(
    db: Session,
    mention: ExtractedMention,
    candidates: list[PipelineCandidate],
) -> tuple[Shop | None, str | None]:
    matches: dict[int, tuple[Shop, str]] = {}
    conflicting_matches: set[str] = set()
    all_shops = db.query(Shop).all()
    existing_shops = [
        shop
        for shop in all_shops
        if any(
            mention_row.review_status == ReviewStatus.APPROVED.value
            for mention_row in shop.mentions
        )
    ]
    for candidate in candidates:
        if not candidate.is_verified or not _matches_mention_name_and_area(
            mention,
            candidate,
        ):
            continue
        if external_identities_conflict(_candidate_external_identities(candidate)):
            return None, "候補の外部IDが同一サービス内で矛盾"
        if (
            candidate.provenance == CandidateProvenance.STRUCTURED_DATA
            and _structured_candidates_with_shared_source_conflict(
                mention,
                candidate,
                candidates,
            )
        ):
            return None, "同じ構造化データ出典の店舗候補が矛盾"
        evidence_matches = _candidate_evidence_matching_shops(all_shops, candidate)
        if len(evidence_matches) > 1:
            return None, "候補の外部IDまたはURLが複数の既存店舗に紐付いている"
        if evidence_matches:
            evidence_shop = evidence_matches[0]
            if external_identities_conflict(_shop_external_identities(evidence_shop)):
                return None, "候補の根拠に一致する既存店舗内で外部IDが矛盾"
            if evidence_shop not in existing_shops:
                return None, "候補の外部IDまたはURLが未承認店舗と競合"
            if not _matches_mention_shop_identity(mention, evidence_shop):
                continue
        for shop in existing_shops:
            if not _matches_mention_shop_identity(mention, shop):
                continue
            if external_identities_conflict(_shop_external_identities(shop)):
                conflicting_matches.add("external_id")
                continue
            outcome = evaluate_identity(_shop_identity(shop), candidate)
            if outcome.is_strong_match and outcome.conflicting_fields:
                conflicting_matches.update(outcome.conflicting_fields)
            elif outcome.is_strong_match:
                matches[shop.id] = (shop, outcome.reason)
    if conflicting_matches:
        fields = ",".join(sorted(conflicting_matches))
        return None, f"強い一致根拠と候補情報が矛盾: fields={fields}"
    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        return None, "複数の既存店舗が強い一致条件を満たしたため自動統合しない"
    return None, None


def _name_with_branch(name: str, branch_name: str | None) -> str:
    normalized_name = normalize_name(name)
    normalized_branch = normalize_name(branch_name or "")
    if not normalized_branch or normalized_name.endswith(normalized_branch):
        return name
    return f"{name} {branch_name}"


def _mention_branch(mention: ExtractedMention) -> str:
    return normalize_name(
        mention.branch_name or extract_branch_token(mention.shop_name) or ""
    )


def _shop_branch(shop: Shop) -> str:
    return normalize_name(shop.branch_name or extract_branch_token(shop.shop_name) or "")


def _shop_external_identities(shop: Shop) -> frozenset[tuple[str, str]]:
    return collect_external_identities(
        external_source=shop.external_source,
        external_id=shop.external_id,
        urls=(shop.canonical_url,),
    )


def _candidate_external_identities(
    candidate: CandidateIdentity,
) -> frozenset[tuple[str, str]]:
    return collect_external_identities(
        external_source=candidate.external_source,
        external_id=candidate.external_id,
        urls=(candidate.canonical_url, candidate.evidence_url),
    )


def _candidate_evidence_matching_shops(
    shops: list[Shop],
    candidate: CandidateIdentity,
) -> tuple[Shop, ...]:
    candidate_identities = _candidate_external_identities(candidate)
    candidate_urls = frozenset(
        normalized
        for url in (candidate.canonical_url, candidate.evidence_url)
        if (normalized := normalize_url_identity(url)) is not None
    )
    return tuple(
        shop
        for shop in shops
        if candidate_identities & _shop_external_identities(shop)
        or normalize_url_identity(shop.canonical_url) in candidate_urls
    )


def _shop_external_ids_for_source(shop: Shop, source: str) -> frozenset[str]:
    normalized_source = normalize_external_source(source)
    if normalized_source is None:
        return frozenset()
    return frozenset(
        external_id
        for identity_source, external_id in _shop_external_identities(shop)
        if identity_source == normalized_source
    )


def _mention_identity_key(
    name: str,
    branch_name: str | None,
    area: str | None,
) -> tuple[str, str, str] | None:
    normalized_name = normalize_name(_name_with_branch(name, branch_name))
    normalized_branch = normalize_name(
        branch_name or extract_branch_token(name) or ""
    )
    canonical_area = canonicalize_area(area)
    if (
        not normalized_name
        or canonical_area is None
        or area_is_municipality(canonical_area)
    ):
        return None
    return normalized_name, normalized_branch, canonical_area


def _shop_identity_aliases(shop: Shop) -> frozenset[tuple[str, str, str]]:
    aliases: set[tuple[str, str, str]] = set()
    primary = _mention_identity_key(shop.shop_name, shop.branch_name, shop.area)
    if primary is not None:
        aliases.add(primary)
    for mention in shop.mentions:
        if mention.review_status != ReviewStatus.APPROVED.value:
            continue
        alias = _mention_identity_key(
            mention.extracted_name,
            mention.extracted_branch_name,
            mention.extracted_area,
        )
        if alias is not None:
            aliases.add(alias)
    return frozenset(aliases)


def _matches_mention_shop_identity(mention: ExtractedMention, shop: Shop) -> bool:
    mention_identity = _mention_identity_key(
        mention.shop_name,
        mention.branch_name,
        mention.area,
    )
    return mention_identity is not None and mention_identity in _shop_identity_aliases(shop)


def _find_existing_shop_from_mention(
    db: Session,
    mention: ExtractedMention,
    canonical_hint: str | None,
) -> tuple[Shop | None, str | None]:
    all_shops = db.query(Shop).all()
    approved_shop_ids = {
        shop_id
        for (shop_id,) in db.query(Shop.id)
        .filter(Shop.mentions.any(ShopMention.review_status == ReviewStatus.APPROVED.value))
        .all()
    }
    approved_shops = [shop for shop in all_shops if shop.id in approved_shop_ids]
    posted_source, posted_id = extract_external_identity(canonical_hint)
    if posted_source and posted_id:
        normalized_posted_id = posted_id.strip().casefold()
        all_external_matches = [
            shop
            for shop in all_shops
            if normalized_posted_id
            in _shop_external_ids_for_source(shop, posted_source)
        ]
        external_matches = [
            shop
            for shop in approved_shops
            if _shop_external_ids_for_source(shop, posted_source)
            == frozenset({normalized_posted_id})
            and _matches_mention_shop_identity(mention, shop)
        ]
        if len(all_external_matches) == 1 and len(external_matches) == 1:
            return external_matches[0], "投稿URLの外部IDと店名・支店名・エリアが既存店舗と一致"
        if len(all_external_matches) > 1:
            return None, "投稿URLの外部IDに複数の店舗が紐付いている"
        if all_external_matches and all_external_matches[0].id not in approved_shop_ids:
            return None, "投稿URLの外部IDが未承認店舗と競合"

    exact_matches = [
        shop for shop in all_shops if _matches_mention_shop_identity(mention, shop)
    ]
    if any(
        external_identities_conflict(_shop_external_identities(shop))
        for shop in exact_matches
    ):
        return None, "店名・支店名・エリア一致の既存店舗内で外部IDが矛盾"
    if posted_source and posted_id and any(
        external_ids
        and external_ids != frozenset({posted_id.strip().casefold()})
        for shop in exact_matches
        if (external_ids := _shop_external_ids_for_source(shop, posted_source))
    ):
        return None, "投稿URLの外部IDが店名・支店名・エリア一致の既存店舗と競合"
    approved_exact_matches = [
        shop
        for shop in exact_matches
        if shop.id in approved_shop_ids
    ]
    if len(approved_exact_matches) == 1:
        return approved_exact_matches[0], "店名・支店名・エリアが承認済み既存店舗と一意に一致"
    if len(approved_exact_matches) > 1:
        return None, "店名・支店名・エリアが一致する承認済み店舗が複数ある"
    if exact_matches:
        return None, "店名・支店名・エリアが未承認店舗と一致"

    return None, None


def _new_shop_collision_reason(
    db: Session,
    mention: ExtractedMention,
    candidate: PipelineCandidate,
) -> str | None:
    candidate_identities = _candidate_external_identities(candidate)
    if external_identities_conflict(candidate_identities):
        return "新規候補の外部IDが同一サービス内で矛盾"
    candidate_area = canonicalize_area(candidate.area) or canonicalize_area(mention.area)
    external_source, external_id = preferred_external_identity(
        external_source=candidate.external_source,
        external_id=candidate.external_id,
        urls=(candidate.canonical_url, candidate.evidence_url),
    )
    collision = find_shop_creation_collision(
        db,
        shop_name=candidate.name,
        branch_name=None,
        area=candidate_area,
        address=candidate.address,
        phone=candidate.phone,
        canonical_url=candidate.canonical_url,
        evidence_url=candidate.evidence_url,
        external_source=external_source,
        external_id=external_id,
    )
    if collision is not None:
        reasons = {
            "external_id": "新規候補の外部IDが既存店舗と競合",
            "canonical_url": "新規候補のcanonical URLまたは根拠URLが既存店舗と競合",
            "name_area": "新規候補の店名・支店名・エリアが既存店舗と競合",
            "strong_identity": "新規候補の電話または住所が既存店舗と強く一致",
        }
        return reasons[collision.kind]
    all_shops = db.query(Shop).all()
    strong_identity_matches = [
        shop
        for shop in all_shops
        if evaluate_identity(_shop_identity(shop), candidate).is_strong_match
    ]
    if strong_identity_matches:
        return "新規候補の電話・住所が既存店舗と強く一致"
    if any(_matches_mention_shop_identity(mention, shop) for shop in all_shops):
        return "新規候補の店名・支店名・エリアが既存店舗と競合"
    return None


def _guard_new_shop_creation(
    db: Session,
    mention: ExtractedMention,
    candidate: PipelineCandidate | None,
    blocking_reason: str | None,
) -> tuple[PipelineCandidate | None, str | None]:
    if blocking_reason is not None or candidate is None:
        return None, blocking_reason
    lock_new_shop_creation(db)
    collision_reason = _new_shop_collision_reason(db, mention, candidate)
    if collision_reason is not None:
        return None, collision_reason
    return candidate, None


def _resolve_verified_candidate_collision(
    db: Session,
    mention: ExtractedMention,
    candidate: PipelineCandidate,
) -> _CreationCollisionResolution:
    candidate_identities = _candidate_external_identities(candidate)
    if external_identities_conflict(candidate_identities):
        return _CreationCollisionResolution(
            None,
            "新規候補の外部IDが同一サービス内で矛盾",
            True,
        )
    candidate_area = canonicalize_area(candidate.area) or canonicalize_area(mention.area)
    external_source, external_id = preferred_external_identity(
        external_source=candidate.external_source,
        external_id=candidate.external_id,
        urls=(candidate.canonical_url, candidate.evidence_url),
    )
    collisions = find_shop_creation_collisions(
        db,
        shop_name=candidate.name,
        branch_name=None,
        area=candidate_area,
        address=candidate.address,
        phone=candidate.phone,
        canonical_url=candidate.canonical_url,
        evidence_url=candidate.evidence_url,
        external_source=external_source,
        external_id=external_id,
    )
    if not collisions:
        return _CreationCollisionResolution(None, None, False)
    shop_ids = {collision.shop_id for collision in collisions}
    if len(shop_ids) != 1:
        return _CreationCollisionResolution(
            None,
            "検証済み候補が複数の既存店舗と競合",
            True,
        )
    shop_id = next(iter(shop_ids))
    shop = db.query(Shop).filter(Shop.id == shop_id).one_or_none()
    if shop is None:
        return _CreationCollisionResolution(
            None,
            f"衝突先の既存店舗が見つからない: shop_id={shop_id}",
            True,
        )
    if not any(
        mention_row.review_status == ReviewStatus.APPROVED.value
        for mention_row in shop.mentions
    ):
        return _CreationCollisionResolution(
            None,
            f"検証済み候補が未承認店舗と競合: shop_id={shop_id}",
            True,
        )
    if not candidate.is_verified:
        return _CreationCollisionResolution(
            None,
            f"未検証候補が既存店舗と競合: shop_id={shop_id}",
            True,
        )
    if not any(collision.matched_by != "evidence_url" for collision in collisions):
        return _CreationCollisionResolution(
            None,
            f"根拠URLだけが既存店舗と一致: shop_id={shop_id}",
            True,
        )
    shop_identities = _shop_external_identities(shop)
    if external_identities_conflict(shop_identities):
        return _CreationCollisionResolution(
            None,
            f"既存店舗内で外部IDが矛盾: shop_id={shop_id}",
            True,
        )
    candidate_sources = {source for source, _external_id in candidate_identities}
    for source in candidate_sources:
        candidate_ids = {
            external_id
            for identity_source, external_id in candidate_identities
            if identity_source == source
        }
        shop_ids_for_source = {
            external_id
            for identity_source, external_id in shop_identities
            if identity_source == source
        }
        if shop_ids_for_source and candidate_ids.isdisjoint(shop_ids_for_source):
            return _CreationCollisionResolution(
                None,
                f"同一サービスの外部IDが既存店舗と矛盾: shop_id={shop_id}; source={source}",
                True,
            )
    has_exact_external_identity = bool(candidate_identities & shop_identities)
    outcome = evaluate_identity(_shop_identity(shop), candidate)
    if outcome.conflicting_fields:
        return _CreationCollisionResolution(
            None,
            f"検証済み候補と既存店舗が矛盾: shop_id={shop_id}; "
            f"fields={','.join(outcome.conflicting_fields)}",
            True,
        )
    shop_area = canonicalize_area(shop.area)
    if shop_area is not None and candidate_area is not None and shop_area != candidate_area:
        return _CreationCollisionResolution(
            None,
            f"検証済み候補と既存店舗のエリアが矛盾: shop_id={shop_id}",
            True,
        )
    mention_branch = _mention_branch(mention)
    shop_branch = _shop_branch(shop)
    name_area_collision = any(
        collision.matched_by == "name_area" for collision in collisions
    )
    if branches_conflict(
        _name_with_branch(mention.shop_name, mention.branch_name),
        _name_with_branch(shop.shop_name, shop.branch_name),
    ) or (
        name_area_collision
        and bool(mention_branch) != bool(shop_branch)
        and not has_exact_external_identity
    ):
        return _CreationCollisionResolution(
            None,
            f"検証済み候補と既存店舗の支店名が矛盾: shop_id={shop_id}",
            True,
        )
    return _CreationCollisionResolution(
        shop,
        f"検証済み候補の衝突先が承認済み既存店舗一件: shop_id={shop_id}",
        True,
    )


def _guard_new_shop_creation_or_link(
    db: Session,
    mention: ExtractedMention,
    candidate: PipelineCandidate | None,
    blocking_reason: str | None,
) -> tuple[PipelineCandidate | None, Shop | None, str | None]:
    if blocking_reason is not None or candidate is None:
        return None, None, blocking_reason
    lock_new_shop_creation(db)
    resolution = _resolve_verified_candidate_collision(db, mention, candidate)
    if resolution.shop is not None:
        return None, resolution.shop, resolution.blocking_reason
    if resolution.has_collision:
        return None, None, resolution.blocking_reason
    collision_reason = _new_shop_collision_reason(db, mention, candidate)
    if collision_reason is not None:
        return None, None, collision_reason
    return candidate, None, None


def _candidate_canonical_area(candidate: CandidateIdentity) -> str | None:
    if not candidate.address:
        return None
    area = area_from_address(candidate.address)
    if area is None or not _candidate_address_precisely_matches_area(
        area, candidate.address
    ):
        return None
    if candidate.area and not _candidate_area_context_matches(area, candidate.area):
        return None
    return area


def _effective_candidate_area(
    mention: ExtractedMention,
    candidate: CandidateIdentity,
) -> str | None:
    mention_area = canonicalize_area(mention.area)
    if mention_area is not None and not area_is_municipality(mention_area):
        return mention_area
    candidate_area = _candidate_canonical_area(candidate)
    if candidate_area is None:
        return None
    if mention.area and not _candidate_area_context_matches(candidate_area, mention.area):
        return None
    return candidate_area


def _matches_mention_name_and_area(
    mention: ExtractedMention,
    candidate: CandidateIdentity,
) -> bool:
    mention_area = _effective_candidate_area(mention, candidate)
    if mention_area is None:
        return False
    expected_name = normalize_name(
        _name_with_branch(mention.shop_name, mention.branch_name)
    )
    candidate_area_matches = _candidate_matches_area(mention_area, candidate)
    return (
        normalize_name(candidate.name) == expected_name
        and candidate_area_matches
        and not branches_conflict(
            _name_with_branch(mention.shop_name, mention.branch_name),
            candidate.name,
        )
    )


def _candidate_matches_area(
    mention_area: str,
    candidate: CandidateIdentity,
) -> bool:
    candidate_area = canonicalize_area(candidate.area)
    if candidate_area is not None:
        if candidate_area != mention_area and (
            area_is_municipality(candidate_area)
            or area_is_municipality(mention_area)
        ):
            return bool(
                candidate.address
                and _candidate_area_context_matches(mention_area, candidate.area or "")
                and _candidate_address_matches_area(mention_area, candidate.address)
                and _candidate_address_matches_area_group(candidate_area, candidate.address)
            )
        return candidate_area == mention_area and (
            not candidate.address
            or _candidate_address_matches_area_group(
                mention_area,
                candidate.address,
            )
        )
    if candidate.area and _candidate_area_has_conflict(
        mention_area,
        candidate.area,
        candidate.address,
    ):
        return False
    if _candidate_address_matches_area(mention_area, candidate.address):
        return True
    return bool(
        candidate.area
        and normalize_name(_area_leaf(mention_area)) in normalize_name(candidate.area)
        and _candidate_area_context_matches(mention_area, candidate.area)
        and _candidate_address_matches_area_group(mention_area, candidate.address)
    )


def _area_leaf(area: str) -> str:
    return area.rsplit(" / ", 1)[-1]


_LEAF_AREAS: tuple[tuple[str, str], ...] = tuple(
    (area, normalize_name(_area_leaf(area)))
    for area in sorted(CANONICAL_AREAS)
    if not area_is_municipality(area)
)


def _canonical_areas_in_text(value: str) -> frozenset[str]:
    normalized = normalize_name(value)
    matches: list[tuple[int, int, str]] = []
    for area, token in _LEAF_AREAS:
        offset = 0
        while token and (start := normalized.find(token, offset)) >= 0:
            end = start + len(token)
            if end == len(normalized) or normalized[end] not in "都道府県市区町村":
                matches.append((start, end, area))
            offset = end
    selected: list[tuple[int, int, str]] = []
    for start, end, area in sorted(
        matches,
        key=lambda item: (-(item[1] - item[0]), item[0], item[2]),
    ):
        if any(
            start < chosen_end
            and end > chosen_start
            and (start, end) != (chosen_start, chosen_end)
            for chosen_start, chosen_end, _ in selected
        ):
            continue
        selected.append((start, end, area))
    return frozenset(area for _, _, area in selected)


def _canonical_areas_in_expected_group(
    value: str,
    mention_area: str,
) -> frozenset[str]:
    expected_group = AREA_TO_GROUP[mention_area]
    return frozenset(
        area
        for area in _canonical_areas_in_text(value)
        if AREA_TO_GROUP[area] == expected_group
    )


def _area_group_parts(area: str) -> tuple[str, str | None]:
    group = AREA_TO_GROUP[area]
    if " / " not in group:
        return group, None
    region, parent = group.split(" / ", 1)
    return region, parent


def _prefecture_region(prefecture: str) -> str:
    if prefecture == "北海道":
        return prefecture
    return prefecture[:-1]


def _expected_prefecture(region: str) -> str:
    return PREFECTURE_NAMES[region]


def _prefectures_in_text(value: str) -> tuple[str, ...]:
    pattern = "|".join(re.escape(prefecture) for prefecture in sorted(PREFECTURES))
    return tuple(re.findall(pattern, normalize_name(value)))


def _candidate_area_regions(value: str) -> frozenset[str]:
    prefectures = _prefectures_in_text(value)
    regions: set[str] = set()
    remainder = normalize_name(value)
    for municipality in sorted(
        municipalities_in_text(value), key=lambda row: len(row.name), reverse=True
    ):
        prefix = normalize_name(f"{municipality.prefecture}{municipality.name}")
        if remainder.startswith(prefix):
            remainder = normalize_name(municipality.prefecture) + remainder[len(prefix):]
            break
    for prefecture in prefectures:
        regions.add(normalize_name(_prefecture_region(prefecture)))
        remainder = remainder.replace(normalize_name(prefecture), "", 1)
    for prefecture in PREFECTURES:
        region = normalize_name(_prefecture_region(prefecture))
        if region and region in remainder:
            regions.add(region)
    return frozenset(regions)


def _has_multiple_geographic_values(value: str) -> bool:
    if canonicalize_area(value) is not None:
        return False
    parts = tuple(
        normalize_name(part)
        for part in GEOGRAPHIC_VALUE_SEPARATOR_RE.split(value)
        if normalize_name(part)
    )
    geographic_parts = tuple(
        part
        for part in parts
        if any(normalize_name(prefecture) in part for prefecture in PREFECTURES)
        or any(suffix in part for suffix in "市区町村")
        or part in PREFECTURE_NAMES
        or bool(_canonical_areas_in_text(part))
    )
    if len(geographic_parts) > 1:
        return True
    admin_parts = tuple(
        normalize_name(part)
        for part in ADMIN_VALUE_BOUNDARY_RE.split(value)
        if normalize_name(part)
    )
    for index, part in enumerate(admin_parts[1:], start=1):
        prefix = "".join(admin_parts[:index])
        token_match = BOUNDARY_ADMIN_TOKEN_RE.match(part)
        if token_match is None:
            continue
        admin_name, level = token_match.groups()
        for separator in "都道府県郡":
            if separator in admin_name:
                admin_name = admin_name.rsplit(separator, 1)[1]
        if level in "町村" and len(admin_name) < 2:
            continue
        token = f"{admin_name}{level}"
        if token in prefix:
            continue
        if level == "市" and any(suffix in prefix for suffix in "市区町村"):
            return True
        if level == "区" and any(suffix in prefix for suffix in "区町村"):
            return True
        if level in "町村" and any(suffix in prefix for suffix in "町村"):
            return True
    return False


def _candidate_area_region_matches(value: str, expected_region: str) -> bool:
    regions = _candidate_area_regions(value)
    return not regions or regions == {normalize_name(expected_region)}


def _address_prefecture_matches(value: str, expected_region: str) -> bool:
    return _prefectures_in_text(value) == (_expected_prefecture(expected_region),)


def _admin_parents_in_text(value: str, region: str) -> frozenset[str]:
    normalized = normalize_name(value)
    return frozenset(
        parent
        for parent in ADMIN_PARENTS_BY_REGION.get(region, frozenset())
        if normalize_name(parent) in normalized
    )


def _text_has_non_admin_leaf(value: str, leaf: str) -> bool:
    offset = 0
    while (start := value.find(leaf, offset)) >= 0:
        end = start + len(leaf)
        if end == len(value) or value[end] not in "都道府県市区町村":
            return True
        offset = end
    return False


def _candidate_area_context_matches(mention_area: str, value: str) -> bool:
    if _has_multiple_geographic_values(value):
        return False
    normalized = normalize_name(value)
    municipality = area_municipality(mention_area)
    canonical_value = canonicalize_area(value)
    value_municipality = area_municipality(canonical_value) if canonical_value else None
    if (
        municipality is None
        and value_municipality is not None
        and mention_area in BROAD_AREA_ADMIN_LOCALITIES
    ):
        region, _parent = _area_group_parts(mention_area)
        return (
            value_municipality.prefecture == _expected_prefecture(region)
            and value_municipality.municipality in BROAD_AREA_ADMIN_LOCALITIES[mention_area]
        )
    if municipality is not None and value_municipality is not None:
        if municipality.prefecture != value_municipality.prefecture:
            return False
        if (
            municipality.code != value_municipality.code
            and municipality.name != value_municipality.city
            and value_municipality.name != municipality.city
        ):
            return False
        if area_is_municipality(canonical_value or ""):
            return True
        if area_is_municipality(mention_area):
            return True
    embedded_areas = (
        frozenset()
        if area_is_municipality(mention_area)
        else _canonical_areas_in_expected_group(value, mention_area)
    )
    if embedded_areas and embedded_areas != frozenset({mention_area}):
        return False
    region, parent = _area_group_parts(mention_area)
    if not _candidate_area_region_matches(value, region):
        return False
    present_parents = _admin_parents_in_text(value, region)
    if (
        municipality is None
        and mention_area not in BROAD_AREA_ADMIN_LOCALITIES
        and present_parents
        and (parent is None or present_parents != frozenset({parent}))
    ):
        return False
    normalized_leaf = normalize_name(_area_leaf(mention_area))
    if region == "東京":
        return bool(
            (parent and normalize_name(parent) in normalized)
            or _text_has_non_admin_leaf(normalized, normalized_leaf)
        )
    return bool(
        normalized_leaf in normalized
        or (parent and normalize_name(parent) in normalized)
        or normalize_name(region) in normalized
    )


def _geographic_text_has_area_conflict(
    mention_area: str,
    value: str | None,
) -> bool:
    if value and _has_multiple_geographic_values(value):
        return True
    normalized_value = normalize_address(value)
    if not normalized_value:
        return False
    region, parent = _area_group_parts(mention_area)
    municipality = area_municipality(mention_area)
    if municipality is None and mention_area in BROAD_AREA_ADMIN_LOCALITIES:
        if any(
            present.municipality not in BROAD_AREA_ADMIN_LOCALITIES[mention_area]
            for present in municipalities_in_text(value or "")
        ):
            return True
    if municipality is not None:
        canonical_value = canonicalize_area(value)
        value_municipality = (
            area_municipality(canonical_value) if canonical_value else None
        )
        present_municipalities = (
            municipalities_in_text(value or "")
            if canonical_value is not None
            or any(prefecture in normalized_value for prefecture in PREFECTURES)
            else ()
        )
        if value_municipality is not None:
            present_municipalities = (*present_municipalities, value_municipality)
        for present in present_municipalities:
            if (
                present.prefecture != municipality.prefecture
                or (
                    present.code != municipality.code
                    and present.name != municipality.city
                    and municipality.name != present.city
                )
            ):
                return True
    present_prefectures = _prefectures_in_text(normalized_value)
    if (
        len(present_prefectures) > 1
        or present_prefectures
        and present_prefectures != (_expected_prefecture(region),)
    ):
        return True
    if not _candidate_area_region_matches(normalized_value, region):
        return True
    present_parents = _admin_parents_in_text(normalized_value, region)
    if (
        municipality is None
        and mention_area not in BROAD_AREA_ADMIN_LOCALITIES
        and present_parents
        and (parent is None or present_parents != frozenset({parent}))
    ):
        return True
    embedded_areas = (
        frozenset()
        if area_is_municipality(mention_area)
        else _canonical_areas_in_expected_group(normalized_value, mention_area)
    )
    return bool(embedded_areas and embedded_areas != frozenset({mention_area}))


def _candidate_area_has_conflict(
    mention_area: str,
    value: str,
    address: str | None,
) -> bool:
    if _geographic_text_has_area_conflict(mention_area, value):
        return True
    normalized = normalize_name(value)
    for prefecture in PREFECTURES:
        normalized = normalized.replace(normalize_name(prefecture), "")
    region, _parent = _area_group_parts(mention_area)
    expected_localities = tuple(
        sorted(
            (normalize_name(locality) for locality in _area_admin_localities(mention_area)),
            key=len,
            reverse=True,
        )
    )
    normalized_region = normalize_name(region)
    if normalized.startswith(normalized_region) and not any(
        normalized.startswith(locality) for locality in expected_localities
    ):
        normalized = normalized[len(normalized_region) :]
    normalized_leaf = normalize_name(_area_leaf(mention_area))
    administrative_remainder = normalized.replace(normalized_leaf, "", 1)
    if not any(suffix in administrative_remainder for suffix in "市区町村"):
        return False
    normalized_address = normalize_address(address)
    if not normalized_address:
        return True
    normalized_prefecture = normalize_name(_expected_prefecture(region))
    prefecture_index = normalized_address.find(normalized_prefecture)
    if prefecture_index < 0:
        return True
    address_tail = normalized_address[prefecture_index + len(normalized_prefecture) :]
    for locality in expected_localities:
        if locality not in normalized:
            continue
        remainder = normalized.replace(locality, "", 1)
        if any(suffix in remainder for suffix in "市町村"):
            return True
        if "区" in remainder:
            return not address_tail.startswith(f"{locality}{remainder}")
        return bool(remainder and remainder not in normalized_address)
    if any(suffix in administrative_remainder for suffix in "市町村"):
        return True
    if administrative_remainder.count("区") != 1:
        return True
    return not any(
        locality.endswith("市")
        and address_tail.startswith(f"{locality}{administrative_remainder}")
        for locality in expected_localities
    )


def _area_admin_localities(area: str) -> frozenset[str]:
    if area in BROAD_AREA_ADMIN_LOCALITIES:
        return BROAD_AREA_ADMIN_LOCALITIES[area]
    municipality = area_municipality(area)
    if municipality is not None:
        return frozenset(
            {municipality.name, f"{municipality.city}{municipality.municipality}"}
        )
    _region, parent = _area_group_parts(area)
    if parent is not None:
        return frozenset({parent})
    return BROAD_AREA_ADMIN_LOCALITIES.get(area, frozenset())


def _address_has_expected_admin_locality(
    mention_area: str,
    normalized_address: str,
) -> bool:
    region, _parent = _area_group_parts(mention_area)
    normalized_prefecture = normalize_name(_expected_prefecture(region))
    prefecture_index = normalized_address.find(normalized_prefecture)
    if prefecture_index < 0:
        return False
    address_tail = normalized_address[prefecture_index + len(normalized_prefecture) :]
    for locality in _area_admin_localities(mention_area):
        normalized_locality = normalize_address(locality)
        if normalized_locality is None:
            continue
        if address_tail.startswith(normalized_locality):
            return True
        county_match = re.match(r"^[^市区町村]{1,20}郡", address_tail)
        if county_match and address_tail[county_match.end() :].startswith(
            normalized_locality
        ):
            return True
    return False


def _candidate_address_matches_area_group(
    mention_area: str,
    address: str | None,
) -> bool:
    normalized_address = normalize_address(address)
    if not normalized_address:
        return False
    if _geographic_text_has_area_conflict(mention_area, address):
        return False
    region, _parent = _area_group_parts(mention_area)
    if not _address_prefecture_matches(normalized_address, region):
        return False
    return _address_has_expected_admin_locality(mention_area, normalized_address)


def _candidate_address_matches_area(
    mention_area: str,
    address: str | None,
) -> bool:
    normalized_address = normalize_address(address)
    if not normalized_address or not _candidate_address_matches_area_group(
        mention_area,
        address,
    ):
        return False
    if area_is_municipality(mention_area):
        return True
    region, _parent = _area_group_parts(mention_area)
    embedded_areas = _canonical_areas_in_expected_group(
        normalized_address,
        mention_area,
    )
    if embedded_areas and embedded_areas != frozenset({mention_area}):
        return False
    if mention_area in BROAD_AREA_ADMIN_LOCALITIES:
        return True
    normalized_leaf = normalize_name(_area_leaf(mention_area))
    return bool(
        normalized_leaf in normalized_address
        if region != "東京"
        else _text_has_non_admin_leaf(normalized_address, normalized_leaf)
    )


def _candidate_address_precisely_matches_area(
    mention_area: str,
    address: str | None,
) -> bool:
    normalized_address = normalize_address(address)
    if not normalized_address or not _candidate_address_matches_area_group(
        mention_area,
        address,
    ):
        return False
    geographic_address = re.sub(
        r"^〒?[0-9]{3}-?[0-9]{4}",
        "",
        normalized_address,
    )
    street_number = re.search(r"[0-9]", geographic_address)
    if street_number is None:
        return False
    geographic_prefix = geographic_address[: street_number.start()]
    if area_is_municipality(mention_area):
        municipality = area_municipality(mention_area)
        if municipality is None:
            return False
        locality = normalize_address(f"{municipality.prefecture}{municipality.name}")
        return bool(
            locality
            and geographic_prefix.startswith(locality)
            and geographic_prefix[len(locality):]
        )
    embedded_areas = _canonical_areas_in_expected_group(
        geographic_prefix,
        mention_area,
    )
    if embedded_areas and embedded_areas != frozenset({mention_area}):
        return False
    region, _parent = _area_group_parts(mention_area)
    normalized_leaves = tuple(
        normalize_name(value)
        for value in PRECISE_AREA_ADDRESS_ALIASES.get(
            mention_area,
            frozenset({_area_leaf(mention_area)}),
        )
    )
    return bool(
        any(leaf in geographic_prefix for leaf in normalized_leaves)
        if region != "東京"
        else any(
            _text_has_non_admin_leaf(geographic_prefix, leaf)
            for leaf in normalized_leaves
        )
    )


def _fallback_confidence_reason(legacy_reason: str, fresh_reason: str) -> str:
    fresh_part = f"新規抽出結果: {fresh_reason}"[:1_000]
    legacy_limit = 2_000 - len(fresh_part) - 3
    legacy_part = legacy_reason[: max(0, legacy_limit)]
    return f"{fresh_part} / {legacy_part}" if legacy_part else fresh_part


def _candidate_branch_matches_mention(
    mention: ExtractedMention,
    candidate_name: str,
) -> bool:
    if not mention.branch_name and extract_branch_token(mention.shop_name):
        return False
    expected_branch = normalize_name(mention.branch_name or "")
    candidate_branch = extract_branch_token(candidate_name)
    if candidate_branch and not expected_branch:
        return False
    if not expected_branch:
        return normalize_name(candidate_name) == normalize_name(mention.shop_name)
    candidate_parts = tuple(
        normalized
        for part in BRANCH_COMPONENT_SEPARATOR_RE.split(candidate_name)
        if (normalized := normalize_name(part))
    )
    if len(candidate_parts) >= 2:
        return candidate_parts[-1] == expected_branch
    return normalize_name(candidate_name) == normalize_name(
        _name_with_branch(mention.shop_name, mention.branch_name)
    )


def _candidate_for_new_shop(
    mention: ExtractedMention,
    candidates: list[PipelineCandidate],
) -> PipelineCandidate | None:
    structured_matches = [
        candidate
        for candidate in candidates
        if candidate.provenance == CandidateProvenance.STRUCTURED_DATA
        and candidate.is_verified
        and _matches_mention_name_and_area(mention, candidate)
    ]
    if len(structured_matches) != 1:
        return None
    selected = structured_matches[0]
    if _structured_candidates_with_shared_source_conflict(
        mention,
        selected,
        candidates,
    ):
        return None
    canonical_area = _effective_candidate_area(mention, selected)
    if canonical_area is None:
        return None
    return selected.model_copy(update={"area": canonical_area})


def _structured_candidate_block_reason(
    mention: ExtractedMention,
    candidates: list[PipelineCandidate],
) -> str | None:
    structured_matches = [
        candidate
        for candidate in candidates
        if candidate.provenance == CandidateProvenance.STRUCTURED_DATA
        and candidate.is_verified
        and _matches_mention_name_and_area(mention, candidate)
    ]
    if len(structured_matches) > 1:
        return "店名・支店名・エリアが一致する構造化候補が複数ある"
    if len(structured_matches) == 1 and _structured_candidates_with_shared_source_conflict(
        mention,
        structured_matches[0],
        candidates,
    ):
        return "同じ構造化データ出典の店舗候補が矛盾"
    return None


def _source_evidence_url_identity(value: str) -> str:
    external_source, external_id = extract_external_identity(value)
    if external_source and external_id:
        return (
            f"external:{normalize_external_source(external_source)}:"
            f"{external_id.strip().casefold()}"
        )
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    hostname = SOURCE_EVIDENCE_HOST_ALIASES.get(hostname, hostname)
    path = parsed.path.rstrip("/") or "/"
    if hostname == "x.com":
        status_match = re.search(r"/(?:[^/]+/)*status/([0-9]+)", path)
        if status_match:
            return f"x.com/status/{status_match.group(1)}"
    if hostname == "youtu.be":
        video_id = path.strip("/").split("/", 1)[0]
        if video_id:
            return f"youtube.com/video/{video_id}"
    if hostname in {"youtube.com", "m.youtube.com"}:
        query_video_id = parse_qs(parsed.query).get("v", [""])[0]
        path_match = re.match(r"/(?:shorts|embed|live)/([^/]+)", path)
        video_id = query_video_id or (path_match.group(1) if path_match else "")
        if video_id:
            return f"youtube.com/video/{video_id}"
    return f"{hostname}{path}"


def _candidate_has_cited_web_source(
    candidate: CandidateIdentity,
    source_urls: tuple[str, ...],
) -> bool:
    candidate_identities = frozenset(
        _citation_url_identity(url)
        for url in (candidate.evidence_url, candidate.canonical_url)
        if url
    )
    source_identities = frozenset(
        _citation_url_identity(url) for url in source_urls
    )
    return bool(candidate_identities & source_identities)


def _sanitize_cited_web_candidate(
    candidate: SearchCandidate,
    source_urls: tuple[str, ...],
) -> SearchCandidate | None:
    source_identities = frozenset(
        _citation_url_identity(url) for url in source_urls
    )
    canonical_url = (
        candidate.canonical_url
        if candidate.canonical_url
        and _citation_url_identity(candidate.canonical_url) in source_identities
        else None
    )
    evidence_url = (
        candidate.evidence_url
        if candidate.evidence_url
        and _citation_url_identity(candidate.evidence_url) in source_identities
        else None
    )
    if canonical_url is None and evidence_url is None:
        return None
    external_source, external_id = preferred_external_identity(
        external_source=None,
        external_id=None,
        urls=(canonical_url, evidence_url),
    )
    return candidate.model_copy(
        update={
            "canonical_url": canonical_url,
            "evidence_url": evidence_url,
            "external_source": external_source,
            "external_id": external_id,
        }
    )


def _citation_url_identity(value: str) -> str:
    source_identity = _source_evidence_url_identity(value)
    if source_identity.startswith("external:") or source_identity.startswith(
        ("x.com/status/", "youtube.com/video/")
    ):
        return source_identity
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in {"fbclid", "gclid", "yclid"}
        )
    )
    return f"{hostname}{path}{f'?{query}' if query else ''}"


def _candidate_source_identity_keys(
    candidate: CandidateIdentity,
) -> frozenset[str]:
    url_identities = {
        f"url:{_citation_url_identity(url)}"
        for url in (candidate.canonical_url, candidate.evidence_url)
        if url
    }
    external_identities = {
        f"external:{source}:{external_id}"
        for source, external_id in _candidate_external_identities(candidate)
    }
    return frozenset(url_identities | external_identities)


def _structured_candidates_with_shared_source_conflict(
    mention: ExtractedMention,
    selected: PipelineCandidate,
    candidates: list[PipelineCandidate],
) -> bool:
    selected_keys = _candidate_source_identity_keys(selected)
    if not selected_keys or external_identities_conflict(
        _candidate_external_identities(selected)
    ):
        return True
    for other in candidates:
        if (
            other is selected
            or other.provenance != CandidateProvenance.STRUCTURED_DATA
            or not other.is_verified
            or not selected_keys & _candidate_source_identity_keys(other)
        ):
            continue
        if external_identities_conflict(_candidate_external_identities(other)):
            return True
        if not _matches_mention_name_and_area(mention, other):
            return True
        if evaluate_identity(selected, other).conflicting_fields:
            return True
    return False


def _web_candidates_with_same_identity_conflict(
    mention: ExtractedMention,
    selected: PipelineCandidate,
    candidates: list[PipelineCandidate],
) -> bool:
    selected_identities = _candidate_external_identities(selected)
    if not selected_identities:
        return False
    mention_area = _effective_candidate_area(mention, selected)
    for other in candidates:
        if other is selected or other.provenance != CandidateProvenance.WEB_SEARCH:
            continue
        if not selected_identities & _candidate_external_identities(other):
            continue
        if not _candidate_branch_matches_mention(mention, other.name):
            return True
        if mention_area is None or not _candidate_matches_area(mention_area, other):
            return True
        if evaluate_identity(selected, other).conflicting_fields:
            return True
    return False


def _source_evidence_has_unresolved_redirect(
    urls: tuple[str, ...],
) -> bool:
    return any(
        (urlparse(url).hostname or "").lower().removeprefix("www.")
        in SOURCE_EVIDENCE_UNRESOLVED_REDIRECT_HOSTS
        for url in urls
    )


def _web_candidate_conflicts_with_structured_data(
    mention: ExtractedMention,
    mention_area: str,
    selected: PipelineCandidate,
    candidates: list[PipelineCandidate],
    source_url_identities: frozenset[str],
) -> bool:
    if _structured_candidate_block_reason(mention, candidates) is not None:
        return True
    selected_url_identities = frozenset(
        _source_evidence_url_identity(url)
        for url in (selected.evidence_url, selected.canonical_url)
        if url
    )
    for structured in candidates:
        if (
            structured.provenance != CandidateProvenance.STRUCTURED_DATA
            or not structured.is_verified
        ):
            continue
        structured_url_identities = frozenset(
            _source_evidence_url_identity(url)
            for url in (structured.evidence_url, structured.canonical_url)
            if url
        )
        comes_from_original_source = bool(
            structured_url_identities & source_url_identities
        )
        is_original_source_candidate = comes_from_original_source
        same_external_identity = bool(
            selected.external_source
            and selected.external_id
            and structured.external_source
            and structured.external_id
            and normalize_external_source(selected.external_source)
            == normalize_external_source(structured.external_source)
            and selected.external_id.strip().casefold()
            == structured.external_id.strip().casefold()
        )
        if not (
            selected_url_identities & structured_url_identities
            or same_external_identity
            or is_original_source_candidate
        ):
            continue
        if not _candidate_branch_matches_mention(mention, structured.name):
            return True
        if (
            not mention.branch_name
            and normalize_name(structured.name)
            != normalize_name(mention.shop_name)
        ):
            return True
        outcome = evaluate_identity(selected, structured)
        if set(outcome.conflicting_fields) - {"address"}:
            return True
        selected_address = re.sub(
            r"^〒?[0-9]{3}-?[0-9]{4}",
            "",
            normalize_address(selected.address) or "",
        )
        structured_address = re.sub(
            r"^〒?[0-9]{3}-?[0-9]{4}",
            "",
            normalize_address(structured.address) or "",
        )
        if (
            selected_address
            and structured_address
            and selected_address != structured_address
        ):
            return True
        structured_area = canonicalize_area(structured.area)
        if structured_area is not None and structured_area != mention_area:
            return True
        if structured.address and not _candidate_address_matches_area_group(
            mention_area,
            structured.address,
        ):
            return True
        if (
            selected.external_source
            and selected.external_id
            and structured.external_source
            and structured.external_id
            and normalize_external_source(selected.external_source)
            == normalize_external_source(structured.external_source)
            and selected.external_id.strip().casefold()
            != structured.external_id.strip().casefold()
        ):
            return True
    return False


def _find_existing_shop_after_candidate_confirmation(
    db: Session,
    mention: ExtractedMention,
    candidate: PipelineCandidate,
) -> tuple[Shop | None, str | None]:
    candidate_area = _effective_candidate_area(mention, candidate)
    candidate_mention = ExtractedMention(
        shop_name=mention.shop_name,
        branch_name=mention.branch_name,
        area=candidate_area or mention.area,
        category=candidate.category or mention.category,
        needs_review=True,
        confidence_reason=candidate.verification_reason or mention.confidence_reason,
    )
    candidate_identities = _candidate_external_identities(candidate)
    if external_identities_conflict(candidate_identities):
        return None, "候補の外部IDが同一サービス内で矛盾"
    all_shops = db.query(Shop).all()
    approved_shop_ids = {
        shop_id
        for (shop_id,) in db.query(Shop.id)
        .filter(
            Shop.mentions.any(
                ShopMention.review_status == ReviewStatus.APPROVED.value
            )
        )
        .all()
    }
    evidence_matches = {
        shop.id: shop
        for shop in _candidate_evidence_matching_shops(all_shops, candidate)
    }
    if any(
        external_identities_conflict(_shop_external_identities(shop))
        for shop in evidence_matches.values()
    ):
        return None, "候補の根拠に一致する既存店舗内で外部IDが矛盾"
    approved_evidence_matches = tuple(
        shop
        for shop in evidence_matches.values()
        if shop.id in approved_shop_ids
        and _matches_mention_shop_identity(candidate_mention, shop)
    )
    if len(evidence_matches) == 1 and len(approved_evidence_matches) == 1:
        return (
            approved_evidence_matches[0],
            "候補の外部IDまたはURLと店名・支店名・エリアが既存店舗と一致",
        )
    if len(evidence_matches) > 1:
        return None, "候補の外部IDまたはURLが複数の既存店舗に紐付いている"
    if evidence_matches:
        return None, "候補の外部IDまたはURLが未承認店舗または異なる店舗情報と競合"
    return _find_existing_shop_from_mention(db, candidate_mention, None)


def _top_web_candidate_for_new_shop(
    mention: ExtractedMention,
    candidates: list[PipelineCandidate],
    *,
    require_precise_area_evidence: bool = False,
    excluded_evidence_urls: tuple[str, ...] = (),
) -> PipelineCandidate | None:
    mention_name = _name_with_branch(mention.shop_name, mention.branch_name)
    if _source_evidence_has_unresolved_redirect(excluded_evidence_urls):
        return None
    excluded_url_identities = frozenset(
        _source_evidence_url_identity(url)
        for url in excluded_evidence_urls
    )
    scored = [
        (candidate, name_similarity(mention_name, candidate.name))
        for candidate in candidates
        if candidate.provenance == CandidateProvenance.WEB_SEARCH
    ]
    if not scored:
        return None
    top_score = max(score for _, score in scored)
    top_candidates = [candidate for candidate, score in scored if score == top_score]
    if top_score < TOP_WEB_MATCHING_SCORE or len(top_candidates) != 1:
        return None
    top_candidate = top_candidates[0]
    if external_identities_conflict(_candidate_external_identities(top_candidate)):
        return None
    if _web_candidates_with_same_identity_conflict(
        mention,
        top_candidate,
        candidates,
    ):
        return None
    mention_area = _effective_candidate_area(mention, top_candidate)
    if mention_area is None:
        return None
    candidate_evidence_urls = tuple(
        url
        for url in (top_candidate.evidence_url, top_candidate.canonical_url)
        if url
    )
    if not candidate_evidence_urls:
        return None
    if _source_evidence_has_unresolved_redirect(candidate_evidence_urls):
        return None
    candidate_url_identities = frozenset(
        _source_evidence_url_identity(url)
        for url in candidate_evidence_urls
    )
    if candidate_url_identities & excluded_url_identities:
        return None
    if require_precise_area_evidence and (
        not top_candidate.address
        or not _candidate_address_precisely_matches_area(
            mention_area,
            top_candidate.address,
        )
    ):
        return None
    if not _candidate_branch_matches_mention(mention, top_candidate.name):
        return None
    if (
        require_precise_area_evidence
        and not mention.branch_name
        and normalize_name(top_candidate.name)
        != normalize_name(mention.shop_name)
    ):
        return None
    if not _candidate_matches_area(mention_area, top_candidate):
        return None
    if _web_candidate_conflicts_with_structured_data(
        mention,
        mention_area,
        top_candidate,
        candidates,
        excluded_url_identities,
    ):
        return None
    return top_candidate.model_copy(
        update={
            "area": mention_area,
            "verification_reason": (
                f"policy={TOP_WEB_MATCHING_POLICY}; "
                f"matching_score={top_score:.3f}; canonical_area={mention_area}; "
                "source_verified=false"
            )
        }
    )


def _page_candidate_cache_value(
    mention: ExtractedMention,
    candidate: PipelineCandidate,
    citation_url: str,
) -> str:
    values = (
        _citation_url_identity(citation_url),
        normalize_name(mention.shop_name),
        normalize_name(mention.branch_name or ""),
        normalize_area(mention.area),
        normalize_name(candidate.name),
        normalize_address(candidate.address) or "",
    )
    return "|".join(values)


def _page_verified_candidate_is_valid(
    mention: ExtractedMention,
    candidate: PipelineCandidate,
) -> bool:
    mention_area = _effective_candidate_area(mention, candidate)
    return bool(
        candidate.provenance == CandidateProvenance.WEB_SEARCH
        and candidate.is_verified
        and mention_area is not None
        and candidate.address
        and _candidate_address_precisely_matches_area(
            mention_area,
            candidate.address,
        )
        and _candidate_branch_matches_mention(mention, candidate.name)
        and _candidate_matches_area(mention_area, candidate)
    )


def _verify_web_candidate_from_structured_page(
    mention: ExtractedMention,
    candidate: PipelineCandidate,
    candidates: list[PipelineCandidate],
) -> PipelineCandidate | None:
    source_keys = _candidate_source_identity_keys(candidate)
    if not source_keys or not candidate.address:
        return None
    page_candidates = [
        item
        for item in candidates
        if item.provenance == CandidateProvenance.STRUCTURED_DATA
        and item.is_verified
        and source_keys & _candidate_source_identity_keys(item)
    ]
    if len(page_candidates) != 1:
        return None
    page_candidate = page_candidates[0]
    if (
        normalize_name(page_candidate.name) != normalize_name(candidate.name)
        or not page_addresses_match(page_candidate.address, candidate.address)
    ):
        return None
    canonical_area = _effective_candidate_area(mention, page_candidate)
    if canonical_area is None:
        return None
    verified_identity = page_candidate.model_copy(
        update={
            "area": canonical_area,
            "provenance": CandidateProvenance.WEB_SEARCH,
            "is_verified": True,
            "verification_reason": (
                f"policy={TOP_WEB_MATCHING_POLICY}; source_verified=true; "
                "matching_score="
                f"{name_similarity(_name_with_branch(mention.shop_name, mention.branch_name), candidate.name):.3f}; "
                "proof=structured_data_cache"
            ),
        }
    )
    return (
        verified_identity
        if _page_verified_candidate_is_valid(mention, verified_identity)
        else None
    )


async def _verify_web_candidate_page(
    db: Session,
    message_id: str,
    mention: ExtractedMention,
    candidate: PipelineCandidate,
    *,
    candidates: list[PipelineCandidate],
    excluded_evidence_urls: tuple[str, ...] = (),
) -> tuple[PipelineCandidate | None, str | None]:
    structured_verified = _verify_web_candidate_from_structured_page(
        mention,
        candidate,
        candidates,
    )
    if structured_verified is not None:
        return structured_verified, None
    citation_url = candidate.canonical_url or candidate.evidence_url
    if citation_url is None:
        return None, "Web候補に引用済み店舗URLがない"
    if _source_evidence_has_unresolved_redirect((citation_url,)):
        return None, "短縮URLの転送先を店舗根拠として確認できない"
    cache_value = _page_candidate_cache_value(mention, candidate, citation_url)
    cached = _get_cache(db, PAGE_EVIDENCE_CACHE_KIND, cache_value)
    if cached is not None:
        try:
            cached_proof = CachedPageCandidateProof.model_validate_json(cached.payload)
        except ValidationError as exc:
            _record_failure(
                db,
                message_id,
                "page_evidence_cache",
                ExtractionError(
                    f"url={citation_url}; invalid cached payload: {exc}"
                ),
            )
            _delete_cache(db, PAGE_EVIDENCE_CACHE_KIND, cache_value, cached)
        else:
            final_identity = _source_evidence_url_identity(cached_proof.final_url)
            excluded_identities = frozenset(
                _source_evidence_url_identity(url)
                for url in excluded_evidence_urls
            )
            if (
                _citation_url_identity(cached_proof.final_url)
                == _citation_url_identity(citation_url)
                and final_identity not in excluded_identities
                and _page_verified_candidate_is_valid(
                    mention,
                    cached_proof.candidate,
                )
            ):
                return cached_proof.candidate, None
            _delete_cache(db, PAGE_EVIDENCE_CACHE_KIND, cache_value, cached)

    try:
        document = await fetch_html_document(citation_url)
    except (ExtractionError, httpx.HTTPError) as exc:
        error = ExtractionError(
            f"url={citation_url}; {type(exc).__name__}: {exc}"
        )
        _record_failure(db, message_id, "page_evidence", error)
        return None, "候補ページをサーバーから取得できない"

    if _citation_url_identity(document.final_url) != _citation_url_identity(
        citation_url
    ):
        return None, "候補ページの転送先が検索で引用された店舗URLと一致しない"
    excluded_identities = frozenset(
        _source_evidence_url_identity(url) for url in excluded_evidence_urls
    )
    if _source_evidence_url_identity(document.final_url) in excluded_identities:
        return None, "候補ページが元投稿の出典へ戻るため独立根拠にならない"

    page_evidence = extract_restaurant_page_evidence(
        document.html,
        document.final_url,
    )
    proof = prove_page_candidate(candidate, page_evidence)
    if proof is None:
        return None, "同一ページ上で店舗名と完全な住所を確認できない"
    canonical_area = _effective_candidate_area(mention, proof.candidate)
    if canonical_area is None:
        return None, "候補ページの住所から管理可能な地域を一意に特定できない"
    verified_identity = proof.candidate.model_copy(
        update={
            "area": canonical_area,
            "evidence_url": document.final_url,
        }
    )
    verified = _pipeline_candidate(
        verified_identity,
        CandidateProvenance.WEB_SEARCH,
        is_verified=True,
        verification_reason=(
            f"policy={TOP_WEB_MATCHING_POLICY}; source_verified=true; "
            "matching_score="
            f"{name_similarity(_name_with_branch(mention.shop_name, mention.branch_name), candidate.name):.3f}; "
            f"proof={proof.method}; page_sha256={proof.page_sha256}"
        ),
    )
    if not _page_verified_candidate_is_valid(mention, verified):
        return None, "候補ページの住所または支店が投稿の地域情報と一致しない"
    cached_proof = CachedPageCandidateProof(
        final_url=document.final_url,
        method=proof.method,
        page_sha256=proof.page_sha256,
        candidate=verified,
    )
    _put_cache(
        db,
        PAGE_EVIDENCE_CACHE_KIND,
        cache_value,
        cached_proof.model_dump_json(),
    )
    return verified, None


def _candidate_verification_urls(
    mention: ExtractedMention,
    candidates: tuple[PipelineCandidate, ...],
) -> tuple[str, ...]:
    urls: list[str] = []
    for candidate in candidates:
        if not _matches_mention_name_and_area(mention, candidate):
            continue
        url = candidate.canonical_url or candidate.evidence_url
        if url and url not in urls:
            urls.append(url)
        if len(urls) == 2:
            break
    return tuple(urls)


def _shop_name_for_storage(
    mention: ExtractedMention,
    candidate: PipelineCandidate,
) -> str:
    if not mention.branch_name:
        return candidate.name
    branch = mention.branch_name.strip()
    for value in (candidate.name, mention.shop_name):
        stripped = value.strip()
        if stripped.endswith(branch):
            base = stripped[: -len(branch)].rstrip(" 　-・()（）")
            if base:
                return base
    return mention.shop_name


def _new_shop_from_candidate(
    mention: ExtractedMention,
    candidate: PipelineCandidate,
) -> Shop:
    shop_name = _shop_name_for_storage(mention, candidate)
    if not candidate.is_verified:
        return Shop(
            shop_name=shop_name,
            branch_name=mention.branch_name,
            area=canonicalize_area(mention.area),
            category=canonicalize_category(mention.category) or mention.category,
            is_visited=False,
        )
    canonical_url = candidate.canonical_url
    candidate_identities = _candidate_external_identities(candidate)
    if external_identities_conflict(candidate_identities):
        raise RuntimeError("Candidate external identities conflict within one service")
    external_source, external_id = preferred_external_identity(
        external_source=candidate.external_source,
        external_id=candidate.external_id,
        urls=(candidate.canonical_url, candidate.evidence_url),
    )
    mention_area = canonicalize_area(mention.area)
    candidate_area = canonicalize_area(candidate.area)
    if (
        mention_area is not None
        and candidate.area
        and candidate.area.strip()
        and not _candidate_matches_area(mention_area, candidate)
    ):
        raise RuntimeError(
            f"Verified candidate area conflicts with the mention: "
            f"mention_area={mention.area}, candidate_area={candidate.area}"
        )
    if is_known_area(mention_area):
        area = mention_area
    elif is_known_area(candidate_area):
        area = candidate_area
    else:
        area = None
    return Shop(
        shop_name=shop_name,
        branch_name=mention.branch_name,
        area=area,
        category=(
            canonicalize_category(candidate.category or mention.category)
            or candidate.category or mention.category
        ),
        address=candidate.address,
        phone=normalize_phone(candidate.phone),
        canonical_url=canonical_url,
        external_source=external_source,
        external_id=external_id,
        is_visited=False,
    )


@dataclass(frozen=True)
class _EventOriginResolution:
    extracted: ExtractedMention | None = None
    candidates: tuple[PipelineCandidate, ...] = ()
    shop_id: int | None = None
    shop_version: int | None = None
    new_candidate: PipelineCandidate | None = None
    reason: str | None = None


async def _prepare_event_origin(
    db: Session, message_id: str, event: ExtractedMention, content: str,
    *, assets: tuple[SourceAssetInput, ...] = (),
    input_assessment: EvidenceAssessment = EvidenceAssessment(),
) -> _EventOriginResolution:
    origin = event.event_origin
    if origin is None or not origin.is_unique or input_assessment.requires_review:
        return _EventOriginResolution(reason="出店元の常設店舗・支店を一意に確認できません。")
    same_name_shops = [
        shop for shop in db.query(Shop).all()
        if normalize_name(shop.shop_name) == normalize_name(origin.shop_name)
    ]
    if not origin.branch_name and (
        len(same_name_shops) > 1 or any(shop.branch_name for shop in same_name_shops)
    ):
        return _EventOriginResolution(reason="同名の支店があり、催事の出店元支店を特定できません。")
    permitted_urls = {*extract_http_urls(content), *(asset.url for asset in assets)}
    permitted_urls.update(shop.canonical_url for shop in same_name_shops if shop.canonical_url)
    source_texts: dict[str, str] = {}
    for url in (origin.relation_source_url, origin.permanent_source_url):
        if not url or url in source_texts:
            continue
        try:
            ExtractedMention.validate_source_url(url)
            permitted = url in permitted_urls
            if not permitted:
                return _EventOriginResolution(reason="出店元の出典URLを元投稿・既存店舗情報から確認できません。")
            document = await fetch_html_document(url)
        except (ExtractionError, httpx.HTTPError, ValueError):
            return _EventOriginResolution(reason="出店元の根拠ページを取得できません。")
        if _citation_url_identity(document.final_url) != _citation_url_identity(url):
            return _EventOriginResolution(reason="出店元の根拠ページが別URLへ転送されました。")
        page = extract_restaurant_page_evidence(document.html, document.final_url)
        source_texts[url] = "\n".join(page.text_blocks)
    if not event_origin_quotes_match(
        name=origin.shop_name, branch=origin.branch_name, area=origin.area,
        relation_evidence=origin.relation_evidence, permanent_evidence=origin.permanent_evidence,
        relation_text=source_texts.get(origin.relation_source_url or "", content),
        permanent_text=source_texts.get(origin.permanent_source_url or "", content),
    ):
        return _EventOriginResolution(reason="催事と出店元の関係、常設店舗・支店の引用根拠が一致しません。")
    extracted = ExtractedMention(
        shop_name=origin.shop_name, branch_name=origin.branch_name, area=origin.area,
        category=event.category, subject_kind="restaurant", identity_evidence="explicit",
        name_evidence=origin.permanent_evidence, branch_evidence=origin.permanent_evidence,
        source_url=origin.permanent_source_url, needs_review=False,
        confidence_reason=(
            f"催事の出店元常設店舗を確認。関係: {origin.relation_evidence} / "
            f"常設店舗: {origin.permanent_evidence} / "
            f"出典: {origin.relation_source_url or '元投稿'} / {origin.permanent_source_url or '元投稿'}"
        )[:2_000],
    )
    existing, blocking = _find_existing_shop_from_mention(db, extracted, None)
    if existing is not None:
        return _EventOriginResolution(extracted, shop_id=existing.id, shop_version=existing.version)
    if blocking:
        return _EventOriginResolution(extracted, reason=blocking)
    candidates: list[PipelineCandidate] = []
    if origin.permanent_source_url:
        candidates.extend(await _structured_candidates(db, message_id, origin.permanent_source_url))
    if not candidates:
        batch = await _web_candidates(db, message_id, extracted)
        candidates.extend(batch.candidates)
        for url in _candidate_verification_urls(extracted, batch.candidates):
            candidates.extend(await _structured_candidates(db, message_id, url))
    candidates = _dedupe_candidates(candidates)
    existing, blocking = _find_existing_shop(db, extracted, candidates)
    if existing is not None:
        return _EventOriginResolution(extracted, tuple(candidates), existing.id, existing.version)
    candidate = None if blocking else _candidate_for_new_shop(extracted, candidates)
    if candidate is None and not blocking:
        candidate = _top_web_candidate_for_new_shop(extracted, candidates)
        if candidate is not None:
            candidate, blocking = await _verify_web_candidate_page(
                db, message_id, extracted, candidate, candidates=candidates,
            )
    if candidate is None or not candidate.is_verified:
        return _EventOriginResolution(extracted, tuple(candidates), reason=blocking or "常設店舗を確定できる候補がありません。")
    return _EventOriginResolution(extracted, tuple(candidates), new_candidate=candidate)


def _event_origin_mention_row(
    db: Session, message: Message, event: ExtractedMention, result: _EventOriginResolution,
    *, occurrence: int, source_url: str | None, extraction_source: str,
    previous: ShopMention | None = None,
) -> ShopMention:
    chosen: Shop | None = None
    if result.extracted is not None and (result.shop_id is not None or result.new_candidate is not None):
        lock_new_shop_creation(db)
        if not result.extracted.branch_name:
            matches = [
                shop for shop in db.query(Shop).populate_existing().all()
                if normalize_name(shop.shop_name) == normalize_name(result.extracted.shop_name)
            ]
            if len(matches) > 1 or any(shop.branch_name for shop in matches):
                result = _EventOriginResolution(
                    extracted=result.extracted, candidates=result.candidates,
                    reason="保存前の再確認で出店元の支店が一意に定まりません。",
                )
    used_existing = result.shop_id is not None
    if result.shop_id is not None:
        chosen = db.query(Shop).filter(Shop.id == result.shop_id).with_for_update().populate_existing().first()
        if chosen is None or chosen.version != result.shop_version:
            chosen = None
        elif db.query(ShopMention).filter(
            ShopMention.shop_id == chosen.id, ShopMention.review_status == ReviewStatus.APPROVED.value,
        ).with_for_update().populate_existing().first() is None:
            chosen = None
    elif result.new_candidate is not None and result.extracted is not None:
        candidate, collision, _blocked = _guard_new_shop_creation_or_link(
            db, result.extracted, result.new_candidate, None,
        )
        if collision is not None:
            chosen = collision
            used_existing = True
        elif candidate is not None:
            chosen = _new_shop_from_candidate(result.extracted, candidate)
            db.add(chosen)
            db.flush()
    excluded = chosen is None
    extracted = (result.extracted or event) if chosen is not None else event
    metadata_status, metadata_difference = _metadata_review_state(
        chosen.area if chosen is not None else None,
        chosen.category if chosen is not None else None,
    )
    assessment = event_exclusion_assessment(result.reason) if excluded else EvidenceAssessment()
    values = dict(
        message=message, shop=chosen, occurrence_index=occurrence,
        extracted_name=extracted.shop_name, extracted_branch_name=extracted.branch_name,
        extracted_area=extracted.area, extracted_category=extracted.category,
        source_url=source_url, extraction_source=extraction_source,
        resolution_status=ResolutionStatus.INVALID.value if excluded else ResolutionStatus.RESOLVED.value,
        review_status=ReviewStatus.REJECTED.value if excluded else ReviewStatus.APPROVED.value,
        metadata_review_status=MetadataReviewStatus.DEFERRED.value if excluded else metadata_status.value,
        metadata_difference_type=None if excluded else metadata_difference,
        difference_type=EVENT_EXCLUDED if excluded else None,
        extraction_error=evidence_review_error(assessment),
        confidence_reason=" / ".join((extracted.confidence_reason, *assessment.reasons))[:2_000],
        resolution_method=None if excluded else ResolutionMethod.AUTOMATIC.value,
        resolution_basis=None if excluded else (
            ResolutionBasis.EXISTING_SHOP.value if used_existing else ResolutionBasis.VERIFIED_CANDIDATE.value
        ),
        reviewed_at=utc_now(),
        metadata_reviewed_at=utc_now() if not excluded and metadata_status == MetadataReviewStatus.APPROVED else None,
    )
    mention = previous or ShopMention()
    for key, value in values.items():
        setattr(mention, key, value)
    if previous is not None:
        mention.version += 1
    db.add(mention)
    db.flush()
    if result.candidates and result.extracted is not None and not mention.candidates:
        _store_candidates(db, mention, result.extracted, list(result.candidates), result.new_candidate if chosen is not None else None)
    return mention


def _candidates_from_image(
    image_result: ImageAnalysisResult,
    evidence_url: str,
) -> list[PipelineCandidate]:
    clues = image_result.clues
    if not clues.usable or not clues.visible_shop_names:
        return []

    names: list[str] = []
    seen_names: set[str] = set()
    for name in clues.visible_shop_names:
        normalized = normalize_name(name)
        if normalized and normalized not in seen_names:
            seen_names.add(normalized)
            names.append(name)

    normalized_addresses = {
        normalized: value
        for value in clues.address_clues
        if (normalized := normalize_address(value))
    }
    normalized_phones = {
        normalized: value
        for value in clues.phone_clues
        if (normalized := normalize_phone(value))
    }
    can_associate_clues = len(names) == 1
    address = (
        next(iter(normalized_addresses.values()))
        if can_associate_clues and len(normalized_addresses) == 1
        else None
    )
    phone = (
        next(iter(normalized_phones.values()))
        if can_associate_clues and len(normalized_phones) == 1
        else None
    )
    return [
        PipelineCandidate(
            name=name,
            address=address,
            phone=phone,
            evidence_url=evidence_url,
            provenance=CandidateProvenance.IMAGE,
            is_verified=False,
            verification_reason="画像解析結果は補助根拠であり店舗同定には未検証",
        )
        for name in names[:5]
    ]


def _single_image_candidate(
    candidates: list[PipelineCandidate],
) -> PipelineCandidate | None:
    image_candidates = [
        candidate
        for candidate in candidates
        if candidate.provenance == CandidateProvenance.IMAGE
    ]
    return image_candidates[0] if len(image_candidates) == 1 else None


def _image_matches_mention(
    mention: ExtractedMention,
    candidate: PipelineCandidate,
) -> bool:
    mention_name = _name_with_branch(mention.shop_name, mention.branch_name)
    return bool(
        not branches_conflict(mention_name, candidate.name)
        and name_similarity(mention_name, candidate.name) >= 0.8
    )


def _verified_image_candidate(
    candidate: PipelineCandidate,
    reason: str,
) -> PipelineCandidate:
    return candidate.model_copy(
        update={
            "is_verified": True,
            "verification_reason": reason,
        }
    )


def _find_existing_shop_from_image(
    db: Session,
    mention: ExtractedMention | None,
    candidate: PipelineCandidate,
) -> tuple[Shop | None, PipelineCandidate | None, str | None]:
    if mention is not None and not _image_matches_mention(mention, candidate):
        return None, None, "画像内の店名が投稿から抽出した店舗と一致しない"

    approved_shops = (
        db.query(Shop)
        .filter(Shop.mentions.any(ShopMention.review_status == ReviewStatus.APPROVED.value))
        .all()
    )
    matches: dict[int, tuple[Shop, str]] = {}
    conflicting_matches = 0
    for shop in approved_shops:
        if external_identities_conflict(_shop_external_identities(shop)):
            conflicting_matches += 1
            continue
        shop_identity = _shop_identity(shop)
        outcome = evaluate_identity(shop_identity, candidate)
        mention_area = canonicalize_area(mention.area) if mention is not None else None
        shop_area = canonicalize_area(shop.area)
        exact_name_and_independent_area = bool(
            mention is not None
            and mention_area is not None
            and shop_area is not None
            and not area_is_municipality(mention_area)
            and normalize_name(shop_identity.name) == normalize_name(candidate.name)
            and shop_area == mention_area
            and not branches_conflict(shop_identity.name, candidate.name)
        )
        if outcome.is_strong_match and not outcome.conflicting_fields:
            matches[shop.id] = (shop, f"画像の{outcome.reason}")
        elif outcome.is_strong_match:
            conflicting_matches += 1
        elif exact_name_and_independent_area and not outcome.conflicting_fields:
            matches[shop.id] = (
                shop,
                "画像の店名・支店名と投稿由来のエリアが一意に一致",
            )
        elif exact_name_and_independent_area:
            conflicting_matches += 1

    if len(matches) == 1:
        shop, reason = next(iter(matches.values()))
        return shop, _verified_image_candidate(candidate, reason), reason
    if len(matches) > 1:
        return None, None, "画像の強い一致条件を満たす既存店舗が複数ある"
    if conflicting_matches:
        return None, None, "画像と既存店舗の電話・住所に矛盾がある"
    return None, None, None


def _image_verification_urls(
    image_candidate: PipelineCandidate,
    candidates: list[PipelineCandidate],
) -> tuple[str, ...]:
    urls: list[str] = []
    image_phone = normalize_phone(image_candidate.phone)
    image_address = normalize_address(image_candidate.address)
    for candidate in candidates:
        if candidate.provenance != CandidateProvenance.WEB_SEARCH:
            continue
        if branches_conflict(image_candidate.name, candidate.name):
            continue
        if name_similarity(image_candidate.name, candidate.name) < 0.8:
            continue
        candidate_phone = normalize_phone(candidate.phone)
        candidate_address = normalize_address(candidate.address)
        if image_phone and candidate_phone and image_phone != candidate_phone:
            continue
        if image_address and candidate_address and image_address != candidate_address:
            continue
        url = candidate.canonical_url or candidate.evidence_url
        if url and url not in urls:
            urls.append(url)
        if len(urls) == 2:
            break
    return tuple(urls)


def _structured_candidate_from_image(
    image_candidate: PipelineCandidate,
    candidates: list[PipelineCandidate],
) -> tuple[PipelineCandidate | None, str | None]:
    matches: list[tuple[PipelineCandidate, str]] = []
    conflicting_matches = 0
    for candidate in candidates:
        if (
            candidate.provenance != CandidateProvenance.STRUCTURED_DATA
            or not candidate.is_verified
        ):
            continue
        outcome = evaluate_identity(candidate, image_candidate)
        if outcome.is_strong_match and not outcome.conflicting_fields:
            matches.append((candidate, f"画像と構造化データの{outcome.reason}"))
        elif outcome.is_strong_match:
            conflicting_matches += 1
    if len(matches) == 1:
        candidate, reason = matches[0]
        return candidate.model_copy(update={"verification_reason": reason}), reason
    if len(matches) > 1:
        return None, "画像と強い一致条件を満たす構造化候補が複数ある"
    if conflicting_matches:
        return None, "画像と構造化候補の電話・住所に矛盾がある"
    return None, None


def _store_candidates(
    db: Session,
    mention_row: ShopMention,
    extracted: ExtractedMention,
    candidates: list[PipelineCandidate],
    automatically_selected: PipelineCandidate | None,
) -> None:
    ranked_candidates = list(candidates)
    if automatically_selected is not None:
        ranked_candidates = [automatically_selected] + [
            candidate
            for candidate in ranked_candidates
            if candidate != automatically_selected
        ]
    for rank, candidate in enumerate(ranked_candidates[:5], start=1):
        extracted_name = _name_with_branch(
            extracted.shop_name,
            extracted.branch_name,
        )
        similarity = name_similarity(extracted_name, candidate.name)
        populated = [
            field
            for field, value in (
                ("external_id", candidate.external_id),
                ("phone", candidate.phone),
                ("address", candidate.address),
            )
            if value
        ]
        conflicts = (
            ["branch"]
            if branches_conflict(extracted_name, candidate.name)
            else []
        )
        db.add(
            ResolutionCandidate(
                mention=mention_row,
                rank=rank,
                name=candidate.name,
                area=candidate.area,
                category=candidate.category,
                address=candidate.address,
                phone=normalize_phone(candidate.phone),
                canonical_url=candidate.canonical_url,
                external_source=candidate.external_source,
                external_id=candidate.external_id,
                evidence_url=candidate.evidence_url,
                provenance=candidate.provenance.value,
                is_verified=candidate.is_verified,
                verification_reason=candidate.verification_reason,
                matched_fields=",".join(populated) or None,
                conflicting_fields=",".join(conflicts) or None,
                name_similarity_milli=round(similarity * 1000),
                is_strong_match=(
                    candidate == automatically_selected and candidate.is_verified
                ),
            )
        )


def _is_discord_image(asset: SourceAssetInput) -> bool:
    host = urlparse(asset.url).netloc.lower().split(":", 1)[0]
    return asset.kind == AssetKind.IMAGE.value and host in _DISCORD_IMAGE_HOSTS


def _metadata_review_state(
    area: str | None,
    category: str | None,
) -> tuple[MetadataReviewStatus, str | None]:
    if not area:
        return MetadataReviewStatus.PENDING, "missing_area"
    if not is_known_area(area):
        return MetadataReviewStatus.PENDING, "unknown_area"
    if not category:
        return MetadataReviewStatus.PENDING, "missing_category"
    if not is_known_category(category):
        return MetadataReviewStatus.PENDING, "unknown_category"
    return MetadataReviewStatus.APPROVED, None


async def _process_image_only_message(
    db: Session,
    message: Message,
    envelope: MessageEnvelope,
    image_urls: tuple[str, ...],
    source_url: str | None,
    unresolved_reason: str,
    *,
    allow_automatic_resolution: bool = True,
    input_assessment: EvidenceAssessment = EvidenceAssessment(),
    commit: bool = True,
) -> MessageProcessResult:
    candidates: list[PipelineCandidate] = []
    selected_candidate: PipelineCandidate | None = None
    chosen_shop: Shop | None = None
    resolution_basis: ResolutionBasis | None = None
    reason_parts = [unresolved_reason, *input_assessment.reasons]

    try:
        image_result = await analyze_restaurant_images(None, image_urls[:1])
    except (ExtractionError, ValidationError) as exc:
        _record_failure(db, envelope.message_id, "image_analysis", exc)
        raise
    _record_metrics(db, envelope.message_id, "image_analysis", image_result.metrics)
    reason_parts.append(f"画像補助: {image_result.clues.reason}")
    image_assessment = image_evidence_assessment(
        image_result.clues.subject_kind,
        content=envelope.content,
        unresolved_reason=unresolved_reason,
        input_assessment=input_assessment,
    )
    reason_parts.extend(reason for reason in image_assessment.reasons if reason not in reason_parts)
    candidates.extend(_candidates_from_image(image_result, image_urls[0]))
    candidates = _dedupe_candidates(candidates)
    image_candidate = _single_image_candidate(candidates)
    if image_assessment.is_event_excluded:
        event = ExtractedMention(
            shop_name=image_candidate.name if image_candidate is not None else UNRESOLVED_SHOP_LABEL,
            needs_review=True, confidence_reason=unresolved_reason,
            subject_kind="event", event_origin=image_result.clues.event_origin,
        )
        origin_result = await _prepare_event_origin(
            db, envelope.message_id, event, envelope.content,
            assets=envelope.assets, input_assessment=input_assessment,
        )
        event_row = _event_origin_mention_row(
            db, message, event, origin_result, occurrence=0,
            source_url=source_url, extraction_source="responses_vision",
        )
        message.processing_status = ProcessingStatus.SUCCEEDED.value
        message.processed_at = utc_now()
        db.commit() if commit else db.flush()
        return MessageProcessResult(
            envelope.message_id, (event_row.shop_id,) if event_row.shop_id is not None else (), (), False,
        )
    image_mention: ExtractedMention | None = None
    image_blocking_reason: str | None = None
    automatic_image_allowed = (
        len(image_urls) == 1 and allow_automatic_resolution and not image_assessment.requires_review
    )
    if len(image_urls) != 1 or not allow_automatic_resolution:
        reason_parts.append("複数画像の対応関係を検証できないため自動確定しない")

    if image_candidate is not None:
        image_mention = ExtractedMention(
            shop_name=image_candidate.name,
            branch_name=extract_branch_token(image_candidate.name),
            area=None,
            category=None,
            needs_review=True,
            confidence_reason=image_result.clues.reason,
            subject_kind=image_result.clues.subject_kind,
            operating_status=image_result.clues.operating_status,
            operating_status_evidence=image_result.clues.operating_status_evidence,
        )
        status_note = operating_status_note(image_mention)
        if status_note:
            reason_parts.append(status_note)
    if automatic_image_allowed and image_candidate is not None:
        (
            existing_shop,
            verified_image,
            existing_reason,
        ) = _find_existing_shop_from_image(db, None, image_candidate)
        if existing_shop is not None and verified_image is not None:
            chosen_shop = existing_shop
            resolution_basis = ResolutionBasis.IMAGE_EVIDENCE
            selected_candidate = verified_image
            candidates.append(verified_image)
            candidates = _dedupe_candidates(candidates)
            if existing_reason:
                reason_parts.append(existing_reason)
        elif existing_reason:
            image_blocking_reason = existing_reason
            reason_parts.append(existing_reason)

    if (
        chosen_shop is None
        and automatic_image_allowed
        and image_blocking_reason is None
        and image_candidate is not None
        and image_mention is not None
        and (image_candidate.phone or image_candidate.address)
    ):
        web_batch = await _web_candidates(db, envelope.message_id, image_mention)
        candidates.extend(web_batch.candidates)
        candidates = _dedupe_candidates(candidates)
        if web_batch.unresolved_reason:
            reason_parts.append(f"Web検索未解決: {web_batch.unresolved_reason}")
        for verification_url in _image_verification_urls(
            image_candidate,
            candidates,
        ):
            candidates.extend(
                await _structured_candidates(
                    db,
                    envelope.message_id,
                    verification_url,
                )
            )
        candidates = _dedupe_candidates(candidates)
        new_candidate, candidate_reason = _structured_candidate_from_image(
            image_candidate,
            candidates,
        )
        collision_candidate = new_candidate
        new_candidate, collision_shop, blocking_reason = _guard_new_shop_creation_or_link(
            db,
            image_mention,
            new_candidate,
            candidate_reason if new_candidate is None else None,
        )
        if collision_shop is not None:
            chosen_shop = collision_shop
            selected_candidate = collision_candidate
            resolution_basis = ResolutionBasis.VERIFIED_COLLISION
            if blocking_reason:
                reason_parts.append(blocking_reason)
        elif new_candidate is not None:
            chosen_shop = _new_shop_from_candidate(image_mention, new_candidate)
            db.add(chosen_shop)
            db.flush()
            selected_candidate = new_candidate
            resolution_basis = ResolutionBasis.IMAGE_EVIDENCE
            reason_parts.append(
                new_candidate.verification_reason or "画像と構造化データで確定"
            )
        elif blocking_reason:
            reason_parts.append(blocking_reason)

    automatically_resolved = chosen_shop is not None
    extracted_name = (
        image_candidate.name if image_candidate is not None else UNRESOLVED_SHOP_LABEL
    )
    final_area = chosen_shop.area if chosen_shop is not None else None
    final_category = chosen_shop.category if chosen_shop is not None else None
    metadata_status, metadata_difference = _metadata_review_state(
        final_area,
        final_category,
    )
    if automatically_resolved:
        difference_type = None
    elif candidates:
        difference_type = "image_unverified"
    else:
        difference_type = "extraction_not_found"
    mention_row = ShopMention(
        message_id=envelope.message_id,
        shop_id=chosen_shop.id if chosen_shop is not None else None,
        occurrence_index=0,
        extracted_name=extracted_name,
        extracted_branch_name=(
            image_mention.branch_name if image_mention is not None else None
        ),
        source_url=source_url,
        resolution_status=(
            ResolutionStatus.RESOLVED.value
            if automatically_resolved
            else (
                ResolutionStatus.AMBIGUOUS.value
                if candidates
                else ResolutionStatus.NOT_FOUND.value
            )
        ),
        review_status=(
            ReviewStatus.APPROVED.value
            if automatically_resolved
            else ReviewStatus.PENDING.value
        ),
        metadata_review_status=metadata_status.value,
        metadata_difference_type=metadata_difference,
        resolution_method=(
            ResolutionMethod.AUTOMATIC.value if automatically_resolved else None
        ),
        resolution_basis=(resolution_basis.value if resolution_basis is not None else None),
        difference_type=difference_type,
        extraction_source="responses_vision",
        extraction_error=(
            evidence_review_error(image_assessment)
            or (None if automatically_resolved else unresolved_reason)
        ),
        confidence_reason=" / ".join(reason_parts),
        reviewed_at=utc_now() if automatically_resolved else None,
        metadata_reviewed_at=(
            utc_now()
            if metadata_status == MetadataReviewStatus.APPROVED
            else None
        ),
    )
    db.add(mention_row)
    db.flush()
    if candidates:
        comparison_mention = image_mention or ExtractedMention(
            shop_name=UNRESOLVED_SHOP_LABEL,
            branch_name=None,
            area=None,
            category=None,
            needs_review=True,
            confidence_reason=unresolved_reason,
        )
        _store_candidates(
            db,
            mention_row,
            comparison_mention,
            candidates,
            selected_candidate,
        )
    message.processing_status = ProcessingStatus.SUCCEEDED.value
    message.processed_at = utc_now()
    if commit:
        db.commit()
    else:
        db.flush()
    return MessageProcessResult(
        envelope.message_id,
        (chosen_shop.id,) if chosen_shop is not None else (),
        () if automatically_resolved else (mention_row.id,),
        False,
    )


async def _process_message_transaction(
    db: Session,
    envelope: MessageEnvelope,
    *,
    commit: bool = True,
    allow_image_fallback: bool = True,
    allow_source_discovery: bool = False,
    fallback_mentions: tuple[ExtractedMention, ...] = (),
) -> MessageProcessResult:
    existing = db.query(Message).filter(Message.message_id == envelope.message_id).first()
    if existing and existing.processing_status in {
        ProcessingStatus.SUCCEEDED.value,
        ProcessingStatus.IGNORED.value,
    }:
        shop_ids = tuple(
            mention.shop_id for mention in existing.mentions if mention.shop_id is not None
        )
        pending = tuple(
            mention.id
            for mention in existing.mentions
            if mention.review_status == ReviewStatus.PENDING.value
        )
        return MessageProcessResult(
            envelope.message_id,
            shop_ids,
            pending,
            existing.processing_status == ProcessingStatus.IGNORED.value,
        )

    message = existing or Message(message_id=envelope.message_id)
    message.channel_id = envelope.channel_id
    message.content = envelope.content
    message.source_created_at = envelope.created_at.astimezone(timezone.utc)
    message.processing_status = ProcessingStatus.PROCESSING.value
    message.fetch_error = None
    if not existing:
        db.add(message)
    else:
        for mention in list(message.mentions):
            db.delete(mention)
    db.flush()
    _replace_source_assets(db, envelope.message_id, envelope.assets)
    input_assessment = _envelope_input_assessment(envelope)

    try:
        source_reuse_match = _find_source_reuse_match(db, envelope)
        if source_reuse_match is not None:
            return _reuse_source_match(
                db,
                message,
                envelope,
                source_reuse_match,
                commit=commit,
            )
        try:
            extraction = await extract_restaurant_message(envelope.content)
        except (ExtractionError, ValidationError) as exc:
            _record_failure(db, envelope.message_id, "message_extraction", exc)
            raise
        _record_metrics(db, envelope.message_id, "message_extraction", extraction.metrics)
        fresh_mentions = tuple(extraction.message.mentions)
        using_fallback_mentions = not fresh_mentions and bool(fallback_mentions)
        discovery_evidence: SourceDiscoveryEvidence | None = None
        discovery_message: ExtractedMessage | None = None
        if allow_source_discovery and not fresh_mentions and not fallback_mentions:
            discovery_evidence = _source_discovery_evidence(envelope)
            if discovery_evidence is not None:
                discovery_message = await _source_discovery_message(
                    db,
                    envelope.message_id,
                    discovery_evidence,
                )
        discovered_mentions = (
            tuple(discovery_message.mentions)
            if discovery_message is not None
            else ()
        )
        input_assessment = _envelope_input_assessment(
            envelope,
            source_input_truncated=bool(discovery_evidence and discovery_evidence.input_truncated),
        )
        using_source_discovery = bool(discovered_mentions)
        source_discovery_attempted = discovery_message is not None
        using_untrusted_mentions = (
            using_fallback_mentions or using_source_discovery
        )
        fallback_context = (
            extraction.message.unresolved_reason
            or extraction.message.ignore_reason
            or "新規抽出で店舗名を特定できませんでした。"
        )
        fallback_effective_mentions = (
            tuple(
                mention.model_copy(
                    update={
                        "needs_review": True,
                        "confidence_reason": _fallback_confidence_reason(
                            mention.confidence_reason,
                            fallback_context,
                        ),
                    }
                )
                for mention in require_extraction_evidence(
                    ExtractedMessage(is_restaurant_message=True, mentions=list(fallback_mentions))
                ).mentions
            )
            if using_fallback_mentions
            else ()
        )
        discovery_effective_mentions = (
            tuple(
                mention.model_copy(update={"needs_review": True})
                for mention in discovered_mentions
            )
            if using_source_discovery
            else ()
        )
        effective_mentions = (
            fresh_mentions
            or fallback_effective_mentions
            or discovery_effective_mentions
        )
        is_restaurant_message = extraction.message.is_restaurant_message or bool(
            discovery_message and discovery_message.is_restaurant_message
        )
        event_context = image_evidence_assessment(
            "unknown", content=envelope.content,
            unresolved_reason=extraction.message.unresolved_reason or extraction.message.ignore_reason or "",
        ).is_event_excluded
        if not is_restaurant_message and not effective_mentions and not input_assessment.requires_review and not event_context:
            message.is_target = False
            message.processing_status = ProcessingStatus.IGNORED.value
            message.processed_at = utc_now()
            if commit:
                db.commit()
            else:
                db.flush()
            return MessageProcessResult(envelope.message_id, (), (), True)

        message.is_target = True
        image_urls = (
            tuple(
                dict.fromkeys(
                    asset.url for asset in envelope.assets if _is_discord_image(asset)
                )
            )
            if allow_image_fallback and not source_discovery_attempted
            else ()
        )
        (
            source_url,
            canonical_hint,
            canonical_assets_ambiguous,
        ) = _source_urls_from_assets(envelope.assets)
        if not effective_mentions:
            unresolved_reason = (
                discovery_message.unresolved_reason
                if discovery_message is not None
                else None
            ) or (
                extraction.message.unresolved_reason
                or "飲食店への言及ですが、投稿から店舗名を特定できませんでした。"
            )
            if input_assessment.requires_review:
                unresolved_reason = " / ".join((unresolved_reason, *input_assessment.reasons))
            if canonical_assets_ambiguous:
                unresolved_reason = (
                    f"{unresolved_reason} / "
                    "複数の店舗ID付きURLと投稿内店舗の対応を一意に確認できない"
                )
            if image_urls:
                return await _process_image_only_message(
                    db,
                    message,
                    envelope,
                    image_urls,
                    source_url,
                    unresolved_reason,
                    allow_automatic_resolution=not canonical_assets_ambiguous,
                    input_assessment=input_assessment,
                    commit=commit,
                )
            if event_context:
                event = ExtractedMention(
                    shop_name=UNRESOLVED_SHOP_LABEL, needs_review=True,
                    subject_kind="event", confidence_reason=unresolved_reason,
                )
                _event_origin_mention_row(
                    db, message, event, _EventOriginResolution(), occurrence=0,
                    source_url=source_url, extraction_source="responses_structured",
                )
                message.processing_status = ProcessingStatus.SUCCEEDED.value
                message.processed_at = utc_now()
                db.commit() if commit else db.flush()
                return MessageProcessResult(envelope.message_id, (), (), False)
            mention_row = ShopMention(
                message_id=envelope.message_id,
                shop_id=None,
                occurrence_index=0,
                extracted_name=UNRESOLVED_SHOP_LABEL,
                source_url=source_url,
                resolution_status=ResolutionStatus.NOT_FOUND.value,
                review_status=ReviewStatus.PENDING.value,
                resolution_method=None,
                difference_type="extraction_not_found",
                extraction_source=(
                    "responses_source_discovery"
                    if source_discovery_attempted
                    else "responses_structured"
                ),
                extraction_error=evidence_review_error(input_assessment) or unresolved_reason,
                confidence_reason=unresolved_reason,
            )
            db.add(mention_row)
            db.flush()
            message.processing_status = ProcessingStatus.SUCCEEDED.value
            message.processed_at = utc_now()
            if commit:
                db.commit()
            else:
                db.flush()
            return MessageProcessResult(
                envelope.message_id,
                (),
                (mention_row.id,),
                False,
            )

        shop_ids: list[int] = []
        pending_ids: list[int] = []
        image_result: ImageAnalysisResult | None = None

        for occurrence, extracted in enumerate(effective_mentions):
            note = operating_status_note(extracted)
            if note:
                extracted = extracted.model_copy(
                    update={"confidence_reason": f"{extracted.confidence_reason} / {note}"}
                )
            mention_input_assessment = _envelope_input_assessment(
                envelope,
                source_input_truncated=bool(discovery_evidence and discovery_evidence.input_truncated),
            )
            safety_assessment = assess_mention_evidence(
                extracted,
                envelope.content,
                input_assessment=mention_input_assessment,
                source_grounded=using_source_discovery and extracted.source_url is not None,
            )
            if safety_assessment.is_event_excluded:
                origin_result = await _prepare_event_origin(
                    db, envelope.message_id, extracted, envelope.content,
                    assets=envelope.assets, input_assessment=mention_input_assessment,
                )
                event_row = _event_origin_mention_row(
                    db, message, extracted, origin_result, occurrence=occurrence,
                    source_url=extracted.source_url if using_source_discovery else source_url,
                    extraction_source="responses_source_discovery" if using_source_discovery else "responses_structured",
                )
                if event_row.shop_id is not None:
                    shop_ids.append(event_row.shop_id)
                continue
            if safety_assessment.requires_review:
                metadata_status, metadata_difference = _metadata_review_state(
                    extracted.area, extracted.category
                )
                mention_row = ShopMention(
                    message_id=envelope.message_id,
                    occurrence_index=occurrence,
                    shop_id=None,
                    extracted_name=extracted.shop_name,
                    extracted_branch_name=extracted.branch_name,
                    extracted_area=extracted.area,
                    extracted_category=extracted.category,
                    source_url=extracted.source_url if using_source_discovery else source_url,
                    resolution_status=ResolutionStatus.AMBIGUOUS.value,
                    review_status=ReviewStatus.PENDING.value,
                    metadata_review_status=metadata_status.value,
                    metadata_difference_type=metadata_difference,
                    difference_type="evidence_review",
                    extraction_source=(
                        "legacy_hint" if using_fallback_mentions
                        else "responses_source_discovery" if using_source_discovery
                        else "responses_structured"
                    ),
                    extraction_error=evidence_review_error(safety_assessment),
                    confidence_reason=" / ".join((extracted.confidence_reason, *safety_assessment.reasons))[:2_000],
                )
                db.add(mention_row)
                db.flush()
                pending_ids.append(mention_row.id)
                continue
            candidates: list[PipelineCandidate] = []
            automatic_evidence_candidate: PipelineCandidate | None = None
            mention_canonical_hint = (
                canonical_hint if len(effective_mentions) == 1 else None
            )
            automatic_resolution_allowed = bool(
                not canonical_assets_ambiguous
                and (
                    not using_source_discovery
                    or extracted.source_url is not None
                )
            )
            if using_untrusted_mentions or not automatic_resolution_allowed:
                existing_shop = None
                existing_reason = None
                creation_blocked_reason = None
            else:
                existing_shop, existing_reason = _find_existing_shop_from_mention(
                    db,
                    extracted,
                    mention_canonical_hint,
                )
                creation_blocked_reason = (
                    existing_reason if existing_shop is None else None
                )
            resolution_basis = (
                ResolutionBasis.EXISTING_SHOP if existing_shop is not None else None
            )
            new_candidate = (
                None
                if existing_shop or creation_blocked_reason
                else _candidate_for_new_shop(
                    extracted,
                    candidates,
                )
            )
            new_candidate, creation_blocked_reason = _guard_new_shop_creation(
                db,
                extracted,
                new_candidate,
                creation_blocked_reason,
            )
            search_unresolved_reason: str | None = None
            if (
                existing_shop is None
                and new_candidate is None
                and creation_blocked_reason is None
            ):
                structured_urls: list[str] = []
                if canonical_hint:
                    structured_urls.append(canonical_hint)
                for asset in envelope.assets:
                    if asset.kind in {AssetKind.LINK.value, AssetKind.EMBED.value}:
                        if asset.url not in structured_urls:
                            structured_urls.append(asset.url)
                for structured_url in structured_urls[:10]:
                    candidates.extend(
                        await _structured_candidates(
                            db,
                            envelope.message_id,
                            structured_url,
                        )
                    )
                candidates = _dedupe_candidates(candidates)
                structured_block_reason = _structured_candidate_block_reason(
                    extracted,
                    candidates,
                )
                if automatic_resolution_allowed and structured_block_reason:
                    creation_blocked_reason = structured_block_reason
                elif not using_source_discovery and automatic_resolution_allowed:
                    existing_shop, candidate_reason = _find_existing_shop(
                        db,
                        extracted,
                        candidates,
                    )
                    if existing_shop is not None:
                        resolution_basis = ResolutionBasis.EXISTING_SHOP
                    existing_reason = candidate_reason or existing_reason
                    creation_blocked_reason = (
                        candidate_reason if existing_shop is None else None
                    )
                    new_candidate = (
                        None
                        if existing_shop or creation_blocked_reason
                        else _candidate_for_new_shop(
                            extracted,
                            candidates,
                        )
                    )
                    collision_candidate = new_candidate
                    (
                        new_candidate,
                        collision_shop,
                        collision_reason,
                    ) = _guard_new_shop_creation_or_link(
                        db,
                        extracted,
                        new_candidate,
                        creation_blocked_reason,
                    )
                    if collision_shop is not None:
                        existing_shop = collision_shop
                        existing_reason = collision_reason
                        automatic_evidence_candidate = collision_candidate
                        resolution_basis = ResolutionBasis.VERIFIED_COLLISION
                        creation_blocked_reason = None
                    else:
                        creation_blocked_reason = collision_reason
            if (
                existing_shop is None
                and new_candidate is None
                and creation_blocked_reason is None
            ):
                web_batch = await _web_candidates(db, envelope.message_id, extracted)
                candidates.extend(web_batch.candidates)
                candidates = _dedupe_candidates(candidates)
                search_unresolved_reason = web_batch.unresolved_reason
                for verification_url in _candidate_verification_urls(
                    extracted,
                    web_batch.candidates,
                ):
                    candidates.extend(
                        await _structured_candidates(
                            db,
                            envelope.message_id,
                            verification_url,
                        )
                    )
                candidates = _dedupe_candidates(candidates)
                structured_block_reason = _structured_candidate_block_reason(
                    extracted,
                    candidates,
                )
                if automatic_resolution_allowed and structured_block_reason:
                    creation_blocked_reason = structured_block_reason
                elif not using_source_discovery and automatic_resolution_allowed:
                    existing_shop, candidate_reason = _find_existing_shop(
                        db,
                        extracted,
                        candidates,
                    )
                    if existing_shop is not None:
                        resolution_basis = ResolutionBasis.EXISTING_SHOP
                    existing_reason = candidate_reason or existing_reason
                    creation_blocked_reason = (
                        candidate_reason if existing_shop is None else None
                    )
                    new_candidate = (
                        None
                        if existing_shop or creation_blocked_reason
                        else _candidate_for_new_shop(
                            extracted,
                            candidates,
                        )
                    )
                    collision_candidate = new_candidate
                    (
                        new_candidate,
                        collision_shop,
                        collision_reason,
                    ) = _guard_new_shop_creation_or_link(
                        db,
                        extracted,
                        new_candidate,
                        creation_blocked_reason,
                    )
                    if collision_shop is not None:
                        existing_shop = collision_shop
                        existing_reason = collision_reason
                        automatic_evidence_candidate = collision_candidate
                        resolution_basis = ResolutionBasis.VERIFIED_COLLISION
                        creation_blocked_reason = None
                    else:
                        creation_blocked_reason = collision_reason
            deferred_page_block_reason: str | None = None
            if (
                existing_shop is None
                and new_candidate is None
                and creation_blocked_reason is None
                and automatic_resolution_allowed
            ):
                new_candidate = _top_web_candidate_for_new_shop(
                    extracted,
                    candidates,
                    require_precise_area_evidence=using_source_discovery,
                    excluded_evidence_urls=(
                        discovery_evidence.source_urls
                        if using_source_discovery
                        and discovery_evidence is not None
                        else ()
                    ),
                )
                if new_candidate is not None:
                    new_candidate, page_block_reason = await _verify_web_candidate_page(
                        db,
                        envelope.message_id,
                        extracted,
                        new_candidate,
                        candidates=candidates,
                        excluded_evidence_urls=(
                            discovery_evidence.source_urls
                            if using_source_discovery
                            and discovery_evidence is not None
                            else ()
                        ),
                    )
                    deferred_page_block_reason = page_block_reason
                if using_source_discovery and new_candidate is not None:
                    matched_shop, matched_reason = (
                        _find_existing_shop_after_candidate_confirmation(
                            db,
                            extracted,
                            new_candidate,
                        )
                    )
                    if matched_shop is not None:
                        existing_shop = matched_shop
                        existing_reason = matched_reason
                        automatic_evidence_candidate = new_candidate
                        resolution_basis = ResolutionBasis.VERIFIED_CANDIDATE
                    elif matched_reason is not None:
                        new_candidate = None
                        creation_blocked_reason = matched_reason
                if existing_shop is None and new_candidate is not None:
                    collision_candidate = new_candidate
                    (
                        new_candidate,
                        collision_shop,
                        collision_reason,
                    ) = _guard_new_shop_creation_or_link(
                        db,
                        extracted,
                        new_candidate,
                        creation_blocked_reason,
                    )
                    if collision_shop is not None:
                        existing_shop = collision_shop
                        existing_reason = collision_reason
                        automatic_evidence_candidate = collision_candidate
                        resolution_basis = ResolutionBasis.VERIFIED_COLLISION
                        creation_blocked_reason = None
                    elif collision_reason is not None:
                        creation_blocked_reason = collision_reason
            image_reason: str | None = None
            image_assessment = EvidenceAssessment()
            if (
                existing_shop is None
                and new_candidate is None
                and creation_blocked_reason is None
                and image_urls
                and len(effective_mentions) == 1
                and not using_untrusted_mentions
                and automatic_resolution_allowed
            ):
                automatic_image_allowed = len(image_urls) == 1
                if image_result is None:
                    try:
                        image_result = await analyze_restaurant_images(None, image_urls[:1])
                    except (ExtractionError, ValidationError) as exc:
                        _record_failure(db, envelope.message_id, "image_analysis", exc)
                        raise
                    _record_metrics(
                        db,
                        envelope.message_id,
                        "image_analysis",
                        image_result.metrics,
                    )
                image_assessment = image_evidence_assessment(image_result.clues.subject_kind)
                if image_assessment.is_event_excluded:
                    event = extracted.model_copy(update={
                        "subject_kind": "event", "event_origin": image_result.clues.event_origin,
                    })
                    origin_result = await _prepare_event_origin(
                        db, envelope.message_id, event, envelope.content,
                        assets=envelope.assets, input_assessment=mention_input_assessment,
                    )
                    event_row = _event_origin_mention_row(
                        db, message, event, origin_result, occurrence=occurrence,
                        source_url=source_url, extraction_source="responses_vision",
                    )
                    if event_row.shop_id is not None:
                        shop_ids.append(event_row.shop_id)
                    continue
                automatic_image_allowed = automatic_image_allowed and not image_assessment.requires_review
                image_reason_parts = [image_result.clues.reason, *image_assessment.reasons]
                if len(image_urls) != 1:
                    image_reason_parts.append(
                        "複数画像の対応関係を検証できないため自動確定しない"
                    )
                image_reason = " / ".join(image_reason_parts)
                candidates.extend(_candidates_from_image(image_result, image_urls[0]))
                candidates = _dedupe_candidates(candidates)
                image_candidate = _single_image_candidate(candidates)
                if automatic_image_allowed and image_candidate is not None:
                    (
                        image_shop,
                        verified_image,
                        image_match_reason,
                    ) = _find_existing_shop_from_image(
                        db,
                        extracted,
                        image_candidate,
                    )
                    if image_shop is not None and verified_image is not None:
                        existing_shop = image_shop
                        resolution_basis = ResolutionBasis.IMAGE_EVIDENCE
                        existing_reason = image_match_reason or existing_reason
                        automatic_evidence_candidate = verified_image
                        candidates.append(verified_image)
                        candidates = _dedupe_candidates(candidates)
                    elif image_match_reason:
                        creation_blocked_reason = image_match_reason

                if (
                    existing_shop is None
                    and automatic_image_allowed
                    and creation_blocked_reason is None
                    and image_candidate is not None
                    and _image_matches_mention(extracted, image_candidate)
                ):
                    for verification_url in _image_verification_urls(
                        image_candidate,
                        candidates,
                    ):
                        candidates.extend(
                            await _structured_candidates(
                                db,
                                envelope.message_id,
                                verification_url,
                            )
                        )
                    candidates = _dedupe_candidates(candidates)
                    existing_shop, candidate_reason = _find_existing_shop(
                        db,
                        extracted,
                        candidates,
                    )
                    if existing_shop is not None:
                        resolution_basis = ResolutionBasis.IMAGE_EVIDENCE
                    if existing_shop is None:
                        new_candidate, image_candidate_reason = (
                            _structured_candidate_from_image(
                                image_candidate,
                                candidates,
                            )
                        )
                        mention_area = canonicalize_area(extracted.area)
                        if (
                            new_candidate is not None
                            and mention_area is not None
                            and not _candidate_matches_area(
                                mention_area,
                                new_candidate,
                            )
                        ):
                            new_candidate = None
                            image_candidate_reason = (
                                "画像と構造化候補の地域が投稿エリアと一致しない"
                            )
                        if image_candidate_reason:
                            if new_candidate is None:
                                creation_blocked_reason = image_candidate_reason
                            else:
                                image_reason = (
                                    f"{image_reason} / {image_candidate_reason}"
                                )
                    existing_reason = candidate_reason or existing_reason
                    if candidate_reason and existing_shop is None:
                        creation_blocked_reason = candidate_reason
                collision_candidate = new_candidate
                (
                    new_candidate,
                    collision_shop,
                    collision_reason,
                ) = _guard_new_shop_creation_or_link(
                    db,
                    extracted,
                    new_candidate,
                    creation_blocked_reason,
                )
                if collision_shop is not None:
                    existing_shop = collision_shop
                    existing_reason = collision_reason
                    automatic_evidence_candidate = collision_candidate
                    resolution_basis = ResolutionBasis.VERIFIED_COLLISION
                    creation_blocked_reason = None
                else:
                    creation_blocked_reason = collision_reason
            if (
                existing_shop is None
                and new_candidate is None
                and creation_blocked_reason is None
                and deferred_page_block_reason is not None
            ):
                creation_blocked_reason = deferred_page_block_reason
            chosen_shop = existing_shop
            if chosen_shop is None and new_candidate is not None:
                chosen_shop = _new_shop_from_candidate(extracted, new_candidate)
                db.add(chosen_shop)
                db.flush()
                resolution_basis = (
                    ResolutionBasis.IMAGE_EVIDENCE
                    if new_candidate.provenance == CandidateProvenance.IMAGE
                    else ResolutionBasis.VERIFIED_CANDIDATE
                )

            final_area = chosen_shop.area if chosen_shop is not None else extracted.area
            final_category = (
                chosen_shop.category if chosen_shop is not None else extracted.category
            )
            metadata_status, metadata_difference = _metadata_review_state(
                final_area,
                final_category,
            )
            automatically_resolved = chosen_shop is not None
            reason_parts = [extracted.confidence_reason]
            if canonical_assets_ambiguous:
                reason_parts.append(
                    "複数の店舗ID付きURLと投稿内店舗の対応を一意に確認できない"
                )
            if creation_blocked_reason:
                existing_reason = creation_blocked_reason
            if existing_reason:
                reason_parts.append(existing_reason)
            if new_candidate:
                reason_parts.append(new_candidate.verification_reason or "検証済み候補で確定")
                if (
                    new_candidate.provenance == CandidateProvenance.WEB_SEARCH
                    and new_candidate.is_verified
                ):
                    candidates = _replace_web_candidate_with_verified_page(
                        candidates,
                        new_candidate,
                    )
                elif new_candidate not in candidates:
                    candidates.insert(0, new_candidate)
                    candidates = _dedupe_candidates(candidates)
            if search_unresolved_reason:
                reason_parts.append(f"Web検索未解決: {search_unresolved_reason}")
            if image_reason:
                reason_parts.append(f"画像補助: {image_reason}")
            if metadata_difference == "unknown_category" and final_category:
                reason_parts.append(f"未知カテゴリ: {final_category}")
            if metadata_difference == "unknown_area" and final_area:
                reason_parts.append(f"未知エリア: {final_area}")

            difference_type = None if automatically_resolved else "new_ambiguous"
            if image_assessment.requires_review:
                difference_type = "evidence_review"
            if image_assessment.is_event_excluded:
                difference_type = EVENT_EXCLUDED

            mention_row = ShopMention(
                message_id=envelope.message_id,
                shop_id=chosen_shop.id if chosen_shop is not None else None,
                occurrence_index=occurrence,
                extracted_name=extracted.shop_name,
                extracted_branch_name=extracted.branch_name,
                extracted_area=extracted.area,
                extracted_category=extracted.category,
                source_url=(
                    extracted.source_url
                    if using_source_discovery
                    else source_url
                ),
                resolution_status=(
                    ResolutionStatus.RESOLVED.value
                    if automatically_resolved
                    else ResolutionStatus.INVALID.value if image_assessment.is_event_excluded else ResolutionStatus.AMBIGUOUS.value
                ),
                review_status=(
                    ReviewStatus.APPROVED.value
                    if automatically_resolved
                    else ReviewStatus.REJECTED.value if image_assessment.is_event_excluded else ReviewStatus.PENDING.value
                ),
                metadata_review_status=MetadataReviewStatus.DEFERRED.value if image_assessment.is_event_excluded else metadata_status.value,
                metadata_difference_type=metadata_difference,
                resolution_method=(
                    ResolutionMethod.AUTOMATIC.value if automatically_resolved else None
                ),
                resolution_basis=(
                    resolution_basis.value if resolution_basis is not None else None
                ),
                difference_type=difference_type,
                extraction_source=(
                    "legacy_hint"
                    if using_fallback_mentions
                    else (
                        "responses_source_discovery"
                        if using_source_discovery
                        else "responses_structured"
                    )
                ),
                extraction_error=evidence_review_error(image_assessment),
                confidence_reason=" / ".join(reason_parts),
                reviewed_at=utc_now() if automatically_resolved else None,
                metadata_reviewed_at=(
                    utc_now()
                    if metadata_status == MetadataReviewStatus.APPROVED
                    else None
                ),
            )
            db.add(mention_row)
            db.flush()
            _store_candidates(
                db,
                mention_row,
                extracted,
                candidates,
                new_candidate or automatic_evidence_candidate,
            )
            if chosen_shop is not None:
                shop_ids.append(chosen_shop.id)
            if not automatically_resolved and not image_assessment.is_event_excluded:
                pending_ids.append(mention_row.id)

        message.processing_status = ProcessingStatus.SUCCEEDED.value
        message.processed_at = utc_now()
        if commit:
            db.commit()
        else:
            db.flush()
        return MessageProcessResult(
            envelope.message_id,
            tuple(shop_ids),
            tuple(pending_ids),
            False,
        )
    except Exception as exc:
        if not commit:
            raise
        db.rollback()
        failed = db.query(Message).filter(Message.message_id == envelope.message_id).first()
        if failed is None:
            failed = Message(
                message_id=envelope.message_id,
                channel_id=envelope.channel_id,
                content=envelope.content,
                source_created_at=envelope.created_at,
                is_target=True,
            )
            db.add(failed)
        failed.processing_status = ProcessingStatus.FAILED.value
        failed.fetch_error = f"{type(exc).__name__}: {exc}"
        db.flush()
        _replace_source_assets(db, envelope.message_id, envelope.assets)
        db.add(
            ProcessingRun(
                message_id=envelope.message_id,
                stage="message_pipeline",
                prompt_version=PROMPT_VERSION,
                status=ProcessingStatus.FAILED.value,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        db.commit()
        raise


def _persist_operational_journal(
    db: Session,
    journal: _OperationalJournal,
) -> None:
    for run in journal.runs:
        db.add(_processing_run(run))
    for kind, value in journal.invalidated_caches:
        key = _cache_key(kind, value)
        db.query(LookupCache).filter(LookupCache.cache_key == key).delete(
            synchronize_session=False
        )
    for cache in journal.caches.values():
        _put_cache(db, cache.kind, cache.value, cache.payload)


async def process_message(
    db: Session,
    envelope: MessageEnvelope,
    *,
    allow_source_discovery: bool = False,
) -> MessageProcessResult:
    journal = _OperationalJournal()
    token: Token[_OperationalJournal | None] = _ACTIVE_OPERATIONAL_JOURNAL.set(journal)
    journal_is_active = True
    try:
        return await _process_message_transaction(
            db,
            envelope,
            allow_source_discovery=allow_source_discovery,
        )
    except Exception:
        _ACTIVE_OPERATIONAL_JOURNAL.reset(token)
        journal_is_active = False
        db.rollback()
        try:
            _persist_operational_journal(db, journal)
            db.commit()
        except Exception as persistence_error:
            db.rollback()
            logger.exception(
                "Failed to preserve pipeline operational records: message_id=%s error=%s",
                envelope.message_id,
                persistence_error,
            )
        raise
    finally:
        if journal_is_active:
            _ACTIVE_OPERATIONAL_JOURNAL.reset(token)
