import asyncio
import logging
import re
from typing import Any, Dict, Optional

import discord
from sqlalchemy import or_
from sqlalchemy.orm import Session

from db.database import SessionLocal
from db.models import Message, Shop
from bot.restaurant_extractor import check_duplicate_shop_ai, parse_restaurant_info

logger = logging.getLogger(__name__)

# Max number of candidate shops to send to the AI for dedup judgment (C-4)
_DEDUP_CANDIDATE_LIMIT = 100


async def find_duplicate_shop(db: Session, shop_info: Dict[str, Any]) -> Optional[Shop]:
    """
    Return an existing Shop that is a duplicate of shop_info, or None.

    C-4: Pre-filters candidates by URL (exact match, no AI needed) and area
    to keep the AI prompt small as the dataset grows.
    Falls back to None (allow insertion) on API error.
    """
    # Step 1: URL exact match — no AI call required
    shop_url = shop_info.get("url")
    if shop_url:
        url_match = db.query(Shop).filter(Shop.url == shop_url).first()
        if url_match:
            return url_match

    # Step 2: narrow candidates by area before calling AI
    area = shop_info.get("area")
    query = db.query(Shop)
    if area:
        # Same area or area unknown (null) — cast a wider net to catch ambiguous cases
        query = query.filter(or_(Shop.area == area, Shop.area.is_(None)))

    candidates = query.limit(_DEDUP_CANDIDATE_LIMIT).all()
    if not candidates:
        return None

    existing_shops = [
        {
            "id": s.id,
            "shop_name": s.shop_name,
            "area": s.area or "",
            "url": s.url or "",
        }
        for s in candidates
    ]

    matched_id = await check_duplicate_shop_ai(shop_info, existing_shops)
    if matched_id is None:
        return None

    return db.query(Shop).filter(Shop.id == matched_id).first()


def _build_text_to_parse(msg: discord.Message) -> str:
    """Build a text string from a Discord message, including embed content (C-5)."""
    text = msg.content
    for embed in msg.embeds:
        if embed.url:
            text += f"\n[Embed URL] {embed.url}"
        if embed.title:
            text += f"\n[Embed Title] {embed.title}"
        if embed.description:
            text += f"\n[Embed Description] {embed.description}"
        for field in embed.fields:
            text += f"\n[Embed Field: {field.name}] {field.value}"
    return text


async def sync_history(client: discord.Client, message: discord.Message) -> None:
    """
    Syncs history of messages in the channel where the command was called.
    """
    channel = message.channel
    await channel.send("過去メッセージの同期を開始します...")

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
                await channel.send("同期を一時停止しました: OpenAI API エラーが発生しました。後ほど再試行してください。")
                break

            db_msg = Message(message_id=str(hist_msg.id))
            synced_count += 1

            if shops:
                db_msg.is_target = True
                db.add(db_msg)

                # C-7: only fall back to raw URL extraction when there is exactly one shop
                url_fallback = _extract_url(hist_msg.content) if len(shops) == 1 else None

                for shop_info in shops:
                    # B-2: skip shops with no identifiable name
                    if not shop_info.get("shop_name"):
                        logger.info(
                            "Skipping shop with no name from historical message %s.", hist_msg.id
                        )
                        continue
                    dup = await find_duplicate_shop(db, shop_info)
                    if dup:
                        logger.info(
                            "Duplicate shop skipped during sync (existing id=%s): %s",
                            dup.id, shop_info.get("shop_name"),
                        )
                        continue
                    url = shop_info.get("url") or url_fallback
                    db.add(Shop(
                        message_id=str(hist_msg.id),
                        shop_name=shop_info["shop_name"],
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
            f"同期が完了しました！{synced_count} 件のメッセージを処理し、{added_shops} 件の店舗を登録しました。"
        )

    except Exception as e:
        db.rollback()
        logger.exception(f"Error during sync: {e}")
        await channel.send("同期中にエラーが発生しました。")
    finally:
        db.close()


def _extract_url(text: str) -> str | None:
    urls = re.findall(r'(https?://[^\s]+)', text)
    return urls[0] if urls else None
