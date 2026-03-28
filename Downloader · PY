"""
Download Telegram files using a Telethon userbot session.

Why: python-telegram-bot (Bot API) caps downloads at 20 MB.
     Telethon connects as your personal account — no size limit (up to 2 GB).

Session file is stored at DATA_DIR/userbot.session (Railway Volume).
First-time login is done via /login command inside the bot.
"""

import asyncio
import logging
import os
import tempfile

from telethon import TelegramClient

logger = logging.getLogger(__name__)

DATA_DIR     = os.environ.get("DATA_DIR", "data")
API_ID       = int(os.environ["TELEGRAM_API_ID"])
API_HASH     = os.environ["TELEGRAM_API_HASH"]
SESSION_PATH = os.path.join(DATA_DIR, "userbot")   # Telethon appends .session

_client: TelegramClient | None = None
_client_lock = asyncio.Lock()


# ── Client lifecycle ──────────────────────────────────────────────────────────

async def get_client() -> TelegramClient:
    """Return a started Telethon client (singleton)."""
    global _client
    async with _client_lock:
        if _client is None or not _client.is_connected():
            os.makedirs(DATA_DIR, exist_ok=True)
            _client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
            await _client.connect()
        return _client


async def is_userbot_logged_in() -> bool:
    try:
        client = await get_client()
        return await client.is_user_authorized()
    except Exception:
        return False


async def send_login_code(phone: str) -> str:
    """
    Request a login code for *phone*.
    Returns the phone_code_hash needed to sign in.
    """
    client = await get_client()
    result = await client.send_code_request(phone)
    return result.phone_code_hash


async def sign_in(phone: str, code: str, phone_code_hash: str, password: str | None = None):
    """Complete sign-in with the received code (and 2FA password if set)."""
    client = await get_client()
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except Exception as e:
        if "two" in str(e).lower() or "password" in str(e).lower():
            if password is None:
                raise RuntimeError("2FA_REQUIRED")
            await client.sign_in(password=password)
        else:
            raise


async def sign_out_userbot():
    client = await get_client()
    await client.log_out()
    session_file = SESSION_PATH + ".session"
    if os.path.exists(session_file):
        os.remove(session_file)


# ── Download ──────────────────────────────────────────────────────────────────

async def download_telegram_video(
    bot,          # python-telegram-bot Bot (kept for API compat, unused here)
    video_obj,    # telegram.Video or telegram.Document (PTB object)
    status_msg,   # PTB Message — edited with progress
) -> tuple[str, str]:
    """
    Download *video_obj* via Telethon userbot.
    Returns (local_path, filename).
    """
    client = await get_client()

    if not await client.is_user_authorized():
        raise RuntimeError("Userbot not logged in. Send /login to authenticate.")

    file_name = getattr(video_obj, "file_name", None) or f"{video_obj.file_id}.mp4"
    file_size = getattr(video_obj, "file_size", 0) or 0

    logger.info("Downloading via Telethon: name=%s  size=%s bytes", file_name, file_size)

    # bot.py attaches the raw Telethon message so we can pass it to download_media
    tg_message = getattr(video_obj, "_telethon_message", None)
    if tg_message is None:
        raise RuntimeError(
            "Telethon message reference missing. "
            "Ensure _attach_telethon_message() ran in bot.py."
        )

    os.makedirs(DATA_DIR, exist_ok=True)
    suffix = os.path.splitext(file_name)[1] or ".mp4"
    tmp    = tempfile.NamedTemporaryFile(dir=DATA_DIR, suffix=suffix, delete=False)
    tmp.close()
    local_path = tmp.name

    last_reported = [-1]   # mutable cell for closure

    async def _progress(received: int, total: int):
        if not total:
            return
        pct    = int(received / total * 100)
        bucket = pct // 5          # report every 5 %
        if bucket != last_reported[0]:
            last_reported[0] = bucket
            try:
                await status_msg.edit_text(f"⬇️ Downloading… {pct}%")
            except Exception:
                pass

    await client.download_media(
        tg_message,
        file              = local_path,
        progress_callback = _progress,
    )

    actual_size = os.path.getsize(local_path)
    logger.info("Download complete: %s  (%d bytes)", local_path, actual_size)
    return local_path, file_name
