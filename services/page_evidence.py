from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bot.restaurant_extractor import extract_json_ld_candidates
from services.resolution import CandidateIdentity, normalize_address, normalize_name


_POSTAL_CODE_RE = re.compile(r"〒?[0-9]{3}-?[0-9]{4}")
_POSTAL_CODE_ANY_RE = re.compile(r"〒?[0-9０-９]{3}-?[0-9０-９]{4}")
_JAPANESE_ADDRESS_MARKER_RE = re.compile(
    r"(?:東京都|北海道|京都府|大阪府|.{2,3}県)"
)
_NAME_CHAR_PATTERN = r"0-9a-zぁ-んァ-ヶ一-龠ー"
_NAME_SEPARATOR_PATTERN = rf"[^{_NAME_CHAR_PATTERN}]*"
_STREET_NUMBER_RE = re.compile(r"(?<![0-9])[0-9]+(?:-[0-9]+){2,}(?![0-9])")
_ADDRESS_CONTEXT_LABELS = frozenset({"", "住所", "所在地", "店舗住所", "address"})
_SPACE_RE = re.compile(r"\s+")
_HEADING_TAGS = frozenset({"title", "h1"})
_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})


@dataclass(frozen=True)
class RestaurantPageEvidence:
    headings: tuple[str, ...]
    text_blocks: tuple[str, ...]
    visible_text: str
    structured_candidates: tuple[CandidateIdentity, ...]
    page_sha256: str


class PageCandidateProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["structured_data", "visible_page"]
    page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate: CandidateIdentity


class _RestaurantHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._heading_tags: list[str] = []
        self._heading_parts: list[list[str]] = []
        self.headings: list[str] = []
        self.visible_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in _SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if normalized_tag in _HEADING_TAGS:
            self._heading_tags.append(normalized_tag)
            self._heading_parts.append([])
        if normalized_tag != "meta":
            return
        values = {
            key.casefold(): value
            for key, value in attrs
            if value is not None
        }
        property_name = values.get("property") or values.get("name")
        content = values.get("content")
        if property_name and property_name.casefold() == "og:title" and content:
            self.headings.append(content)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in _SKIPPED_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth or not self._heading_tags:
            return
        if self._heading_tags[-1] != normalized_tag:
            return
        self._heading_tags.pop()
        parts = self._heading_parts.pop()
        heading = _SPACE_RE.sub(" ", " ".join(parts)).strip()
        if heading:
            self.headings.append(heading)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = data.strip()
        if not value:
            return
        self.visible_parts.append(value)
        if self._heading_parts:
            self._heading_parts[-1].append(value)


def extract_restaurant_page_evidence(
    html: str,
    page_url: str,
) -> RestaurantPageEvidence:
    parser = _RestaurantHtmlParser()
    parser.feed(html)
    parser.close()
    headings = tuple(dict.fromkeys(parser.headings))
    text_blocks = tuple(
        dict.fromkeys(
            value
            for part in parser.visible_parts
            if (value := _SPACE_RE.sub(" ", part).strip())
        )
    )
    visible_text = _SPACE_RE.sub(" ", " ".join(text_blocks)).strip()
    return RestaurantPageEvidence(
        headings=headings,
        text_blocks=text_blocks,
        visible_text=visible_text,
        structured_candidates=tuple(extract_json_ld_candidates(html, page_url)),
        page_sha256=hashlib.sha256(html.encode("utf-8")).hexdigest(),
    )


def _comparable_address(value: str | None) -> str:
    normalized = normalize_address(value) or ""
    return _POSTAL_CODE_RE.sub("", normalized)


def page_addresses_match(left: str | None, right: str | None) -> bool:
    left_address = _comparable_address(left)
    return bool(left_address and left_address == _comparable_address(right))


def page_text_mentions_restaurant_name(name: str, text: str) -> bool:
    return _restaurant_name_pattern(name).search(
        unicodedata.normalize("NFKC", text).casefold()
    ) is not None


def page_text_without_restaurant_name(name: str, text: str) -> str | None:
    pattern = _restaurant_name_pattern(name)
    normalized_text = unicodedata.normalize("NFKC", text).casefold()
    if pattern.search(normalized_text) is None:
        return None
    return pattern.sub(" ", normalized_text)


def _restaurant_name_pattern(name: str) -> re.Pattern[str]:
    normalized_name = normalize_name(name)
    if not normalized_name:
        return re.compile(r"(?!)")
    separator = _NAME_SEPARATOR_PATTERN
    body = separator.join(re.escape(character) for character in normalized_name)
    left_boundary = rf"(?<![{_NAME_CHAR_PATTERN}])"
    right_boundary = rf"(?![{_NAME_CHAR_PATTERN}])"
    return re.compile(f"{left_boundary}{body}{right_boundary}")


def page_mentions_restaurant_name(
    name: str,
    evidence: RestaurantPageEvidence,
) -> bool:
    normalized_name = normalize_name(name)
    if not normalized_name:
        return False
    if any(
        normalize_name(candidate.name) == normalized_name
        for candidate in evidence.structured_candidates
    ):
        return True
    return any(
        page_text_mentions_restaurant_name(name, text)
        for text in (*evidence.headings, *evidence.text_blocks)
    )


def _address_occurrence_count(value: str) -> int:
    normalized = unicodedata.normalize("NFKC", value)
    postal_count = len(_POSTAL_CODE_ANY_RE.findall(normalized))
    prefecture_count = len(_JAPANESE_ADDRESS_MARKER_RE.findall(normalized))
    without_postal = _POSTAL_CODE_ANY_RE.sub("", normalized)
    normalized_address = normalize_address(without_postal) or ""
    street_number_count = len(_STREET_NUMBER_RE.findall(normalized_address))
    return max(postal_count, prefecture_count, street_number_count)


def _block_binds_name_and_address(
    block: str,
    candidate_name: str,
    candidate_address: str,
) -> bool:
    if not page_text_mentions_restaurant_name(candidate_name, block):
        return False
    if _address_occurrence_count(block) != 1:
        return False
    block_address = _comparable_address(block)
    if not block_address or not block_address.endswith(candidate_address):
        return False
    prefix = normalize_name(block_address[: -len(candidate_address)])
    normalized_candidate_name = normalize_name(candidate_name)
    if prefix.count(normalized_candidate_name) != 1:
        return False
    context = prefix.replace(normalized_candidate_name, "", 1)
    return context in _ADDRESS_CONTEXT_LABELS


def prove_page_candidate(
    candidate: CandidateIdentity,
    evidence: RestaurantPageEvidence,
) -> PageCandidateProof | None:
    candidate_name = normalize_name(candidate.name)
    candidate_address = _comparable_address(candidate.address)
    if not candidate_name or not candidate_address:
        return None

    structured = evidence.structured_candidates
    if structured:
        if len(structured) != 1:
            return None
        page_candidate = structured[0]
        if (
            normalize_name(page_candidate.name) != candidate_name
            or not page_addresses_match(page_candidate.address, candidate.address)
        ):
            return None
        return PageCandidateProof(
            method="structured_data",
            page_sha256=evidence.page_sha256,
            candidate=page_candidate,
        )

    if any(
        _block_binds_name_and_address(
            block,
            candidate.name,
            candidate_address,
        )
        for block in evidence.text_blocks
    ):
        return PageCandidateProof(
            method="visible_page",
            page_sha256=evidence.page_sha256,
            candidate=candidate.model_copy(
                update={
                    "category": None,
                    "phone": None,
                    "external_source": None,
                    "external_id": None,
                }
            ),
        )

    return None
