from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
import json
import logging
import os
import re

import discord

from bot.restaurant_extractor import preflight_models
from bot.sync_logic import SyncSummary, WeeklyAttempt, sync_channel_history
from db.database import init_db
from services.security_config import validate_bot_security


logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9), name="Asia/Tokyo")
WEEKLY_SYNC_TIME = time(hour=4, tzinfo=JST)


@dataclass(frozen=True)
class WeeklySyncSettings:
    enabled: bool
    channel_id: int | None

    @classmethod
    def from_env(cls, *, require_channel: bool = False) -> WeeklySyncSettings:
        enabled_value: str = os.getenv("WEEKLY_SYNC_ENABLED", "false").strip().lower()
        if enabled_value not in {"true", "false"}:
            raise RuntimeError("WEEKLY_SYNC_ENABLED must be true or false")
        channel: str = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        if (channel or enabled_value == "true" or require_channel) and not re.fullmatch(r"[0-9]{17,20}", channel):
            raise RuntimeError("DISCORD_CHANNEL_ID must configure a channel ID for weekly sync")
        return cls(enabled=enabled_value == "true", channel_id=int(channel) if channel else None)


def weekly_slot(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("Weekly sync time must have a timezone")
    local: datetime = now.astimezone(JST)
    slot: datetime = datetime.combine(local.date() - timedelta(days=local.weekday()), WEEKLY_SYNC_TIME)
    if slot > local:
        slot -= timedelta(days=7)
    return slot.astimezone(timezone.utc)


def next_weekly_run(now: datetime) -> datetime:
    return (weekly_slot(now) + timedelta(days=7)).astimezone(JST)


async def run_configured_sync(
    client: discord.Client,
    settings: WeeklySyncSettings,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> SyncSummary | None:
    if not force and not settings.enabled:
        return None
    if settings.channel_id is None:
        raise RuntimeError("Weekly sync requires DISCORD_CHANNEL_ID")
    if os.getenv("APP_READ_ONLY", "false").lower() == "true":
        raise RuntimeError("Channel sync refused because APP_READ_ONLY=true")
    started: datetime = now or datetime.now(timezone.utc)
    channel = await client.fetch_channel(settings.channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise RuntimeError(f"Weekly sync requires a guild text channel or thread: channel_id={settings.channel_id}")
    result = await sync_channel_history(
        client, channel, weekly=WeeklyAttempt(weekly_slot(started), started, force),
    )
    if result is not None:
        logger.info("Weekly channel sync completed: channel_id=%s result=%s", settings.channel_id, asdict(result))
    return result


class SyncOnceClient(discord.Client):
    def __init__(self, settings: WeeklySyncSettings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings: WeeklySyncSettings = settings
        self.started: bool = False
        self.result: SyncSummary | None = None
        self.failure: Exception | None = None

    async def on_ready(self) -> None:
        if self.started:
            return
        self.started = True
        try:
            await preflight_models()
            self.result = await run_configured_sync(self, self.settings, force=True)
            if self.result is None:
                raise RuntimeError("Initial channel sync returned no result")
        except Exception as exc:
            self.failure = exc
            logger.exception("Initial channel sync failed: channel_id=%s", self.settings.channel_id)
        finally:
            await self.close()


async def run_once() -> SyncSummary:
    validate_bot_security()
    if os.getenv("APP_READ_ONLY", "false").lower() == "true":
        raise RuntimeError("Channel sync refused because APP_READ_ONLY=true")
    settings = WeeklySyncSettings.from_env(require_channel=True)
    init_db()
    client = SyncOnceClient(settings)
    async with client:
        await client.start(os.environ["DISCORD_TOKEN"])
    if client.failure is not None:
        raise client.failure
    if client.result is None:
        raise RuntimeError("Discord closed before the initial sync completed")
    return client.result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    summary = asyncio.run(run_once())
    print(json.dumps(asdict(summary), ensure_ascii=False))
