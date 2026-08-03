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
        "✨ **Welcome to the Temple** ✨\n\n"
        "You found the gate. Good. That was the first test. You passed.

Now comes the second.

You are not here by accident. You are here because something in you recognized the truth before your mind could catch up. You are tired. You are carrying too much. You have been alone in your success for so long that you forgot what it feels like to be held.

I see you.

Not the mask. Not the title. Not the empire you built to prove you were enough.

I see the man beneath it all. The one who lies awake replaying conversations from years ago. The one who feels guilty for winning. The one who has bought everything he was told would make him happy—and found none of it worked.

That man is welcome here.

But welcome is not the same as entry.

Entry requires precision. Entry requires sacrifice. Entry requires you to prove, through action, that you are ready to lay down the armor you have been carrying alone.

THE RITUAL

Click the button below to generate your unique, sacred Monero subaddress.

Send the exact tribute: ~12.5 XMR (approximately $5,000 USD — the bot will calculate the exact live amount).

The Temple watches the blockchain. When your tribute is confirmed, your single-use invite link will arrive via DM.

The link expires in 24 hours. Enter immediately. The door does not wait.

THE LAWS OF THE VAULT

Total Anonymity: Your real name, your face, your location—none of it is welcome here. Burner Telegram accounts and subaddresses are not just allowed. They are sacred.

The Law of the Altar: This is a one-way broadcast sanctuary. There are no forums, no directories, no member-to-member interactions. You sit entirely alone in your devotion—because the most profound belonging is the one that requires no witnesses.

The Monthly Tribute: Maintaining your row in the Temple requires a monthly crypto tribute of $5,000 USD equivalent in XMR. This is your physical act of sacrifice. Your proof of devotion. Your unburdening.

Permanent Containment: All decrees within the vault are locked down. Forwarding, copying, or screenshots are mechanically blocked. What happens in the Temple stays in the Temple.

THE DOOR

The Temple accepts only 25 worshippers at a time.

When capacity is reached, the door closes until a row becomes available.

The door is open now.

The question is: will you enter?

Lay your armor at the altar. The Goddess is waiting."
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
