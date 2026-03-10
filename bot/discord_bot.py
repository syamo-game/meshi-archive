import os
import discord
import logging
from dotenv import load_dotenv

from db.database import init_db, SessionLocal
from db.models import Message, Shop
from bot.restaurant_extractor import parse_restaurant_info
from bot.sync_logic import sync_history

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

            text_to_parse = message.content
            for embed in message.embeds:
                if embed.title:
                    text_to_parse += f"\n[Embed Title] {embed.title}"
                if embed.description:
                    text_to_parse += f"\n[Embed Description] {embed.description}"

            result = await parse_restaurant_info(text_to_parse)
            
            db_msg = Message(message_id=str(message.id))
            
            if result and not result.get("ignore", True):
                db_msg.is_target = True
                
                # Try to get URL from AI result, fallback to simple parse, fallback to None
                from bot.sync_logic import _extract_url
                url = result.get("url") or _extract_url(message.content)

                new_shop = Shop(
                    message_id=str(message.id),
                    shop_name=result.get("shop_name", "Unknown"),
                    area=result.get("area"),
                    category=result.get("category"),
                    url=url,
                    is_visited=False
                )
                db.add(new_shop)
                db.add(db_msg)
                db.commit()
                
                area_text = new_shop.area if new_shop.area else "Unknown Area"
                cat_text = new_shop.category if new_shop.category else "Unknown Category"
                await message.reply(f"🍽️ Registered 【{new_shop.shop_name}】!\n📍 {area_text} | 🏷️ {cat_text}")
                await message.add_reaction("✅")
            elif result and result.get("ignore"):
                db_msg.is_target = False
                db.add(db_msg)
                db.commit()
                await message.add_reaction("⏭️") # Skipped
            else:
                db_msg.is_target = False
                db.add(db_msg)
                db.commit()
                await message.add_reaction("❌") # Error/Failed parsing
                
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
