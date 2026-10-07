from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session, selectinload

from bot.restaurant_extractor import (
    CandidateSearchContext,
    ExtractedMention,
    ExtractionError,
    extract_restaurant_message,
    search_restaurant_candidates,
)
from db.models import ShopMention
from services.identification_pipeline import _sanitize_cited_web_candidate


logger = logging.getLogger(__name__)
SUGGESTION_TIMEOUT_SECONDS: float = 45.0
MAX_SOURCE_CHARACTERS: int = 16_000
MAX_ACTIVE_SUGGESTIONS: int = 2
_ACTIVE_SUGGESTIONS: set[int] = set()
_ACTIVE_LOCK = Lock()
_UNRESOLVED_NAME = "（店舗名未特定）"


def validate_reference_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("参考URLのポートが不正です。") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"} or not host
        or parsed.username or parsed.password
        or port not in {None, 80, 443}
        or host == "localhost" or host.endswith((".localhost", ".local"))
        or "." not in host
    ):
        raise ValueError("参考URLは公開されたHTTP/HTTPSのURLで指定してください。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("参考URLに内部ネットワークのアドレスは使用できません。")
    return value


class SuggestionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    shop_name: str | None = Field(default=None, max_length=500)
    branch_name: str | None = Field(default=None, max_length=255)
    area: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=2_000)
    phone: str | None = Field(default=None, max_length=64)
    canonical_url: str | None = Field(default=None, max_length=2_048)

    @field_validator("canonical_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return validate_reference_url(value)


class ReviewSuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    expected_version: int = Field(ge=1)
    shop_version: int | None = Field(default=None, ge=1)
    draft: SuggestionDraft = Field(default_factory=SuggestionDraft)
    supplement: str = Field(default="", max_length=4_000)
    reference_url: str | None = Field(default=None, max_length=2_048)

    @field_validator("reference_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return validate_reference_url(value)


class SuggestedShop(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    shop_name: str
    branch_name: str | None
    area: str | None
    category: str | None
    address: str | None
    phone: str | None
    canonical_url: str | None
    evidence_urls: list[str]
    reason: str


class ReviewSuggestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mention_id: int
    expected_version: int
    shop_version: int | None
    candidates: list[SuggestedShop]
    unresolved_reason: str | None


@dataclass(frozen=True)
class SuggestionContext:
    mention_id: int
    version: int
    shop_id: int | None
    shop_version: int | None
    message_content: str
    draft: SuggestionDraft


class SuggestionFailure(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        super().__init__(message)


@contextmanager
def reserve_suggestion(mention_id: int) -> Iterator[None]:
    # Requests share this limit within one application worker.
    with _ACTIVE_LOCK:
        if mention_id in _ACTIVE_SUGGESTIONS:
            raise SuggestionFailure(409, "suggestion_in_progress", "この項目はAIで確認中です。完了を待ってください。")
        if len(_ACTIVE_SUGGESTIONS) >= MAX_ACTIVE_SUGGESTIONS:
            raise SuggestionFailure(429, "suggestion_capacity", "AI補完が混み合っています。少し待って再度お試しください。")
        _ACTIVE_SUGGESTIONS.add(mention_id)
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_SUGGESTIONS.remove(mention_id)


def load_suggestion_context(
    db: Session, mention_id: int, request: ReviewSuggestionRequest,
) -> SuggestionContext:
    try:
        mention = (
            db.query(ShopMention)
            .options(selectinload(ShopMention.message), selectinload(ShopMention.shop))
            .filter(ShopMention.id == mention_id)
            .populate_existing()
            .one_or_none()
        )
        if mention is None:
            raise SuggestionFailure(404, "mention_not_found", "確認項目が見つかりません。一覧を更新してください。")
        if mention.version != request.expected_version:
            raise SuggestionFailure(409, "stale_mention", "確認項目が変更されています。最新情報を読み込んでください。")
        if mention.review_status == "rejected":
            raise SuggestionFailure(409, "suggestion_excluded", "この項目は対象外です。AI補完は実行できません。")
        shop = mention.shop
        shop_version: int | None = shop.version if shop is not None else None
        if shop_version != request.shop_version:
            raise SuggestionFailure(409, "stale_shop", "店舗情報が変更されています。最新情報を読み込んでください。")
        fields: set[str] = request.draft.model_fields_set
        try:
            draft = SuggestionDraft(
                shop_name=request.draft.shop_name if "shop_name" in fields else shop.shop_name if shop is not None else mention.extracted_name,
                branch_name=request.draft.branch_name if "branch_name" in fields else shop.branch_name if shop is not None else mention.extracted_branch_name,
                area=request.draft.area if "area" in fields else shop.area if shop is not None else mention.extracted_area,
                category=request.draft.category if "category" in fields else shop.category if shop is not None else mention.extracted_category,
                address=request.draft.address if "address" in fields else shop.address if shop is not None else None,
                phone=request.draft.phone if "phone" in fields else shop.phone if shop is not None else None,
                canonical_url=request.draft.canonical_url if "canonical_url" in fields else shop.canonical_url if shop is not None else None,
            )
        except ValidationError as exc:
            logger.warning("Suggestion stored input is invalid: mention_id=%s", mention_id)
            raise SuggestionFailure(400, "suggestion_invalid_input", "保存された店舗情報に補完へ送れない値があります。入力欄を確認・修正してください。") from exc
        return SuggestionContext(
            mention_id=mention.id, version=mention.version,
            shop_id=mention.shop_id, shop_version=shop_version,
            message_content=mention.message.content or "", draft=draft,
        )
    finally:
        # End the read transaction before any network request.
        db.rollback()


def verify_suggestion_context(
    db: Session, expected: SuggestionContext, request: ReviewSuggestionRequest,
) -> None:
    current = load_suggestion_context(db, expected.mention_id, request)
    if current.shop_id != expected.shop_id or current.message_content != expected.message_content:
        raise SuggestionFailure(409, "stale_mention", "元のデータが変更されています。最新情報を読み込んでください。")


async def _search(context: SuggestionContext, request: ReviewSuggestionRequest) -> ReviewSuggestionResponse:
    draft = context.draft
    name: str = (draft.shop_name or "").strip()
    mention: ExtractedMention
    if not name or name == _UNRESOLVED_NAME:
        if len(context.message_content) > MAX_SOURCE_CHARACTERS:
            raise SuggestionFailure(400, "source_too_large", "元投稿が長いため、対象の店名を入力してからAI補完してください。")
        if not context.message_content.strip() and not request.supplement:
            raise SuggestionFailure(400, "suggestion_input_required", "店名または店舗を特定する補足情報を入力してください。")
        extraction_input: str = (
            "[元の投稿]\n" + context.message_content
            + "\n[管理者の補足情報・原文とは別の未検証の手掛かり]\n"
            + request.supplement
            + "\n[管理者の入力欄]\n" + draft.model_dump_json(exclude_none=True)
            + "\n[参考URL]\n" + (request.reference_url or "")
        )
        extraction = await extract_restaurant_message(extraction_input)
        if len(extraction.message.mentions) != 1:
            reason: str = (
                "複数の店舗が見つかりました。対象の店名を入力して再度補完してください。"
                if len(extraction.message.mentions) > 1
                else extraction.message.unresolved_reason or extraction.message.ignore_reason
                or "店舗を特定できませんでした。店名や所在地を追加してください。"
            )
            return ReviewSuggestionResponse(
                mention_id=context.mention_id, expected_version=context.version,
                shop_version=context.shop_version, candidates=[], unresolved_reason=reason,
            )
        mention = extraction.message.mentions[0]
        if mention.subject_kind in {"event", "product"}:
            return ReviewSuggestionResponse(
                mention_id=context.mention_id, expected_version=context.version,
                shop_version=context.shop_version, candidates=[],
                unresolved_reason="催事・商品から店舗を確定できません。対象の常設店舗名を入力してください。",
            )
        mention = mention.model_copy(update={
            "branch_name": draft.branch_name or mention.branch_name,
            "area": draft.area or mention.area,
            "category": draft.category or mention.category,
        })
    else:
        mention = ExtractedMention(
            shop_name=name, branch_name=draft.branch_name or None,
            area=draft.area or None, category=draft.category or None,
            needs_review=True, confidence_reason="管理者の入力を検索の手掛かりに使用",
        )
    references: tuple[str, ...] = tuple(dict.fromkeys(
        url for url in (request.reference_url, draft.canonical_url) if url
    ))
    hints = CandidateSearchContext(
        category=draft.category, address=draft.address, phone=draft.phone,
        supplement=request.supplement, reference_urls=references,
    )
    # Each request uses its full input; no partial-key cache is reused.
    result = await search_restaurant_candidates(mention, context=hints)
    if result.metrics.web_search_calls != result.metrics.api_attempts:
        raise ExtractionError("Suggestion search did not complete the expected web lookup")
    candidates: list[SuggestedShop] = []
    rejected_count: int = 0
    for candidate in result.candidates.candidates:
        cited = _sanitize_cited_web_candidate(candidate, result.source_urls)
        if cited is None:
            rejected_count += 1
            continue
        evidence: list[str] = list(dict.fromkeys(
            url for url in (cited.evidence_url, cited.canonical_url) if url
        ))
        for url in evidence:
            validate_reference_url(url)
        candidates.append(SuggestedShop(
            shop_name=cited.name, branch_name=None, area=cited.area,
            category=cited.category, address=cited.address, phone=cited.phone,
            canonical_url=cited.canonical_url, evidence_urls=evidence,
            reason=cited.confidence_reason,
        ))
    reason = result.candidates.unresolved_reason
    if rejected_count:
        reason = " / ".join(part for part in (
            reason, "検索の出典を確認できない候補は表示していません。",
        ) if part)
        logger.warning("Suggestion candidates excluded: mention_id=%s count=%s", context.mention_id, rejected_count)
    if not candidates and not reason:
        reason = "根拠のある候補が見つかりませんでした。店名や所在地を追加してください。"
    return ReviewSuggestionResponse(
        mention_id=context.mention_id, expected_version=context.version,
        shop_version=context.shop_version, candidates=candidates, unresolved_reason=reason,
    )


async def suggest_shops(
    context: SuggestionContext, request: ReviewSuggestionRequest,
) -> ReviewSuggestionResponse:
    try:
        return await asyncio.wait_for(_search(context, request), timeout=SUGGESTION_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        logger.warning("Suggestion deadline exceeded: mention_id=%s", context.mention_id)
        raise SuggestionFailure(504, "suggestion_timeout", "AI補完が制限時間を超えました。入力は保持しています。再度お試しください。") from exc
    except (ExtractionError, ValidationError, ValueError) as exc:
        logger.error("Suggestion search failed: mention_id=%s error_type=%s", context.mention_id, type(exc).__name__)
        raise SuggestionFailure(502, "suggestion_failed", "AI補完に失敗しました。入力は保持しています。時間をおいて再度お試しください。") from exc
