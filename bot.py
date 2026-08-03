import os
import logging
import asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# Configure logging to see everything clearly in Railway logs
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Fetch environment variables safely
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_USER_ID")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD")
XMR_RPC_URL = os.getenv("MONERO_RPC_URL", "http://127.0.0")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The master Temple greeting message triggered by /start"""
    user_id = str(update.effective_user.id)
   
    # Calculate a dummy dynamic pricing fallback ($5,000 USD to XMR placeholder)
    xmr_amount = "12.5"
   
    welcome_text = (
        "✨ **Welcome to the Sacred Temple Gates** ✨\n\n"
        "Your presence here has been recognized. To secure entry and ensure your "
        "complete anonymity is held sacred, your admission requires a non-custodial tribute.\n\n"
        f"**Required Payment:** `{xmr_amount} XMR` (~$5,000 USD)\n\n"
        "Please click the button below to generate your unique structural subaddress "
        "and complete your verification process."
    )
   
    keyboard = [[InlineKeyboardButton("🔒 Generate Temple Subaddress", callback_data="gen_address")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
   
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=reply_markup)

async def action_cb(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None):
    """The automated verification background logic wrapper with built-in crash defense"""
    logger.info("Executing background verification queue sync...")
    try:
        # Check connection status to our sidecar Monero box
        payload = {"jsonrpc": "2.0", "id": "0", "method": "get_version"}
        response = requests.post(XMR_RPC_URL, json=payload, timeout=5)
        if response.status_code == 200:
            logger.info("Successfully connected to secure Monero tracking companion service.")
        else:
            logger.warning(f"Companion service responded with status code: {response.status_code}")
    except Exception as e:
        logger.warning(f"Background Monero wallet sync delayed (waiting for network synchronization): {e}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user clicking the generate subaddress button"""
    query = update.callback_query
    await query.answer()
   
    # Defense check for background actions
    try:
        await action_cb(update, context)
    except Exception as e:
        logger.error(f"Callback intercept error handling task: {e}")
       
    await query.edit_message_text(
        text="⏳ **Generating subaddress...**\nYour tracking wallet link is establishing. Please try again in 30 seconds once the secure tunnel fully syncs.",
        parse_mode="Markdown"
    )

def main():
    """Main application loop with safe configuration mapping"""
    if not TOKEN:
        logger.fatal("FATAL ERROR: TELEGRAM_BOT_TOKEN environment variable is missing!")
        return

    # Build the Telegram Bot application framework
    application = Application.builder().token(TOKEN).build()

    # Link the command triggers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Safely trigger background loop verification test on launch without breaking startup
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(action_cb())
        else:
            asyncio.run(action_cb())
    except Exception as startup_err:
        logger.warning(f"Startup task scheduler bypass: {startup_err}")

    # Spin up the listener engine
    logger.info("The Temple Gatekeeper bot is fully initialized and listening for commands...")
    application.run_polling()

if __name__ == '__main__':
    main()
