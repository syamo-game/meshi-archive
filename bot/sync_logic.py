import discord
from db.database import SessionLocal
from db.models import Message, Shop
from bot.restaurant_extractor import parse_restaurant_info
import logging

logger = logging.getLogger(__name__)

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
            
            text_to_parse = hist_msg.content
            for embed in hist_msg.embeds:
                if embed.title:
                    text_to_parse += f"\n[Embed Title] {embed.title}"
                if embed.description:
                    text_to_parse += f"\n[Embed Description] {embed.description}"
                    
            import asyncio
            result = await parse_restaurant_info(text_to_parse)
            logger.info(f"AI Parse Result for {hist_msg.id}: {result}")
            await asyncio.sleep(4)
            
            if result is None:
                # API error or parse failure. Stop syncing to prevent marking items as skipped.
                await channel.send("🛑 Sync paused: Gemini API error (Rate limit or API issue). Please try again later.")
                break

            db_msg = Message(message_id=str(hist_msg.id))
            synced_count += 1
            
            if not result.get("ignore", True):
                db_msg.is_target = True
                new_shop = Shop(
                    message_id=str(hist_msg.id),
                    shop_name=result.get("shop_name") or "Unknown",
                    area=result.get("area"),
                    category=result.get("category"),
                    url=result.get("url") or _extract_url(hist_msg.content),
                    is_visited=False
                )
                db.add(new_shop)
                added_shops += 1
            else:
                db_msg.is_target = False
                
            db.add(db_msg)
            db.commit() # Commit periodically or per message to save progress safely

        await channel.send(f"Sync complete! Processed {synced_count} messages and registered {added_shops} new shops.")

    except Exception as e:
        logger.error(f"Error during sync: {e}")
        await channel.send("An error occurred during synchronization.")
    finally:
        db.close()

def _extract_url(text: str) -> str | None:
    # A simple fallback URL extraction if AI misses it
    import re
    urls = re.findall(r'(https?://[^\s]+)', text)
    return urls[0] if urls else None

