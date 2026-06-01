import os
import sys
import logging
import asyncio
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder

from database import init_db
import handlers

# 1. Set up standard Python logging output to print cleanly to stdout
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# 2. Load environment variables from .env file
load_dotenv()

# 4. Define the root async function main()
async def main() -> None:
    """
    Root async entry point for the application.
    Initializes the database and starts the Telegram bot.
    """
    logger.info("Initializing database schema...")
    # Execute await init_db() to automatically configure the PostgreSQL tables
    await init_db()
    
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("Critical Error: BOT_TOKEN environment variable is missing.")
        sys.exit(1)
        
    logger.info("Building Telegram bot application...")
    # 5. Initialize ApplicationBuilder() and configure the bot token
    application = ApplicationBuilder().token(bot_token).build()
    
    logger.info("Registering modular handlers...")
    # Register all the modular handlers from handlers.py
    application.add_handler(handlers.get_core_handler())
    
    logger.info("Starting bot polling...")
    # Start up the system with application.run_polling()
    application.run_polling()

if __name__ == "__main__":
    try:
        # Execute the async main function
        asyncio.run(main())
    except RuntimeError as e:
        # Production fallback: python-telegram-bot v20+ run_polling() manages its own event loop.
        # If asyncio.run(main()) conflicts with run_polling(), we gracefully handle the initialization 
        # and start polling synchronously to ensure container stability on Render.
        if "Event loop is closed" in str(e) or "cannot be called from a running event loop" in str(e):
            logger.warning("Event loop conflict detected. Falling back to synchronous polling execution.")
            
            # Run DB init in the current loop
            loop = asyncio.get_event_loop()
            loop.run_until_complete(init_db())
            
            # Build and run the application synchronously
            app = ApplicationBuilder().token(os.getenv("BOT_TOKEN")).build()
            app.add_handler(handlers.get_core_handler())
            app.run_polling()
        else:
            raise
    except KeyboardInterrupt:
        logger.info("Bot shutdown signal received.")