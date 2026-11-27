"""Telegram Inbox Bot (Persian): tracks /start events, shows sender identity to owner, and persists to Postgres/SQLite."""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import signal
from pathlib import Path

import tornado.web
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


async def get_owner_display_handle(context: ContextTypes.DEFAULT_TYPE, owner_id: int) -> str:
    """Return @username (or display name) of the owner for the Persian welcome message."""
    env_username = os.getenv("OWNER_USERNAME", "").strip().lstrip("@")
    if env_username:
        return f"@{html.escape(env_username)}"

    try:
        chat = await context.bot.get_chat(owner_id)
        if chat.username:
            return f"@{html.escape(chat.username)}"
        if chat.full_name:
            return html.escape(chat.full_name)
    except TelegramError as exc:
        logger.warning("Could not fetch owner chat info for %s: %s", owner_id, exc)

    return "مالک ربات"


def format_sender_header(user: User, chat_id: int, title: str = "📩 پیام جدید دریافت شد") -> str:
    """Build an HTML-formatted Persian sender identity block for the bot owner."""
    full_name = html.escape(user.full_name or "نامشخص")
    username = f"@{html.escape(user.username)}" if user.username else "<i>ندارد</i>"
    profile_link = f'<a href="tg://user?id={user.id}">{full_name}</a>'
    return (
        f"<b>{html.escape(title)}</b>\n\n"
        f"👤 <b>نام:</b> {profile_link}\n"
        f"🔗 <b>یوزرنیم:</b> {username}\n"
        f"🆔 <b>آیدی عددی (User ID):</b> <code>{user.id}</code>\n"
        f"💬 <b>چت آیدی (Chat ID):</b> <code>{chat_id}</code>"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start and ?start=ref_<id> deep links, logging and notifying owner."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    owner_id = get_owner_id()

    if is_owner(user):
        bot_username = context.bot.username or "your_bot"
        ref_link = f"https://t.me/{html.escape(bot_username)}?start=ref_{user.id}"
        db_mode = "PostgreSQL" if db.is_postgres else "SQLite"
        await message.reply_text(
            "👑 <b>حالت مالک فعال است</b>\n\n"
            f"🔗 <b>لینک پیام ناشناس شما:</b>\n{ref_link}\n\n"
            "هر کسی با این لینک ربات را استارت کند یا پیام بفرستد، فوراً همراه با نام، یوزرنیم، آیدی عددی و چت آیدی به شما اطلاع داده می‌شود و در دیتابیس ذخیره می‌گردد.\n\n"
            f"💾 <b>دیتابیس فعال:</b> {db_mode}\n\n"
            "<b>دستورات:</b>\n"
            "• <code>/stats</code> — آمار کل کاربران، استارت‌ها و پیام‌ها\n"
            "• <code>/users</code> — لیست آخرین کاربرانی که ربات را استارت کرده‌اند\n"
            "• <code>/export</code> — دریافت فایل بکاپ کامل دیتابیس (JSON)\n"
            "• <code>/block &lt;user_id&gt;</code> — مسدود کردن کاربر\n"
            "• <code>/unblock &lt;user_id&gt;</code> — رفع مسدودیت کاربر\n"
            "• برای پاسخ دادن، روی هر پیام دریافتی ریپلای (Reply) کنید.",
            parse_mode=ParseMode.HTML,
        )
        return

    if owner_id is None:
        await message.reply_text("⚠️ ربات هنوز توسط مالک تنظیم نشده است.")
        return

    start_param = context.args[0] if context.args else None
    sender_name = user.full_name or "نامشخص"
    start_count = db.record_start(
        user_id=user.id,
        chat_id=message.chat_id,
        username=user.username,
        full_name=sender_name,
        start_param=start_param,
    )

    param_line = (
        f"\n🔗 <b>پارامتر لینک:</b> <code>{html.escape(start_param)}</code>"
        if start_param
        else "\n🔗 <b>پارامتر لینک:</b> <i>بدون پارامتر</i>"
    )
    notify_text = (
        f"{format_sender_header(user, message.chat_id, title='🔔 کاربر جدید ربات را استارت کرد!')}"
        f"{param_line}\n"
        f"🔢 <b>تعداد استارت:</b> {start_count}"
    )
    try:
        await context.bot.send_message(
            chat_id=owner_id,
            text=notify_text,
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        logger.warning("Failed to notify owner %s about /start from %s: %s", owner_id, user.id, exc)

    owner_handle = await get_owner_display_handle(context, owner_id)
    await message.reply_text(
        f"بصورت ناشناس به {owner_handle} پیام بده 👇\n\n"
        "هر متن، عکس، ویس، استیکر یا فایلی بفرستی براش ارسال می‌شه.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the caller's numeric Telegram user ID and chat ID."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    await message.reply_text(
        f"🆔 آیدی عددی شما: <code>{user.id}</code>\n"
        f"💬 چت آیدی: <code>{message.chat_id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user, /start, message, and block statistics (owner only)."""
    message = update.effective_message
    if not message or not is_owner(update.effective_user):
        return

    total_users, total_starts, total_messages, unique_senders, blocked = db.get_stats()
    db_mode = "PostgreSQL" if db.is_postgres else "SQLite"
    await message.reply_text(
        f"📊 <b>آمار ربات ({db_mode})</b>\n"
        f"• کل کاربران ثبت‌شده: <b>{total_users}</b>\n"
        f"• کل دفعات استارت: <b>{total_starts}</b>\n"
        f"• کل پیام‌ها: <b>{total_messages}</b>\n"
        f"• فرستنده‌های یکتا: <b>{unique_senders}</b>\n"
        f"• کاربران مسدودشده: <b>{blocked}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the most recent users who started or messaged the bot (owner only)."""
    message = update.effective_message
    if not message or not is_owner(update.effective_user):
        return

    users = db.get_recent_users(limit=20)
    if not users:
        await message.reply_text("هنوز هیچ کاربری ثبت نشده است.")
        return

    lines = ["👥 <b>آخرین کاربران ثبت‌شده:</b>\n"]
    for idx, row in enumerate(users, start=1):
        name = html.escape(str(row["full_name"]))
        username = f"@{html.escape(row['username'])}" if row.get("username") else "ندارد"
        uid = row["user_id"]
        cid = row["chat_id"]
        starts = row["start_count"]
        lines.append(
            f"{idx}. <a href=\"tg://user?id={uid}\">{name}</a> | {username}\n"
            f"   🆔 <code>{uid}</code> | 💬 <code>{cid}</code> | استارت: {starts}"
        )

    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export the full database as a JSON file to the owner (owner only)."""
    message = update.effective_message
    if not message or not is_owner(update.effective_user):
        return

    data = db.export_all()
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    bio = io.BytesIO(payload)
    bio.name = "thetanonbot_export.json"
    await message.reply_document(
        document=bio,
        caption=(
            f"💾 بکاپ کامل دیتابیس\n"
            f"کاربران: {len(data['users'])} | استارت‌ها: {len(data['start_logs'])} | پیام‌ها: {len(data['messages'])}"
        ),
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
        await message.reply_text(
            "نحوه استفاده: <code>/block &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_id == get_owner_id():
        await message.reply_text("نمی‌توانید خودتان را مسدود کنید.")
        return

    db.block_user(target_id)
    await message.reply_text(
        f"🚫 کاربر <code>{target_id}</code> مسدود شد.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unblock a user by numeric ID (owner only)."""
    message = update.effective_message
    if not message or not is_owner(update.effective_user):
        return

    target_id = _parse_target_user_id(context.args or [])
    if target_id is None:
        await message.reply_text(
            "نحوه استفاده: <code>/unblock &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    removed = db.unblock_user(target_id)
    if removed:
        await message.reply_text(
            f"✅ مسدودیت کاربر <code>{target_id}</code> برداشته شد.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply_text(
            f"کاربر <code>{target_id}</code> مسدود نبود.",
            parse_mode=ParseMode.HTML,
        )


async def _handle_owner_reply(message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a reply from the owner back to the original sender."""
    replied = message.reply_to_message
    if not replied:
        await message.reply_text(
            "↩️ برای پاسخ دادن، روی یکی از پیام‌های دریافتی ریپلای (Reply) کنید."
        )
        return

    sender_id = db.get_sender_for_owner_message(replied.message_id)
    if sender_id is None:
        await message.reply_text("⚠️ فرستنده اصلی این پیام پیدا نشد.")
        return

    try:
        if message.text:
            await context.bot.send_message(
                chat_id=sender_id,
                text=f"💬 <b>پاسخ جدید:</b>\n{html.escape(message.text)}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(chat_id=sender_id, text="💬 پاسخ جدید:")
            await message.copy(chat_id=sender_id)
        await message.reply_text("✅ پاسخ شما ارسال شد.")
    except TelegramError as exc:
        logger.warning("Failed to deliver owner reply to %s: %s", sender_id, exc)
        await message.reply_text(f"❌ ارسال پاسخ ناموفق بود: {exc}")


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
        await message.reply_text("⚠️ ربات هنوز توسط مالک تنظیم نشده است.")
        return

    if db.is_blocked(user.id):
        await message.reply_text("🚫 شما امکان ارسال پیام به این ربات را ندارید.")
        return

    header = format_sender_header(user, message.chat_id)
    sender_name = user.full_name or "نامشخص"

    try:
        if message.text:
            body = f"{header}\n\n💬 <b>متن پیام:</b>\n{html.escape(message.text)}"
            sent = await context.bot.send_message(
                chat_id=owner_id,
                text=body,
                parse_mode=ParseMode.HTML,
            )
            db.record_message(
                sent.message_id,
                user.id,
                user.username,
                sender_name,
                chat_id=message.chat_id,
                message_type="text",
                content_preview=message.text[:500],
            )
        else:
            header_msg = await context.bot.send_message(
                chat_id=owner_id,
                text=header,
                parse_mode=ParseMode.HTML,
            )
            copied = await message.copy(chat_id=owner_id)
            media_preview = message.caption[:500] if message.caption else None
            db.record_message(
                header_msg.message_id,
                user.id,
                user.username,
                sender_name,
                chat_id=message.chat_id,
                message_type="media",
                content_preview=media_preview,
            )
            db.record_message(
                copied.message_id,
                user.id,
                user.username,
                sender_name,
                chat_id=message.chat_id,
                message_type="media",
                content_preview=media_preview,
            )

        await message.reply_text("✅ پیام شما با موفقیت ارسال شد!")
    except TelegramError as exc:
        logger.error("Failed to forward message from %s to owner %s: %s", user.id, owner_id, exc)
        await message.reply_text("❌ ارسال پیام با خطا مواجه شد. لطفاً دوباره تلاش کنید.")


class HealthHandler(tornado.web.RequestHandler):
    """Health-check endpoint so GET/HEAD / and /health return a tiny 200 OK."""

    def get(self) -> None:
        self.set_status(200)
        self.set_header("Content-Type", "text/plain; charset=utf-8")
        self.write("OK")

    def head(self) -> None:
        self.set_status(200)
        self.set_header("Content-Type", "text/plain; charset=utf-8")


class WebhookHandler(tornado.web.RequestHandler):
    """Receives Telegram webhook POST updates and forwards them to the PTB queue."""

    def initialize(self, ptb_app: Application, secret_token: str | None) -> None:
        self.ptb_app = ptb_app
        self.secret_token = secret_token

    async def post(self) -> None:
        if self.secret_token:
            incoming_secret = self.request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if incoming_secret != self.secret_token:
                self.set_status(403)
                self.write("Forbidden")
                return

        try:
            data = json.loads(self.request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.set_status(400)
            self.write("Bad Request")
            return

        update = Update.de_json(data, self.ptb_app.bot)
        if update is not None:
            await self.ptb_app.update_queue.put(update)
        self.set_status(200)
        self.write("OK")


async def run_webhook_server(
    ptb_app: Application,
    port: int,
    webhook_url: str,
    secret_token: str | None,
) -> None:
    """Run Tornado HTTP server with health check + Telegram webhook."""
    await ptb_app.initialize()
    await ptb_app.start()
    await ptb_app.bot.set_webhook(
        url=webhook_url,
        secret_token=secret_token,
        allowed_updates=Update.ALL_TYPES,
    )

    tornado_app = tornado.web.Application(
        [
            (r"/", HealthHandler),
            (r"/health", HealthHandler),
            (r"/webhook", WebhookHandler, {"ptb_app": ptb_app, "secret_token": secret_token}),
        ]
    )
    tornado_app.listen(port, address="0.0.0.0")
    logger.info("Webhook server listening on 0.0.0.0:%s -> %s", port, webhook_url)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    finally:
        await ptb_app.stop()
        await ptb_app.shutdown()


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
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_incoming_message))

    base_webhook_url = (
        os.getenv("WEBHOOK_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    )
    if base_webhook_url:
        port = int(os.getenv("PORT", "10000"))
        webhook_url = f"{base_webhook_url.rstrip('/')}/webhook"
        secret_token = os.getenv("WEBHOOK_SECRET", "").strip() or None
        asyncio.run(run_webhook_server(app, port, webhook_url, secret_token))
    else:
        logger.info("Starting polling mode...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
