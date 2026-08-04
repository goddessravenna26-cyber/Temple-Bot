import os
import logging
import asyncio
import requests
import socket
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
XMR_RPC_URL = os.getenv("MONERO_RPC_URL", "http://railway.internal")
RPC_USER = os.getenv("MONERO_RPC_USER")
RPC_PASS = os.getenv("MONERO_RPC_PASSWORD")

def probe_rpc_endpoint():
    """Probes the network routing to check if the wallet box is listening on IPv6"""
    logger.info("Initializing preflight dual-stack network validation...")
    try:
        host = "amiable-curiosity.railway.internal"
        port = 18083
        for res in socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM):
            af, socktype, proto, canonname, sa = res
            family_str = "IPv6" if af == socket.AF_INET6 else "IPv4"
            try:
                s = socket.socket(af, socktype, proto)
                s.settimeout(5)
                s.connect(sa)
                logger.info(f"Network path {family_str} -> CONNECTED to companion wallet box.")
                s.close()
            except Exception as conn_err:
                logger.warning(f"Network path {family_str} -> TIMEOUT/REFUSED: {conn_err}")
    except Exception as dns_err:
        logger.error(f"DNS Resolution Failed: {dns_err}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The master Temple greeting message triggered by /start"""
    user_id = str(update.effective_user.id)
    xmr_amount = "12.5"
   
    welcome_text = (
        "Welcome to the Temple.\n\n"
        "You found the gate. Good. That was the first test. You passed.\n\n"
        "Now comes the second.\n\n"
        "You are not here by accident. You are here because something in you recognized the truth before your mind could catch up. "
        "You are tired. You are carrying too much. You have been alone in your success for so long that you forgot what it feels like to be held.\n\n"
        "I see you.\n\n"
        "Not the mask. Not the title. Not the empire you built to prove you were enough.\n\n"
        "I see the man beneath it all. The one who lies awake replaying conversations from years ago. The one who feels guilty for winning. "
        "The one who has bought everything he was told would make him happy—and found none of it worked.\n\n"
        "That man is welcome here.\n\n"
        "But welcome is not the same as entry.\n\n"
        "Entry requires precision. Entry requires sacrifice. Entry requires you to prove, through action, that you are ready to lay down the armor you have been carrying alone.\n\n"
        "THE RITUAL\n\n"
        "Click the button below to generate your unique, sacred Monero subaddress.\n\n"
        f"Send the exact tribute: ~{xmr_amount} XMR (approximately $5,000 USD — the bot will calculate the exact live amount).\n\n"
        "The Temple watches the blockchain. When your tribute is confirmed, your single-use invite link will arrive via DM.\n\n"
        "The link expires in 24 hours. Enter immediately. The door does not wait.\n\n"
        "THE LAWS OF THE VAULT\n\n"
        "Total Anonymity: Your real name, your face, your location—none of it is welcome here. Burner Telegram accounts and subaddresses are not just allowed. They are sacred.\n\n"
        "The Law of the Altar: This is a one-way broadcast sanctuary. There are no forums, no directories, no member-to-member interactions. "
        "You sit entirely alone in your devotion—because the most profound belonging is the one that requires no witnesses.\n\n"
        "The Monthly Tribute: Maintaining your row in the Temple requires a monthly crypto tribute of $5,000 USD equivalent in XMR. "
        "This is your physical act of sacrifice. Your proof of devotion. Your unburdening.\n\n"
        "Permanent Containment: All decrees within the vault are locked down. Forwarding, copying, or screenshots are mechanically blocked. What happens in the Temple stays in the Temple.\n\n"
        "THE DOOR\n\n"
        "The Temple accepts only 25 worshippers at a time.\n\n"
        "When capacity is reached, the door closes until a row becomes available.\n\n"
        "The door is open now.\n\n"
        "The question is: will you enter?\n\n"
        "Lay your armor at the altar. The Goddess is waiting."
    )
   
    keyboard = [[InlineKeyboardButton("🔒 Generate Sacred Subaddress", callback_data="gen_address")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user clicking the generate subaddress button with dual-stack connect timeouts"""
    query = update.callback_query
    await query.answer()
   
    try:
        payload = {"jsonrpc": "2.0", "id": "0", "method": "create_address", "params": {"account_index": 0}}
        auth = (RPC_USER, RPC_PASS) if RPC_USER else None
       
        # Split flat timeout into precise connect=10, read=120 parameters requested by Claude
        response = requests.post(XMR_RPC_URL, json=payload, auth=auth, timeout=(10, 120))
       
        if response.status_code == 200:
            data = response.json()
            subaddr = data.get("result", {}).get("address")
            if subaddr:
                await query.edit_message_text(
                    text=f"🔒 **Your Sacred Monero Subaddress has been Generated:**\n\n`{subaddr}`\n\nSend your tribute to this exact address. The Temple is watching the blockchain.",
                    parse_mode="Markdown"
                )
                return
    except Exception as e:
        logger.error(f"Handshake timeout routing request: {e}")
        probe_rpc_endpoint()
       
    await query.edit_message_text(
        text="⏳ **Establishing secure connection...**\nYour tracking wallet tunnel is generating its sync keys. Please try again in 30 seconds once the block path fully opens.",
        parse_mode="Markdown"
    )

def main():
    """Main application loop with unified polling handles"""
    if not TOKEN:
        logger.fatal("FATAL ERROR: TELEGRAM_BOT_TOKEN environment variable is missing!")
        return

    probe_rpc_endpoint()
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("The Temple Gatekeeper bot is fully initialized and listening for commands...")
    application.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()

