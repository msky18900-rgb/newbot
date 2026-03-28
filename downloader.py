"""
Download Telegram files using a Telethon userbot session.

The client is a true singleton — created once, never recreated mid-session.
This is critical: phone_code_hash from send_code_request must be consumed
by sign_in on the exact same client object, or Telegram rejects the code.
"""

import asyncio
import logging
import os
import tempfile

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

logger = logging.getLogger(__name__)

DATA_DIR     = os.environ.get("DATA_DIR", "data")
API_ID       = int(os.environ["TELEGRAM_API_ID"])
API_HASH     = os.environ["TELEGRAM_API_HASH"]
SESSION_PATH = os.path.join(DATA_DIR, "userbot")

# ── True singleton — never replaced once created ──────────────────────────────
_client: TelegramClient | None = None
_client_ready = False   # True once connect() has been called


async def get_client() -> TelegramClient:
    """Return the singleton Telethon client, connecting if needed."""
    global _client, _client_ready
    if _client is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

    if not _client.is_connected():
        await _client.connect()
        _client_ready = True

    return _client


async def is_userbot_logged_in() -> bool:
    try:
        client = await get_client()
        return await client.is_user_authorized()
    except Exception:
        return False


async def send_login_code(phone: str) -> str:
    """
    Request a login code.
    IMPORTANT: uses the singleton client — sign_in must use the same instance.
    Returns phone_code_hash.
    """
    client = await get_client()
    # Disconnect any existing auth attempt cleanly first
    result = await client.send_code_request(phone, force_sms=False)
    logger.info("Login code sent to %s  hash=%s…", phone, result.phone_code_hash[:6])
    return result.phone_code_hash


async def sign_in(
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str | None = None,
) -> None:
    """
    Sign in using the singleton client (same one that called send_code_request).
    Raises RuntimeError('2FA_REQUIRED') if a cloud password is needed.
    """
    client = await get_client()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if password is None:
            raise RuntimeError("2FA_REQUIRED")
        await client.sign_in(password=password)


async def sign_in_2fa(password: str) -> None:
    """Complete 2FA sign-in (call after sign_in raises 2FA_REQUIRED)."""
    client = await get_client()
    await client.sign_in(password=password)


async def sign_out_userbot() -> None:
    global _client, _client_ready
    client = await get_client()
    await client.log_out()
    _client = None
    _client_ready = False
    session_file = SESSION_PATH + ".session"
    if os.path.exists(session_file):
        os.remove(session_file)


# ── Download ──────────────────────────────────────────────────────────────────

async def download_telegram_video(
    bot,
    video_obj,
    status_msg,
) -> tuple[str, str]:
    """Download video via Telethon userbot. Returns (local_path, filename)."""
    client = await get_client()

    if not await client.is_user_authorized():
        raise RuntimeError("Userbot not logged in. Send /login to authenticate.")

    file_name = getattr(video_obj, "file_name", None) or f"{video_obj.file_id}.mp4"

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

    last_reported = [-1]

    async def _progress(received: int, total: int):
        if not total:
            return
        pct    = int(received / total * 100)
        bucket = pct // 5
        if bucket != last_reported[0]:
            last_reported[0] = bucket
            try:
                await status_msg.edit_text(f"⬇️ Downloading… {pct}%")
            except Exception:
                pass

    await client.download_media(
        tg_message,
        file=local_path,
        progress_callback=_progress,
    )

    logger.info("Download complete: %s (%d bytes)", local_path, os.path.getsize(local_path))
    return local_path, file_name
