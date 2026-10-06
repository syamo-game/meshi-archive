from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, Message, SourceAsset
from scripts.restore_discord_sources import (
    DiscordMessagePayload,
    fetch_message,
    parse_args,
    save_success,
)


def discord_payload() -> dict[str, object]:
    return {
        "id": "12345678901234567",
        "channel_id": "22345678901234567",
        "content": "割烹みやび",
        "timestamp": "2026-01-02T03:04:05+00:00",
        "attachments": [],
        "embeds": [],
    }


def test_restore_arguments_accept_specific_message_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "restore_discord_sources.py",
            "--message-id",
            "12345678901234567",
            "--message-id",
            "22345678901234567",
            "--channel-id",
            "32345678901234567",
        ],
    )

    args = parse_args()

    assert args.limit is None
    assert args.message_ids == ("12345678901234567", "22345678901234567")
    assert args.channel_id == "32345678901234567"


def test_discord_not_found_is_recorded_as_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Unknown Message"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            outcome = await fetch_message(
                client,
                "token",
                "22345678901234567",
                "12345678901234567",
            )
        assert outcome.payload is None
        assert outcome.http_status == 404
        assert outcome.error is not None
        assert "source_unavailable" in outcome.error

    asyncio.run(run())


def test_discord_retries_server_errors_at_most_three_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 4:
            return httpx.Response(500, json={"message": "temporary"})
        return httpx.Response(200, json=discord_payload())

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            outcome = await fetch_message(
                client,
                "token",
                "22345678901234567",
                "12345678901234567",
            )
        assert outcome.payload is not None
        assert calls == 4

    asyncio.run(run())


def test_discord_restore_populates_source_identity_fields() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        source_url = "https://twitter.com/food/status/1924813456236278183?s=20"
        payload_data = discord_payload()
        payload_data["content"] = source_url
        payload_data["timestamp"] = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        payload = DiscordMessagePayload.model_validate(payload_data)
        message = Message(message_id=payload.id)
        db.add(message)

        save_success(db, message, payload)
        db.commit()
        asset = db.query(SourceAsset).one()

        assert asset.source_service == "x"
        assert asset.source_item_id == "1924813456236278183"
        assert asset.normalized_url == "https://x.com/i/status/1924813456236278183"
        assert len(asset.content_fingerprint or "") == 64
    finally:
        db.close()
        engine.dispose()
