"""
Google OAuth2 helpers.
Credentials are stored at DATA_DIR/token.json on the Railway Volume.
"""

import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _data_dir() -> str:
    return os.environ.get("DATA_DIR", "data")


def _token_path() -> str:
    return os.path.join(_data_dir(), "token.json")


def _callback_uri() -> str:
    """
    Built fresh on every call so it always reflects the current PUBLIC_URL env var.
    Must exactly match the URI registered in Google Cloud Console.
    """
    base = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            "PUBLIC_URL env var is not set. "
            "Set it to your Railway domain, e.g. https://yourapp.up.railway.app"
        )
    uri = f"{base}/oauth/callback"
    logger.debug("OAuth redirect_uri: %s", uri)
    return uri


def _client_config(redirect_uri: str) -> dict:
    return {
        "web": {
            "client_id":     os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        }
    }


# ── Public API ────────────────────────────────────────────────────────────────

def get_auth_url(state: str) -> str:
    """Return the Google consent-screen URL."""
    redirect_uri = _callback_uri()
    flow = Flow.from_client_config(
        _client_config(redirect_uri),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    logger.info("Generated auth URL with redirect_uri=%s", redirect_uri)
    return url


def exchange_code_for_tokens(code: str) -> None:
    """Exchange an auth code for credentials and persist them."""
    redirect_uri = _callback_uri()
    flow = Flow.from_client_config(
        _client_config(redirect_uri),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    _save_credentials(flow.credentials)
    logger.info("Tokens saved to %s", _token_path())


def load_credentials() -> Credentials | None:
    """Load credentials from disk, refreshing access token if expired."""
    path = _token_path()
    if not os.path.exists(path):
        return None

    creds = Credentials.from_authorized_user_file(path, SCOPES)

    if creds.expired and creds.refresh_token:
        logger.info("Refreshing expired access token…")
        creds.refresh(Request())
        _save_credentials(creds)

    return creds


def is_authenticated() -> bool:
    creds = load_credentials()
    return creds is not None and creds.valid


def _save_credentials(creds: Credentials) -> None:
    os.makedirs(_data_dir(), exist_ok=True)
    with open(_token_path(), "w") as f:
        f.write(creds.to_json())
