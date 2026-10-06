from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import SessionLocal, init_db
from db.models import (
    FetchStatus,
    Message,
    ProcessingRun,
    ProcessingStatus,
    ReviewStatus,
    ShopMention,
    SourceAsset,
)
from services.source_identity import source_asset_fingerprint, source_asset_identity


DISCORD_API_BASE = "https://discord.com/api/v10"
MESSAGE_ID_RE = re.compile(r"^[0-9]{17,20}$")
URL_RE = re.compile(r"https?://[^\s<>]+")


class DiscordAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: str
    filename: str
    size: int = Field(ge=0)
    url: str
    proxy_url: str
    content_type: str | None = None
    description: str | None = None


class DiscordEmbedMedia(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    url: str


class DiscordEmbed(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    title: str | None = None
    description: str | None = None
    url: str | None = None
    image: DiscordEmbedMedia | None = None
    thumbnail: DiscordEmbedMedia | None = None


class DiscordMessagePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: str
    channel_id: str
    content: str
    timestamp: datetime
    attachments: list[DiscordAttachment]
    embeds: list[DiscordEmbed]


class DiscordRateLimit(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    retry_after: float


@dataclass(frozen=True)
class FetchOutcome:
    payload: DiscordMessagePayload | None
    error: str | None
    http_status: int | None


@dataclass(frozen=True)
class RestoreArgs:
    apply: bool
    limit: int | None
    channel_id: str | None
    message_ids: tuple[str, ...]


@dataclass
class RestoreCounts:
    requested: int = 0
    restored: int = 0
    unavailable: int = 0
    failed: int = 0


def parse_args() -> RestoreArgs:
    parser = argparse.ArgumentParser(
        description="Restore Discord message bodies and asset metadata without running OpenAI."
    )
    parser.add_argument("--apply", action="store_true", help="Persist fetched source data.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--channel-id", default=os.getenv("DISCORD_CHANNEL_ID"))
    parser.add_argument(
        "--message-id",
        action="append",
        default=[],
        help="Restore only this message ID. Repeat to select multiple messages.",
    )
    namespace = parser.parse_args()
    limit = namespace.limit
    if limit is not None and limit < 1:
        parser.error("--limit must be at least 1")
    message_ids = tuple(dict.fromkeys(namespace.message_id))
    if limit is not None and message_ids:
        parser.error("--limit and --message-id cannot be used together")
    invalid_message_ids = [
        message_id for message_id in message_ids if not MESSAGE_ID_RE.fullmatch(message_id)
    ]
    if invalid_message_ids:
        parser.error("--message-id must be an exact 17-20 digit string")
    channel_id = namespace.channel_id
    if channel_id is not None and not MESSAGE_ID_RE.fullmatch(channel_id):
        parser.error("--channel-id must be an exact 17-20 digit string")
    return RestoreArgs(
        apply=namespace.apply,
        limit=limit,
        channel_id=channel_id,
        message_ids=message_ids,
    )


def retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    if response.status_code == 429:
        try:
            limit = DiscordRateLimit.model_validate_json(response.content)
            return max(0.0, limit.retry_after)
        except ValidationError:
            pass
    return min(8.0, 2.0**attempt)


async def fetch_message(
    client: httpx.AsyncClient,
    token: str,
    channel_id: str,
    message_id: str,
) -> FetchOutcome:
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"
    for attempt in range(4):
        try:
            response = await client.get(url, headers={"Authorization": f"Bot {token}"})
        except httpx.HTTPError as exc:
            if attempt < 3:
                await asyncio.sleep(min(8.0, 2.0**attempt))
                continue
            return FetchOutcome(
                payload=None,
                error=(
                    "stage=discord_fetch "
                    f"channel_id={channel_id} message_id={message_id} "
                    f"error={type(exc).__name__}: {exc}"
                ),
                http_status=None,
            )

        if response.status_code == 200:
            try:
                payload = DiscordMessagePayload.model_validate_json(response.content)
            except ValidationError as exc:
                return FetchOutcome(
                    payload=None,
                    error=(
                        "stage=discord_parse http_status=200 "
                        f"channel_id={channel_id} message_id={message_id} "
                        f"error={type(exc).__name__}: {exc}"
                    ),
                    http_status=200,
                )
            if payload.id != message_id or payload.channel_id != channel_id:
                return FetchOutcome(
                    payload=None,
                    error=(
                        "stage=discord_validate http_status=200 "
                        f"channel_id={channel_id} message_id={message_id} "
                        f"payload_channel_id={payload.channel_id} payload_message_id={payload.id}"
                    ),
                    http_status=200,
                )
            return FetchOutcome(payload=payload, error=None, http_status=200)

        if response.status_code in {403, 404}:
            return FetchOutcome(
                payload=None,
                error=(
                    "source_unavailable stage=discord_fetch "
                    f"http_status={response.status_code} channel_id={channel_id} "
                    f"message_id={message_id}"
                ),
                http_status=response.status_code,
            )
        if response.status_code == 401:
            raise RuntimeError(
                "Discord authentication failed: stage=discord_fetch http_status=401 "
                f"channel_id={channel_id} message_id={message_id}"
            )
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            if attempt < 3:
                await asyncio.sleep(retry_delay(response, attempt))
                continue
        return FetchOutcome(
            payload=None,
            error=(
                "stage=discord_fetch "
                f"http_status={response.status_code} channel_id={channel_id} "
                f"message_id={message_id} response={response.text[:500]}"
            ),
            http_status=response.status_code,
        )

    raise RuntimeError(
        f"Discord retry loop ended unexpectedly: channel_id={channel_id}, message_id={message_id}"
    )


def asset_rows(payload: DiscordMessagePayload) -> list[tuple[str, str, str | None, str | None, str | None]]:
    rows: list[tuple[str, str, str | None, str | None, str | None]] = []
    for url in URL_RE.findall(payload.content):
        rows.append(("link", url.rstrip(".,);]）】"), None, None, None))
    for embed in payload.embeds:
        if embed.url:
            rows.append(("embed", embed.url, embed.title, embed.description, None))
        if embed.image:
            rows.append(("image", embed.image.url, embed.title, embed.description, None))
        if embed.thumbnail:
            rows.append(("image", embed.thumbnail.url, embed.title, embed.description, None))
    for attachment in payload.attachments:
        kind = "image" if (attachment.content_type or "").startswith("image/") else "attachment"
        rows.append(
            (kind, attachment.url, attachment.filename, attachment.description, attachment.content_type)
        )
    return list(dict.fromkeys(rows))


def save_success(db: Session, message: Message, payload: DiscordMessagePayload) -> None:
    message.channel_id = payload.channel_id
    message.content = payload.content
    message.source_created_at = payload.timestamp
    message.fetch_error = None
    for kind, url, title, description, mime_type in asset_rows(payload):
        asset = (
            db.query(SourceAsset)
            .filter(
                SourceAsset.message_id == message.message_id,
                SourceAsset.kind == kind,
                SourceAsset.url == url,
            )
            .first()
        )
        if asset is None:
            asset = SourceAsset(message_id=message.message_id, kind=kind, url=url)
            db.add(asset)
        asset.title = title
        asset.description = description
        asset.mime_type = mime_type
        identity = source_asset_identity(url)
        normalized_url = identity.normalized_url if identity is not None else url
        asset.source_service = (
            identity.source_service.value
            if identity is not None and identity.source_service is not None
            else None
        )
        asset.source_item_id = identity.source_item_id if identity is not None else None
        asset.normalized_url = normalized_url
        asset.content_fingerprint = source_asset_fingerprint(
            kind=kind,
            normalized_url=normalized_url,
            title=title,
            description=description,
            extracted_text=asset.extracted_text,
        )
        asset.fetch_status = FetchStatus.AVAILABLE.value
        asset.fetch_error = None
    for mention in message.mentions:
        if mention.difference_type == "source_unavailable":
            mention.difference_type = "source_restored"


def save_failure(db: Session, message: Message, outcome: FetchOutcome) -> None:
    message.fetch_error = outcome.error
    for mention in message.mentions:
        mention.review_status = ReviewStatus.PENDING.value
        if mention.difference_type is None:
            mention.difference_type = "source_unavailable"
        mention.extraction_error = outcome.error


def record_run(
    db: Session,
    message_id: str,
    outcome: FetchOutcome,
    started_at: float,
) -> None:
    db.add(
        ProcessingRun(
            message_id=message_id,
            stage="discord_restore",
            status=(
                ProcessingStatus.SUCCEEDED.value
                if outcome.payload is not None
                else ProcessingStatus.FAILED.value
            ),
            latency_ms=int((time.monotonic() - started_at) * 1000),
            error=outcome.error,
        )
    )


async def restore(args: RestoreArgs, token: str) -> RestoreCounts:
    init_db()
    db = SessionLocal()
    counts = RestoreCounts()
    try:
        query = db.query(Message).order_by(Message.message_id)
        if args.message_ids:
            query = query.filter(Message.message_id.in_(args.message_ids))
        elif args.limit is not None:
            query = query.limit(args.limit)
        messages = query.all()
        if args.message_ids:
            found_message_ids = {message.message_id for message in messages}
            missing_message_ids = [
                message_id
                for message_id in args.message_ids
                if message_id not in found_message_ids
            ]
            if missing_message_ids:
                raise RuntimeError(
                    "Requested messages are missing from the database: "
                    f"message_ids={','.join(missing_message_ids)}"
                )
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            for message in messages:
                channel_id = message.channel_id or args.channel_id
                if channel_id is None:
                    raise RuntimeError(
                        "Discord channel is missing: "
                        f"message_id={message.message_id}, set --channel-id or DISCORD_CHANNEL_ID"
                    )
                counts.requested += 1
                started_at = time.monotonic()
                outcome = await fetch_message(client, token, channel_id, message.message_id)
                if outcome.payload is not None:
                    counts.restored += 1
                elif outcome.http_status in {403, 404}:
                    counts.unavailable += 1
                else:
                    counts.failed += 1
                if args.apply:
                    if outcome.payload is not None:
                        save_success(db, message, outcome.payload)
                    else:
                        save_failure(db, message, outcome)
                    record_run(db, message.message_id, outcome, started_at)
                    db.commit()
        return counts
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    args = parse_args()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print(json.dumps({"status": "failed", "error": "DISCORD_TOKEN is not configured"}))
        return 2
    try:
        counts = asyncio.run(restore(args, token))
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {"status": "applied" if args.apply else "dry_run", **asdict(counts)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if counts.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
