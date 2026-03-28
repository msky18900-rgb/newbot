"""
Upload a local video file to YouTube using the resumable upload API.
Handles files of any size and retries transient failures automatically.
"""

import logging
import os
import time
from typing import Callable

import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from auth import load_credentials

logger = logging.getLogger(__name__)

# Chunk size for resumable upload: 8 MB
CHUNK_SIZE = 8 * 1024 * 1024

# Retryable HTTP error codes
RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES      = 10


def upload_video(
    local_path: str,
    title:       str       = "Untitled",
    description: str       = "",
    category_id: str       = "22",   # People & Blogs
    privacy:     str       = "unlisted",
    progress_cb: Callable | None = None,
) -> str:
    """
    Upload *local_path* to YouTube.

    Parameters
    ----------
    local_path   : path to the video file on disk
    title        : YouTube video title (max 100 chars)
    description  : YouTube video description
    category_id  : YouTube category ID (22 = People & Blogs)
    privacy      : "public" | "unlisted" | "private"
    progress_cb  : optional callable(percent: int) called during upload

    Returns
    -------
    URL of the uploaded video  (https://youtu.be/<id>)
    """
    creds = load_credentials()
    if creds is None or not creds.valid:
        raise RuntimeError("Not authenticated. Run /auth first.")

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title":       title,
            "description": description,
            "categoryId":  category_id,
        },
        "status": {
            "privacyStatus":            privacy,
            "selfDeclaredMadeForKids":  False,
        },
    }

    file_size = os.path.getsize(local_path)
    logger.info(
        "Starting resumable upload: %s  size=%d  title=%r",
        local_path, file_size, title,
    )

    media = MediaFileUpload(
        local_path,
        mimetype    = "video/*",
        chunksize   = CHUNK_SIZE,
        resumable   = True,
    )

    request = youtube.videos().insert(
        part  = "snippet,status",
        body  = body,
        media_body = media,
    )

    response   = None
    error      = None
    retry      = 0

    while response is None:
        try:
            status, response = request.next_chunk()

            if status:
                pct = int(status.resumable_progress / file_size * 100)
                logger.info("Upload progress: %d%%", pct)
                if progress_cb:
                    try:
                        progress_cb(pct)
                    except Exception:
                        pass

        except HttpError as e:
            if e.resp.status in RETRYABLE_STATUS:
                error = e
            else:
                raise

        except (httplib2.HttpLib2Error, IOError) as e:
            error = e

        if error is not None:
            retry += 1
            if retry > MAX_RETRIES:
                raise RuntimeError(f"Upload failed after {MAX_RETRIES} retries: {error}")
            wait = min(2 ** retry, 64)
            logger.warning("Retryable error (%s), sleeping %ds…", error, wait)
            time.sleep(wait)
            error = None

    video_id = response["id"]
    url      = f"https://youtu.be/{video_id}"
    logger.info("Upload complete: %s", url)

    if progress_cb:
        try:
            progress_cb(100)
        except Exception:
            pass

    return url
