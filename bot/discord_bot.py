import os
import discord
import logging
from dotenv import load_dotenv

from db.database import init_db, SessionLocal
from db.models import Message, Shop
from bot.restaurant_extractor import parse_restaurant_info
from bot.sync_logic import find_duplicate_shop, sync_history, _extract_url, _build_text_to_parse

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load ENVs
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

# Setup Discord intents
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    logger.info(f'Logged in as {client.user}!')
    init_db()  # Ensure DB and tables are created
    logger.info("Database initialized.")

@client.event
async def on_message(message: discord.Message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Check if the bot is mentioned
    if client.user in message.mentions:
        # Security: Only allow ADMIN_USER_ID to trigger the bot
        if ADMIN_USER_ID and str(message.author.id) != ADMIN_USER_ID:
            logger.warning(f"Unauthorized access attempt by {message.author.name} (ID: {message.author.id})")
            return
            
        content = message.content.replace(f'<@{client.user.id}>', '').strip()
        
        # Check command
        if content.lower() == "sync":
            await sync_history(client, message)
            return
            
        # Real-time processing
        await message.add_reaction("⏳") # Add hourglass reaction while processing
        
        db = SessionLocal()
        try:
            # Check if already processed
            existing_msg = db.query(Message).filter_by(message_id=str(message.id)).first()
            if existing_msg:
                logger.info(f"Message {message.id} already processed.")
                await message.remove_reaction("⏳", client.user)
                return

            shops = await parse_restaurant_info(_build_text_to_parse(message))

            db_msg = Message(message_id=str(message.id))

            if shops is None:
                # API error — do not mark as processed so it can be retried
                db_msg.is_target = False
                db.add(db_msg)
                db.commit()
                await message.add_reaction("❌")
            elif len(shops) == 0:
                # Not a restaurant message
                db_msg.is_target = False
                db.add(db_msg)
                db.commit()
                await message.add_reaction("⏭️")
            else:
                db_msg.is_target = True
                db.add(db_msg)
                added: list[Shop] = []
                skipped: list[str] = []
                for shop_info in shops:
                    dup = find_duplicate_shop(db, shop_info)
                    if dup:
                        logger.info(
                            "Duplicate shop skipped (existing id=%s): %s",
                            dup.id, shop_info.get("shop_name"),
                        )
                        skipped.append(dup.shop_name)
                        continue
                    url = shop_info.get("url") or _extract_url(message.content)
                    new_shop = Shop(
                        message_id=str(message.id),
                        shop_name=shop_info.get("shop_name") or "Unknown",
                        area=shop_info.get("area"),
                        category=shop_info.get("category"),
                        url=url,
                        is_visited=False,
                    )
                    db.add(new_shop)
                    added.append(new_shop)
                db.commit()

                if not added and skipped:
                    await message.reply(
                        f"👀 Already registered: {', '.join(f'【{n}】' for n in skipped)}"
                    )
                    await message.add_reaction("👀")
                else:
                    lines = [f"🍽️ Registered {len(added)} shop(s)!"]
                    for s in added:
                        area_text = s.area or "Unknown Area"
                        cat_text = s.category or "Unknown Category"
                        lines.append(f"  【{s.shop_name}】 📍 {area_text} | 🏷️ {cat_text}")
                    if skipped:
                        lines.append(f"  ⏭️ Skipped (duplicate): {', '.join(f'【{n}】' for n in skipped)}")
                    await message.reply("\n".join(lines))
                    await message.add_reaction("✅")
                
        except Exception as e:
            logger.error(f"Error handling message {message.id}: {e}")
            await message.remove_reaction("⏳", client.user)
            await message.add_reaction("⚠️")
        finally:
            db.close()
            await message.remove_reaction("⏳", client.user)

if __name__ == "__main__":
    if DISCORD_TOKEN:
        client.run(DISCORD_TOKEN)
    else:
        logger.error("DISCORD_TOKEN not supplied.")
