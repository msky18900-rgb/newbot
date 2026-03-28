"""
Telegram → YouTube uploader bot.

- python-telegram-bot: commands / UI
- Telethon userbot: large-file downloads (no 20 MB cap)
- Google OAuth2: handled inside the bot via built-in HTTP server
- YouTube: resumable upload API
"""

import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from auth import get_auth_url, exchange_code_for_tokens, load_credentials, is_authenticated
from downloader import (
    get_client as get_telethon,
    is_userbot_logged_in,
    send_login_code,
    sign_in,
    sign_in_2fa,
    sign_out_userbot,
)
from uploader import upload_video

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Env ───────────────────────────────────────────────────────────────────────
OWNER_ID   = int(os.environ["OWNER_TELEGRAM_ID"])
BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
OAUTH_PORT = int(os.environ.get("OAUTH_CALLBACK_PORT", "8080"))

# ConversationHandler states
AWAIT_PHONE = 1
AWAIT_CODE  = 2
AWAIT_2FA   = 3

_pending_oauth: dict[str, int] = {}
_ptb_app = None

# Per-user login state: { user_id: { phone, phone_code_hash } }
_login_state: dict[int, dict] = {}


# ── OAuth callback HTTP server ────────────────────────────────────────────────

class OAuthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info("OAuth HTTP: " + fmt, *args)

    def do_GET(self):
        logger.info("OAuth callback hit: %s", self.path)

        parsed = urlparse(self.path)

        # Accept both /oauth/callback and /oauth/callback/
        if not parsed.path.rstrip("/").endswith("/oauth/callback"):
            logger.warning("Unknown OAuth path: %s", parsed.path)
            self._respond(404, f"Not found: {parsed.path}")
            return

        params = parse_qs(parsed.query)
        codes  = params.get("code")
        state  = params.get("state", [None])[0]

        logger.info("OAuth params — code present: %s  state: %s", bool(codes), state)

        if not codes or not state:
            self._respond(400, "Missing code or state parameter.")
            return

        chat_id = _pending_oauth.pop(state, None)
        if chat_id is None:
            self._respond(400, "Unknown or expired state. Please run /auth again.")
            return

        try:
            exchange_code_for_tokens(codes[0])
            self._respond(200, (
                "✅ Google account connected!\n\n"
                "You can close this tab and return to Telegram."
            ))
            asyncio.run_coroutine_threadsafe(
                _ptb_app.bot.send_message(
                    chat_id,
                    "✅ *Google account connected!*\n"
                    "You can now forward videos to upload them to YouTube.",
                    parse_mode="Markdown",
                ),
                _ptb_app.update_queue._loop,  # type: ignore[attr-defined]
            )
        except Exception as e:
            logger.exception("Token exchange failed")
            self._respond(500, f"Authentication failed: {e}\n\nPlease try /auth again.")

    def _respond(self, status: int, body: str):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _run_oauth_server():
    srv = HTTPServer(("0.0.0.0", OAUTH_PORT), OAuthHandler)
    logger.info("OAuth callback server listening on 0.0.0.0:%s", OAUTH_PORT)
    srv.serve_forever()


# ── Owner guard ───────────────────────────────────────────────────────────────

def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("⛔ Private bot.")
            return
        return await func(update, ctx)
    return wrapper


# ── /start ────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yt_ok  = is_authenticated()
    tg_ok  = await is_userbot_logged_in()
    await update.message.reply_text(
        "🎬 *YouTube Uploader Bot*\n\n"
        f"YouTube:  {'✅ Connected'   if yt_ok else '❌ Run /auth'}\n"
        f"Userbot:  {'✅ Logged in'   if tg_ok else '❌ Run /login'}\n\n"
        "Forward any video once both show ✅.",
        parse_mode="Markdown",
    )


# ── /status ───────────────────────────────────────────────────────────────────

@owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yt_ok = is_authenticated()
    tg_ok = await is_userbot_logged_in()
    lines = [
        f"*YouTube:* {'✅ authenticated' if yt_ok else '❌ not connected'}",
        f"*Userbot:* {'✅ logged in'     if tg_ok else '❌ not logged in'}",
    ]
    if yt_ok:
        creds = load_credentials()
        lines.append(f"*Token valid:* `{not creds.expired}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /auth ─────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import secrets
    state   = secrets.token_urlsafe(16)
    chat_id = update.effective_chat.id
    _pending_oauth[state] = chat_id

    auth_url = get_auth_url(state)
    logger.info("Auth URL generated for chat_id=%s", chat_id)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔑 Connect Google Account", url=auth_url)
    ]])
    await update.message.reply_text(
        "Tap the button to authorise YouTube access.\n"
        "After approving, return here — the bot confirms automatically.",
        reply_markup=kb,
    )


@owner_only
async def cmd_revoke_yt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    path = os.path.join(os.environ.get("DATA_DIR", "data"), "token.json")
    if os.path.exists(path):
        os.remove(path)
        await update.message.reply_text("🗑 YouTube token revoked. Use /auth to reconnect.")
    else:
        await update.message.reply_text("No YouTube token found.")


# ── /login (Telegram userbot, ConversationHandler) ────────────────────────────

@owner_only
async def cmd_login_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await is_userbot_logged_in():
        await update.message.reply_text(
            "✅ Userbot is already logged in.\nUse /logoutuserbot to disconnect."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "📱 *Telegram Userbot Login*\n\n"
        "Send your phone number in international format:\n"
        "`+919876543210`\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAIT_PHONE


async def login_got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return ConversationHandler.END

    phone = update.message.text.strip()
    await update.message.reply_text(f"📨 Sending code to {phone}…")

    try:
        phone_code_hash = await send_login_code(phone)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send code: {e}")
        return ConversationHandler.END

    # Store for this user
    _login_state[update.effective_user.id] = {
        "phone":           phone,
        "phone_code_hash": phone_code_hash,
    }

    await update.message.reply_text(
        "✉️ Code sent!\n\n"
        "Enter the code from Telegram — paste it *exactly* as shown "
        "(e.g. `12345`, no spaces needed).\n\n"
        "⚠️ Do NOT share this code with anyone.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAIT_CODE


async def login_got_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return ConversationHandler.END

    code  = update.message.text.strip().replace(" ", "")
    state = _login_state.get(update.effective_user.id)

    if not state:
        await update.message.reply_text("Session expired. Please start /login again.")
        return ConversationHandler.END

    try:
        await sign_in(
            phone           = state["phone"],
            code            = code,
            phone_code_hash = state["phone_code_hash"],
        )
        _login_state.pop(update.effective_user.id, None)
        await update.message.reply_text(
            "✅ *Userbot logged in!*\nYou can now forward videos up to 2 GB.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    except RuntimeError as e:
        if str(e) == "2FA_REQUIRED":
            await update.message.reply_text(
                "🔐 Two-factor authentication is enabled.\n"
                "Send your Telegram cloud password:"
            )
            return AWAIT_2FA
        _login_state.pop(update.effective_user.id, None)
        await update.message.reply_text(f"❌ Login failed: {e}\n\nTry /login again.")
        return ConversationHandler.END

    except Exception as e:
        _login_state.pop(update.effective_user.id, None)
        await update.message.reply_text(f"❌ Login failed: {e}\n\nTry /login again.")
        return ConversationHandler.END


async def login_got_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return ConversationHandler.END

    password = update.message.text.strip()

    try:
        await sign_in_2fa(password)
        _login_state.pop(update.effective_user.id, None)
        await update.message.reply_text(
            "✅ *Userbot logged in!*\nYou can now forward videos up to 2 GB.",
            parse_mode="Markdown",
        )
    except Exception as e:
        _login_state.pop(update.effective_user.id, None)
        await update.message.reply_text(f"❌ 2FA failed: {e}\n\nTry /login again.")

    return ConversationHandler.END


async def login_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _login_state.pop(update.effective_user.id, None)
    await update.message.reply_text("Login cancelled.")
    return ConversationHandler.END


@owner_only
async def cmd_logout_userbot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await sign_out_userbot()
        await update.message.reply_text("✅ Userbot logged out.")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ── Video handler ─────────────────────────────────────────────────────────────

@owner_only
async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authenticated():
        await update.message.reply_text("❌ YouTube not connected. Run /auth first.")
        return
    if not await is_userbot_logged_in():
        await update.message.reply_text("❌ Userbot not logged in. Run /login first.")
        return

    msg   = update.message
    video = msg.video or msg.document

    if video is None:
        await msg.reply_text("Please forward a video file.")
        return

    status_msg = await msg.reply_text("⬇️ Fetching video…")

    try:
        # Bridge: get the raw Telethon message so downloader can call download_media()
        telethon_client = await get_telethon()
        tg_msg = await telethon_client.get_messages(
            entity=msg.chat_id,
            ids=msg.message_id,
        )
        if tg_msg is None:
            raise RuntimeError(
                "Userbot couldn't see this message. "
                "Make sure your account can access the source chat."
            )

        video._telethon_message = tg_msg

        from downloader import download_telegram_video
        local_path, filename = await download_telegram_video(ctx.bot, video, status_msg)

        caption = msg.caption
        if not caption and getattr(msg, "forward_origin", None):
            try:
                caption = msg.forward_origin.sender_user.full_name
            except AttributeError:
                pass
        title       = (caption or filename or "Uploaded via Telegram Bot")[:100]
        description = f"Uploaded via Telegram Bot\nOriginal file: {filename}"

        await status_msg.edit_text("📤 Uploading to YouTube…")

        loop = asyncio.get_event_loop()

        def _progress_cb(pct: int):
            asyncio.run_coroutine_threadsafe(
                status_msg.edit_text(f"📤 Uploading to YouTube… {pct}%"),
                loop,
            )

        video_url = await asyncio.to_thread(
            upload_video,
            local_path,
            title=title,
            description=description,
            progress_cb=_progress_cb,
        )

        await status_msg.edit_text(
            f"✅ *Uploaded!*\n\n🎬 [{title[:60]}]({video_url})",
            parse_mode="Markdown",
            disable_web_page_preview=False,
        )

    except Exception as e:
        logger.exception("Upload pipeline failed")
        await status_msg.edit_text(f"❌ Error: {e}")
    finally:
        try:
            if "local_path" in locals() and os.path.exists(local_path):
                os.remove(local_path)
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _ptb_app

    threading.Thread(target=_run_oauth_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    _ptb_app = app

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", cmd_login_start)],
        states={
            AWAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_phone)],
            AWAIT_CODE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_code)],
            AWAIT_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_2fa)],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
        conversation_timeout=300,
    )

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("auth",          cmd_auth))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CommandHandler("revoke",        cmd_revoke_yt))
    app.add_handler(CommandHandler("logoutuserbot", cmd_logout_userbot))
    app.add_handler(login_conv)
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO,
        handle_video,
    ))

    logger.info("Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
