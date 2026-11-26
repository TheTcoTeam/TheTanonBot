"""Telegram Inbox Bot: presents an anonymous-style inbox to senders while showing sender identity to the owner."""

from __future__ import annotations

import html
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Message, Update, User
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db import BotDatabase

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

db = BotDatabase(BASE_DIR / "inbox_bot.db")


def get_owner_id() -> int | None:
    """Parse OWNER_CHAT_ID from environment, returning None if unset or invalid."""
    raw = os.getenv("OWNER_CHAT_ID", "").strip()
    if not raw or raw == "123456789":
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("OWNER_CHAT_ID is not a valid integer: %r", raw)
        return None


def is_owner(user: User | None) -> bool:
    """Return True if the Telegram user matches OWNER_CHAT_ID."""
    owner_id = get_owner_id()
    return user is not None and owner_id is not None and user.id == owner_id


def format_sender_header(user: User) -> str:
    """Build an HTML-formatted sender identity block for the bot owner."""
    full_name = html.escape(user.full_name or "Unknown")
    username = f"@{html.escape(user.username)}" if user.username else "<i>No username</i>"
    profile_link = f'<a href="tg://user?id={user.id}">{full_name}</a>'
    return (
        f"📩 <b>New message</b>\n"
        f"👤 <b>From:</b> {profile_link}\n"
        f"🔗 <b>Username:</b> {username}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    if is_owner(user):
        bot_username = context.bot.username or "your_bot"
        await message.reply_text(
            "👑 <b>Owner mode active</b>\n\n"
            f"Share your link: https://t.me/{html.escape(bot_username)}\n"
            "Incoming messages will appear here with the sender's name, username, and ID.\n\n"
            "<b>Commands:</b>\n"
            "• <code>/stats</code> — view message counts\n"
            "• <code>/block &lt;user_id&gt;</code> — block a sender\n"
            "• <code>/unblock &lt;user_id&gt;</code> — unblock a sender\n"
            "• Reply to any forwarded message to answer the sender.",
            parse_mode=ParseMode.HTML,
        )
        return

    await message.reply_text(
        "👋 Send me any message (text, photo, voice, sticker, etc.) and I'll deliver it!"
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the caller's numeric Telegram user ID to help with setup."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    await message.reply_text(
        f"🆔 Your Telegram user ID is: <code>{user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message and block statistics (owner only)."""
    message = update.effective_message
    if not message or not is_owner(update.effective_user):
        return

    total, unique_senders, blocked = db.get_stats()
    await message.reply_text(
        f"📊 <b>Bot Stats</b>\n"
        f"• Total messages: <b>{total}</b>\n"
        f"• Unique senders: <b>{unique_senders}</b>\n"
        f"• Blocked users: <b>{blocked}</b>",
        parse_mode=ParseMode.HTML,
    )


def _parse_target_user_id(args: list[str]) -> int | None:
    if len(args) != 1:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Block a user by numeric ID (owner only)."""
    message = update.effective_message
    if not message or not is_owner(update.effective_user):
        return

    target_id = _parse_target_user_id(context.args or [])
    if target_id is None:
        await message.reply_text("Usage: <code>/block &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return

    if target_id == get_owner_id():
        await message.reply_text("You cannot block yourself.")
        return

    db.block_user(target_id)
    await message.reply_text(f"🚫 Blocked user <code>{target_id}</code>.", parse_mode=ParseMode.HTML)


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unblock a user by numeric ID (owner only)."""
    message = update.effective_message
    if not message or not is_owner(update.effective_user):
        return

    target_id = _parse_target_user_id(context.args or [])
    if target_id is None:
        await message.reply_text("Usage: <code>/unblock &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return

    removed = db.unblock_user(target_id)
    if removed:
        await message.reply_text(f"✅ Unblocked user <code>{target_id}</code>.", parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(f"User <code>{target_id}</code> was not blocked.", parse_mode=ParseMode.HTML)


async def _handle_owner_reply(message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a reply from the owner back to the original sender."""
    replied = message.reply_to_message
    if not replied:
        await message.reply_text("↩️ Reply directly to any incoming message to answer that sender.")
        return

    sender_id = db.get_sender_for_owner_message(replied.message_id)
    if sender_id is None:
        await message.reply_text("⚠️ Couldn't find the original sender for that message.")
        return

    try:
        if message.text:
            await context.bot.send_message(
                chat_id=sender_id,
                text=f"💬 <b>Reply:</b>\n{html.escape(message.text)}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(chat_id=sender_id, text="💬 Reply:")
            await message.copy(chat_id=sender_id)
        await message.reply_text("✅ Reply sent.")
    except TelegramError as exc:
        logger.warning("Failed to deliver owner reply to %s: %s", sender_id, exc)
        await message.reply_text(f"❌ Could not send reply: {exc}")


async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward incoming messages to the owner with sender identity, or route owner replies."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    if is_owner(user):
        await _handle_owner_reply(message, context)
        return

    owner_id = get_owner_id()
    if owner_id is None:
        await message.reply_text("⚠️ Bot owner is not configured yet.")
        return

    if db.is_blocked(user.id):
        await message.reply_text("🚫 You are not allowed to send messages to this bot.")
        return

    header = format_sender_header(user)
    sender_name = user.full_name or "Unknown"

    try:
        if message.text:
            body = f"{header}\n\n💬 <b>Message:</b>\n{html.escape(message.text)}"
            sent = await context.bot.send_message(
                chat_id=owner_id,
                text=body,
                parse_mode=ParseMode.HTML,
            )
            db.record_message(sent.message_id, user.id, user.username, sender_name)
        else:
            header_msg = await context.bot.send_message(
                chat_id=owner_id,
                text=header,
                parse_mode=ParseMode.HTML,
            )
            copied = await message.copy(chat_id=owner_id)
            db.record_message(header_msg.message_id, user.id, user.username, sender_name)
            db.record_message(copied.message_id, user.id, user.username, sender_name)

        await message.reply_text("✅ Your message has been sent!")
    except TelegramError as exc:
        logger.error("Failed to forward message from %s to owner %s: %s", user.id, owner_id, exc)
        await message.reply_text("❌ Failed to send your message. Please try again later.")


def main() -> None:
    """Start the Telegram bot in webhook mode (Render/Web Service) or polling mode (local)."""
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "your_bot_token_here":
        raise SystemExit(
            "BOT_TOKEN is missing. Set BOT_TOKEN in your environment or .env file."
        )

    if get_owner_id() is None:
        logger.warning(
            "OWNER_CHAT_ID is not configured yet. Send /myid to the bot and set OWNER_CHAT_ID."
        )

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_incoming_message))

    base_webhook_url = (
        os.getenv("WEBHOOK_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    )
    if base_webhook_url:
        port = int(os.getenv("PORT", "10000"))
        url_path = "webhook"
        webhook_url = f"{base_webhook_url.rstrip('/')}/{url_path}"
        secret_token = os.getenv("WEBHOOK_SECRET", "").strip() or None
        logger.info("Starting webhook on 0.0.0.0:%s -> %s", port, webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=webhook_url,
            secret_token=secret_token,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Starting polling mode...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
