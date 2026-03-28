"""
Google OAuth2 helpers.
Credentials are stored as data/token.json on the Railway Volume.
"""

import json
import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

DATA_DIR   = os.environ.get("DATA_DIR", "data")
TOKEN_PATH = os.path.join(DATA_DIR, "token.json")

# ── Client secrets come from env vars (no JSON file needed on Railway) ────────

def _client_config() -> dict:
    return {
        "web": {
            "client_id":                os.environ["GOOGLE_CLIENT_ID"],
            "client_secret":            os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri":                 "https://accounts.google.com/o/oauth2/auth",
            "token_uri":                "https://oauth2.googleapis.com/token",
            "redirect_uris":            [_callback_uri()],
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        }
    }


def _callback_uri() -> str:
    base = os.environ.get("PUBLIC_URL", "http://localhost:8080").rstrip("/")
    return f"{base}/oauth/callback"


# ── Public API ────────────────────────────────────────────────────────────────

def get_auth_url(state: str, redirect_uri: str) -> str:
    """Return the Google consent-screen URL."""
    flow = Flow.from_client_config(
        _client_config(),
        scopes       = SCOPES,
        redirect_uri = redirect_uri,
    )
    url, _ = flow.authorization_url(
        access_type     = "offline",
        include_granted_scopes = "true",
        prompt          = "consent",   # force refresh_token every time
        state           = state,
    )
    return url


def exchange_code_for_tokens(code: str) -> None:
    """Exchange an auth code for credentials and persist them."""
    flow = Flow.from_client_config(
        _client_config(),
        scopes       = SCOPES,
        redirect_uri = _callback_uri(),
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_credentials(creds)
    logger.info("Tokens saved to %s", TOKEN_PATH)


def load_credentials() -> Credentials | None:
    """Load credentials from disk, refreshing if expired."""
    if not os.path.exists(TOKEN_PATH):
        return None

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if creds.expired and creds.refresh_token:
        logger.info("Refreshing expired access token…")
        creds.refresh(Request())
        _save_credentials(creds)

    return creds


def is_authenticated() -> bool:
    creds = load_credentials()
    return creds is not None and creds.valid


def _save_credentials(creds: Credentials) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
