from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import discord
from discord.ext import tasks
from dotenv import load_dotenv

from bot.restaurant_extractor import ExtractionError, preflight_models
from bot.sync_logic import build_message_envelope, sync_history
from bot.weekly_sync import JST, WEEKLY_SYNC_TIME, WeeklySyncSettings, next_weekly_run, run_configured_sync
from db.database import SessionLocal, init_db
from db.models import Message, ProcessingStatus
from services.identification_pipeline import process_message
from services.security_config import validate_bot_security


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
weekly_settings: WeeklySyncSettings = WeeklySyncSettings.from_env()


@tasks.loop(time=WEEKLY_SYNC_TIME, reconnect=False)
async def weekly_history_sync() -> None:
    if datetime.now(JST).weekday() != 0:
        return
    try:
        await run_configured_sync(client, weekly_settings)
    except Exception:
        logger.exception("Weekly channel sync failed: channel_id=%s", weekly_settings.channel_id)



@client.event
async def on_ready() -> None:
    validate_bot_security()
    init_db()
    try:
        await preflight_models()
    except ExtractionError as exc:
        logger.critical("OpenAI model preflight failed: %s", exc)
        await client.close()
        return
    logger.info("Bot ready: user=%s", client.user)
    if weekly_settings.enabled and not weekly_history_sync.is_running():
        weekly_history_sync.start()
        logger.info(
            "Weekly sync scheduled: channel_id=%s next_run=%s",
            weekly_settings.channel_id, next_weekly_run(datetime.now(timezone.utc)).isoformat(),
        )


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author == client.user or client.user not in message.mentions:
        return
    if not ADMIN_USER_ID or str(message.author.id) != ADMIN_USER_ID:
        logger.warning(
            "Unauthorized bot command: author_id=%s message_id=%s",
            message.author.id,
            message.id,
        )
        return

    content = message.content.replace(f"<@{client.user.id}>", "").strip()
    if content.lower() == "sync":
        await sync_history(client, message)
        return

    await message.add_reaction("⏳")
    db = SessionLocal()
    try:
        existing = db.query(Message).filter(Message.message_id == str(message.id)).first()
        if existing and existing.processing_status in {
            ProcessingStatus.SUCCEEDED.value,
            ProcessingStatus.IGNORED.value,
        }:
            await message.add_reaction("👀")
            return

        result = await process_message(
            db,
            build_message_envelope(message),
            allow_source_discovery=True,
        )
        if result.ignored:
            await message.add_reaction("⏭️")
            return
        lines = [f"🍽️ {len(result.shop_ids)} 件を処理しました。"]
        if result.pending_mention_ids:
            lines.append(f"🔎 データ確認: {len(result.pending_mention_ids)} 件")
        else:
            lines.append("✅ すべて強い根拠で確定しました。")
        await message.reply("\n".join(lines))
        await message.add_reaction("✅" if not result.pending_mention_ids else "🔎")
    except Exception as exc:
        logger.exception("Message processing failed: message_id=%s error=%s", message.id, exc)
        await message.add_reaction("❌")
        await message.reply(
            f"処理に失敗しました。message_id={message.id}。同じ投稿でもう一度実行できます。"
        )
    finally:
        db.close()
        try:
            await message.remove_reaction("⏳", client.user)
        except discord.HTTPException as exc:
            logger.warning(
                "Failed to remove progress reaction: message_id=%s status=%s",
                message.id,
                exc.status,
            )


if __name__ == "__main__":
    validate_bot_security()
    if os.getenv("APP_READ_ONLY", "false").lower() == "true":
        raise RuntimeError("Bot startup refused because APP_READ_ONLY=true")
    client.run(DISCORD_TOKEN)
