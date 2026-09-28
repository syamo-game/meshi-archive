from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from web.area_groups import canonicalize_area


_CORPORATE_PREFIXES = ("株式会社", "有限会社", "合同会社")
_BRANCH_PATTERN = re.compile(r"([^\s　]+(?:本店|支店|店))$")
_NON_WORD_PATTERN = re.compile(r"[^0-9a-zぁ-んァ-ヶ一-龠ー]+", re.IGNORECASE)
_EXTERNAL_SOURCE_ALIASES = {
    "食べログ": "tabelog",
    "tabelogcom": "tabelog",
    "wwwtabelogcom": "tabelog",
    "ホットペッパー": "hotpepper",
    "hotpepperjp": "hotpepper",
    "wwwhotpepperjp": "hotpepper",
    "ぐるなび": "gnavi",
    "gnavicojp": "gnavi",
    "wwwgnavicojp": "gnavi",
    "gurunavi": "gnavi",
    "gurunavicom": "gnavi",
    "wwwgurunavicom": "gnavi",
    "一休": "ikyu",
    "ikyucom": "ikyu",
    "wwwikyucom": "ikyu",
    "retty": "retty",
    "rettyme": "retty",
    "wwwrettyme": "retty",
}


class CandidateIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=500)
    area: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=2_000)
    phone: str | None = Field(default=None, max_length=64)
    canonical_url: str | None = Field(default=None, max_length=2_048)
    external_source: str | None = Field(default=None, max_length=64)
    external_id: str | None = Field(default=None, max_length=255)
    evidence_url: str | None = Field(default=None, max_length=2_048)

    @field_validator("canonical_url", "evidence_url")
    @classmethod
    def validate_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlparse(value)
            parsed.port
        except ValueError as exc:
            raise ValueError(f"URL is invalid: {exc}") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("URL must use http or https")
        if parsed.username or parsed.password:
            raise ValueError("URL userinfo is not allowed")
        return value


class ResolutionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_strong_match: bool
    name_similarity: float = Field(ge=0.0, le=1.0)
    matched_fields: tuple[str, ...]
    conflicting_fields: tuple[str, ...]
    reason: str


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower().strip()
    for prefix in _CORPORATE_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return _NON_WORD_PATTERN.sub("", normalized)


def normalize_area(value: str | None) -> str:
    canonical = canonicalize_area(value)
    comparable = canonical or value
    if not comparable:
        return ""
    normalized = unicodedata.normalize("NFKC", comparable).lower().strip()
    return _NON_WORD_PATTERN.sub("", normalized)


def normalize_external_source(value: str | None) -> str | None:
    if not value:
        return None
    normalized = normalize_name(value)
    if not normalized:
        return None
    return _EXTERNAL_SOURCE_ALIASES.get(normalized, normalized)


def normalize_external_identity(
    source: str | None,
    external_id: str | None,
) -> tuple[str, str] | None:
    normalized_source = normalize_external_source(source)
    normalized_id = (
        unicodedata.normalize("NFKC", external_id).strip().casefold()
        if external_id
        else ""
    )
    if not normalized_source or not normalized_id:
        return None
    return normalized_source, normalized_id


def extract_branch_token(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", value).strip().rstrip(
        ")]}」』】〉》〕〗〙〛"
    )
    match = _BRANCH_PATTERN.search(normalized)
    if match is None:
        return None
    token = match.group(1)
    opening_index = max(
        (token.rfind(opening) for opening in "([{「『【〈《〔〖〘〚"),
        default=-1,
    )
    if opening_index >= 0:
        token = token[opening_index + 1 :]
    return normalize_name(token)


def branches_conflict(left: str, right: str) -> bool:
    left_branch = extract_branch_token(left)
    right_branch = extract_branch_token(right)
    return bool(left_branch and right_branch and left_branch != right_branch)


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", unicodedata.normalize("NFKC", value))
    if digits.startswith("81") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits or None


def normalize_address(value: str | None) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = normalized.replace("丁目", "-").replace("番地", "-").replace("番", "-")
    normalized = normalized.replace("号", "")
    normalized = re.sub(r"[\s　,，.。・]", "", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized or None


def normalize_url_identity(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Canonical URL has an invalid port") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Canonical URL must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("Canonical URL must not contain userinfo")
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def name_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_name(left), normalize_name(right)).ratio()


def evaluate_identity(existing: CandidateIdentity, candidate: CandidateIdentity) -> ResolutionOutcome:
    similarity = name_similarity(existing.name, candidate.name)
    matched: list[str] = []
    conflicts: list[str] = []

    branch_conflict = branches_conflict(existing.name, candidate.name)
    if branch_conflict:
        conflicts.append("branch")

    existing_external_identity = normalize_external_identity(
        existing.external_source,
        existing.external_id,
    )
    candidate_external_identity = normalize_external_identity(
        candidate.external_source,
        candidate.external_id,
    )
    same_external_identity = bool(
        existing_external_identity
        and existing_external_identity == candidate_external_identity
    )
    external_identity_conflict = bool(
        existing_external_identity
        and candidate_external_identity
        and existing_external_identity[0] == candidate_external_identity[0]
        and existing_external_identity[1] != candidate_external_identity[1]
    )
    if same_external_identity:
        matched.append("external_id")
    elif external_identity_conflict:
        conflicts.append("external_id")

    existing_phone = normalize_phone(existing.phone)
    candidate_phone = normalize_phone(candidate.phone)
    same_phone = bool(existing_phone and candidate_phone and existing_phone == candidate_phone)
    if same_phone:
        matched.append("phone")
    elif existing_phone and candidate_phone:
        conflicts.append("phone")

    existing_address = normalize_address(existing.address)
    candidate_address = normalize_address(candidate.address)
    same_address = bool(
        existing_address and candidate_address and existing_address == candidate_address
    )
    if same_address:
        matched.append("address")
    elif existing_address and candidate_address:
        conflicts.append("address")

    if similarity >= 0.8:
        matched.append("name")
    else:
        conflicts.append("name")

    strong = bool(
        not branch_conflict
        and not external_identity_conflict
        and (
            same_external_identity
            or (same_phone and similarity >= 0.8)
            or (same_address and similarity >= 0.9)
        )
    )

    if branch_conflict:
        reason = "支店名が矛盾"
    elif external_identity_conflict:
        reason = "同一サービスの外部IDが矛盾"
    elif same_external_identity:
        reason = "同一サービスの外部IDが一致"
    elif same_phone and similarity >= 0.8:
        reason = "電話番号が一致し、店名の類似度が0.80以上"
    elif same_address and similarity >= 0.9:
        reason = "住所が一致し、店名の類似度が0.90以上"
    else:
        reason = "自動確定に必要な強い一致根拠が不足"

    return ResolutionOutcome(
        is_strong_match=strong,
        name_similarity=similarity,
        matched_fields=tuple(matched),
        conflicting_fields=tuple(conflicts),
        reason=reason,
    )
