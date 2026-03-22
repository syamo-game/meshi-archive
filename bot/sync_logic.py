import asyncio
import logging
import re
from typing import Any, Dict, Optional

import discord
from sqlalchemy import or_
from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import Message, Shop
from bot.restaurant_extractor import parse_restaurant_info

logger = logging.getLogger(__name__)


def find_duplicate_shop(db: Session, shop_info: Dict[str, Any]) -> Optional[Shop]:
    """
    Return an existing Shop that is a duplicate of shop_info, or None.

    Priority:
      1. Same URL (if present) — URLs are definitive identifiers.
      2. Same shop_name + area, including prefix matches for branch names
         (e.g. "天よし" matches "天よし 浅草本店" and vice versa).
         Uses space as a branch separator to avoid false positives like
         "天よし" matching "天よし食堂".
    """
    url = (shop_info.get("url") or "").strip()
    if url:
        existing = db.query(Shop).filter(Shop.url == url).first()
        if existing:
            return existing

    name = (shop_info.get("shop_name") or "").strip()
    area = (shop_info.get("area") or "").strip()
    if not name or not area:
        return None

    # Match exact name, or existing shop whose name starts with "name "
    # (branch suffix pattern), or new name starts with existing name + " ".
    existing = (
        db.query(Shop)
        .filter(
            Shop.area.ilike(area),
            or_(
                Shop.shop_name.ilike(name),           # exact
                Shop.shop_name.ilike(f"{name} %"),    # existing has branch suffix
                Shop.shop_name.ilike(f"{name}\u3000%"), # full-width space variant
            ),
        )
        .first()
    )
    if existing:
        return existing

    # Also check if new name is an extension of a shorter existing name
    # (e.g. inserting "天よし 浅草本店" when "天よし" already exists).
    # Fetch candidates with same area and check Python-side to avoid
    # complex SQL substring logic.
    name_lower = name.lower()
    candidates = db.query(Shop).filter(Shop.area.ilike(area)).all()
    for candidate in candidates:
        base = candidate.shop_name.strip().lower()
        if name_lower.startswith(base + " ") or name_lower.startswith(base + "\u3000"):
            return candidate

    return None


def _build_text_to_parse(msg: discord.Message) -> str:
    text = msg.content
    for embed in msg.embeds:
        if embed.title:
            text += f"\n[Embed Title] {embed.title}"
        if embed.description:
            text += f"\n[Embed Description] {embed.description}"
    return text


async def sync_history(client: discord.Client, message: discord.Message) -> None:
    """
    Syncs history of messages in the channel where the command was called.
    """
    channel = message.channel
    await channel.send("Starting historical sync...")

    db = SessionLocal()
    try:
        # Get the latest message processed
        last_msg = db.query(Message).order_by(Message.message_id.desc()).first()
        after_date_or_id = None

        if last_msg:
            # We must fetch the actual discord.Object for 'after' parameter
            after_date_or_id = discord.Object(id=int(last_msg.message_id))
            logger.info(f"Syncing after message ID: {last_msg.message_id}")
        else:
            logger.info("No prior messages found. Fetching recent history.")

        synced_count = 0
        added_shops = 0

        # Iterate over history
        async for hist_msg in channel.history(limit=500, after=after_date_or_id, oldest_first=True):
            # Skip own messages or empty messages
            if hist_msg.author == client.user or not hist_msg.content.strip():
                continue

            # Check if this msg already exists in DB
            exists = db.query(Message).filter_by(message_id=str(hist_msg.id)).first()
            if exists:
                continue

            # Process with AI
            logger.info(f"Processing historical message: {hist_msg.id}")

            text_to_parse = _build_text_to_parse(hist_msg)

            shops = await parse_restaurant_info(text_to_parse)
            logger.info("AI parse result for %s: %s", hist_msg.id, shops)

            await asyncio.sleep(4)

            if shops is None:
                # API error — stop syncing to avoid marking messages as skipped
                await channel.send("Sync paused: OpenAI API error. Please try again later.")
                break

            db_msg = Message(message_id=str(hist_msg.id))
            synced_count += 1

            if shops:
                db_msg.is_target = True
                db.add(db_msg)
                for shop_info in shops:
                    dup = find_duplicate_shop(db, shop_info)
                    if dup:
                        logger.info(
                            "Duplicate shop skipped during sync (existing id=%s): %s",
                            dup.id, shop_info.get("shop_name"),
                        )
                        continue
                    url = shop_info.get("url") or _extract_url(hist_msg.content)
                    db.add(Shop(
                        message_id=str(hist_msg.id),
                        shop_name=shop_info.get("shop_name") or "Unknown",
                        area=shop_info.get("area"),
                        category=shop_info.get("category"),
                        url=url,
                        is_visited=False,
                    ))
                    added_shops += 1
            else:
                db_msg.is_target = False
                db.add(db_msg)

            db.commit()

        await channel.send(
            f"Sync complete! Processed {synced_count} messages and registered {added_shops} new shops."
        )

    except Exception as e:
        db.rollback()
        logger.exception(f"Error during sync: {e}")
        await channel.send("An error occurred during synchronization.")
    finally:
        db.close()


def _extract_url(text: str) -> str | None:
    urls = re.findall(r'(https?://[^\s]+)', text)
    return urls[0] if urls else None
