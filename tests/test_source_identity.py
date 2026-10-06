from __future__ import annotations

import pytest

from services.source_identity import (
    SourceService,
    identify_source_url,
    source_asset_fingerprint,
    source_asset_identity,
)


@pytest.mark.parametrize(
    ("url", "service", "item_id", "normalized_url"),
    (
        (
            "https://twitter.com/food/status/1234567890123456789?s=20",
            SourceService.X,
            "1234567890123456789",
            "https://x.com/i/status/1234567890123456789",
        ),
        (
            "https://x.com/i/web/status/1234567890123456789",
            SourceService.X,
            "1234567890123456789",
            "https://x.com/i/status/1234567890123456789",
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ?si=abc",
            SourceService.YOUTUBE,
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            SourceService.YOUTUBE,
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.instagram.com/reel/ABC_def-123/?utm_source=share",
            SourceService.INSTAGRAM,
            "ABC_def-123",
            "https://www.instagram.com/p/ABC_def-123/",
        ),
        (
            "https://www.tiktok.com/@food/video/7412345678901234567",
            SourceService.TIKTOK,
            "7412345678901234567",
            "https://www.tiktok.com/video/7412345678901234567",
        ),
    ),
)
def test_identify_source_url_normalizes_social_post_variants(
    url: str,
    service: SourceService,
    item_id: str,
    normalized_url: str,
) -> None:
    identity = identify_source_url(url)

    assert identity is not None
    assert identity.service == service
    assert identity.item_id == item_id
    assert identity.normalized_url == normalized_url


def test_source_asset_identity_keeps_non_social_url_without_source_id() -> None:
    identity = source_asset_identity(
        "https://example.com/shop/?utm_source=x&b=2&a=1#menu"
    )

    assert identity is not None
    assert identity.source_service is None
    assert identity.source_item_id is None
    assert identity.normalized_url == "https://example.com/shop?a=1&b=2"


def test_source_asset_fingerprint_is_stable_across_whitespace() -> None:
    first = source_asset_fingerprint(
        kind="embed",
        normalized_url="https://x.com/i/status/123",
        title="  Good   shop ",
        description="Lunch\nmenu",
    )
    second = source_asset_fingerprint(
        kind="EMBED",
        normalized_url="https://x.com/i/status/123",
        title="good shop",
        description="lunch menu",
    )

    assert first == second
    assert len(first) == 64
