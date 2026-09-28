from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import StrEnum
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourceService(StrEnum):
    X = "x"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"
    THREADS = "threads"


class SourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    service: SourceService
    item_id: str = Field(min_length=1, max_length=255)
    normalized_url: str = Field(min_length=1, max_length=2_048)

    @field_validator("normalized_url")
    @classmethod
    def validate_normalized_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("normalized_url must be an absolute HTTPS URL")
        return value


class SourceAssetIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_service: SourceService | None = None
    source_item_id: str | None = Field(default=None, max_length=255)
    normalized_url: str = Field(min_length=1, max_length=32_768)

    @model_validator(mode="after")
    def validate_identity_pair(self) -> SourceAssetIdentity:
        if (self.source_service is None) != (self.source_item_id is None):
            raise ValueError("source_service and source_item_id must be set together")
        return self


_X_HOSTS = frozenset(
    {
        "x.com",
        "www.x.com",
        "mobile.x.com",
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
    }
)
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)
_INSTAGRAM_HOSTS = frozenset(
    {"instagram.com", "www.instagram.com", "m.instagram.com"}
)
_FACEBOOK_HOSTS = frozenset(
    {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch"}
)
_TIKTOK_HOSTS = frozenset(
    {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
)
_THREADS_HOSTS = frozenset({"threads.net", "www.threads.net"})
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,255}$")
_X_STATUS_RE = re.compile(r"/(?:[^/]+|i/web)/status/(\d+)(?:/|$)", re.IGNORECASE)
_YOUTUBE_PATH_RE = re.compile(r"/(?:shorts|live|embed)/(?:[^/]*?/)?([A-Za-z0-9_-]{6,64})(?:/|$)")
_INSTAGRAM_PATH_RE = re.compile(r"/(p|reel|reels|tv)/([A-Za-z0-9_-]+)(?:/|$)", re.IGNORECASE)
_FACEBOOK_PATH_PATTERNS = (
    re.compile(r"/(?:[^/]+/)?posts/(\d+)(?:/|$)", re.IGNORECASE),
    re.compile(r"/(?:reel|videos)/(\d+)(?:/|$)", re.IGNORECASE),
    re.compile(r"/share/(?:p|r)/([A-Za-z0-9_-]+)(?:/|$)", re.IGNORECASE),
)
_TIKTOK_VIDEO_RE = re.compile(r"/(?:@[^/]+/)?video/(\d+)(?:/|$)", re.IGNORECASE)
_THREADS_POST_RE = re.compile(r"/(?:@[^/]+/)?post/([A-Za-z0-9_-]+)(?:/|$)", re.IGNORECASE)
_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "feature",
        "si",
        "s",
        "t",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)


def normalize_asset_url(value: str) -> str | None:
    normalized_input = unicodedata.normalize("NFKC", value).strip()
    try:
        parsed = urlparse(normalized_input)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_pairs = [
        (key, query_value)
        for key, query_value in parse_qs(
            parsed.query,
            keep_blank_values=True,
        ).items()
        if key.casefold() not in _TRACKING_QUERY_KEYS
        for query_value in query_value
    ]
    query = urlencode(sorted(query_pairs))
    return urlunparse((scheme, netloc, path, "", query, ""))


def identify_source_url(value: str) -> SourceIdentity | None:
    normalized_url = normalize_asset_url(value)
    if normalized_url is None:
        return None
    parsed = urlparse(normalized_url)
    host = parsed.hostname or ""
    path = parsed.path

    if host in _X_HOSTS:
        match = _X_STATUS_RE.search(path)
        if match is not None:
            item_id = match.group(1)
            return SourceIdentity(
                service=SourceService.X,
                item_id=item_id,
                normalized_url=f"https://x.com/i/status/{item_id}",
            )

    if host in _YOUTUBE_HOSTS:
        if host == "youtu.be":
            item_id = path.strip("/").split("/", 1)[0]
        else:
            item_id = parse_qs(parsed.query).get("v", [""])[0]
            if not item_id:
                match = _YOUTUBE_PATH_RE.search(path)
                item_id = match.group(1) if match is not None else ""
        if _ITEM_ID_RE.fullmatch(item_id):
            return SourceIdentity(
                service=SourceService.YOUTUBE,
                item_id=item_id,
                normalized_url=f"https://www.youtube.com/watch?v={item_id}",
            )

    if host in _INSTAGRAM_HOSTS:
        match = _INSTAGRAM_PATH_RE.search(path)
        if match is not None:
            item_id = match.group(2)
            return SourceIdentity(
                service=SourceService.INSTAGRAM,
                item_id=item_id,
                normalized_url=f"https://www.instagram.com/p/{item_id}/",
            )

    if host in _FACEBOOK_HOSTS:
        item_id = parse_qs(parsed.query).get("v", [""])[0]
        if not item_id:
            for pattern in _FACEBOOK_PATH_PATTERNS:
                match = pattern.search(path)
                if match is not None:
                    item_id = match.group(1)
                    break
        if item_id and len(item_id) <= 255:
            return SourceIdentity(
                service=SourceService.FACEBOOK,
                item_id=item_id,
                normalized_url=f"https://www.facebook.com/watch/?v={item_id}",
            )

    if host in _TIKTOK_HOSTS:
        match = _TIKTOK_VIDEO_RE.search(path)
        if match is not None:
            item_id = match.group(1)
            return SourceIdentity(
                service=SourceService.TIKTOK,
                item_id=item_id,
                normalized_url=f"https://www.tiktok.com/video/{item_id}",
            )

    if host in _THREADS_HOSTS:
        match = _THREADS_POST_RE.search(path)
        if match is not None:
            item_id = match.group(1)
            return SourceIdentity(
                service=SourceService.THREADS,
                item_id=item_id,
                normalized_url=f"https://www.threads.net/post/{item_id}",
            )
    return None


def source_asset_identity(value: str) -> SourceAssetIdentity | None:
    source_identity = identify_source_url(value)
    if source_identity is not None:
        return SourceAssetIdentity(
            source_service=source_identity.service,
            source_item_id=source_identity.item_id,
            normalized_url=source_identity.normalized_url,
        )
    normalized_url = normalize_asset_url(value)
    if normalized_url is None:
        return None
    return SourceAssetIdentity(normalized_url=normalized_url)


def source_asset_fingerprint(
    *,
    kind: str,
    normalized_url: str,
    title: str | None,
    description: str | None,
    extracted_text: str | None = None,
) -> str:
    components = (
        kind.strip().casefold(),
        normalized_url,
        _normalize_content(title),
        _normalize_content(description),
        _normalize_content(extracted_text),
    )
    return hashlib.sha256("\x1f".join(components).encode("utf-8")).hexdigest()


def _normalize_content(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()
