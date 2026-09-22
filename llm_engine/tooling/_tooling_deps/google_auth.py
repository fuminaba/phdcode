"""Shared Google OAuth credential loading/refresh -- one combined
credential per user covering every Google connector (Gmail, Calendar),
not one per service. Extracted out of what was gmail_client.py's own
get_credentials/GmailAuthError once Calendar needed the exact same
logic: a single consent flow (scripts/setup_google_oauth.py) requests
every connector's scopes together, so there's one oauth_credentials row
per user (service='google') to load and refresh, not one per API.
"""
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

try:
    from ...context import run_on_main_loop
except ImportError:
    from context import run_on_main_loop

load_dotenv()

log = logging.getLogger(__name__)

# The single row every Google connector reads/refreshes -- see this
# module's own docstring for why it's one shared row, not one per API.
SERVICE_NAME = "google"
GOOGLE_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET")
# Google's own fixed token endpoint -- same for every installed-app OAuth
# client, not something scripts/setup_google_oauth.py's project needs to
# configure per user.
_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleAuthError(RuntimeError):
    """Raised when this user has no stored Google credentials at all
    (never ran scripts/setup_google_oauth.py), or an access-token refresh
    fails (most likely cause: the refresh token expired -- Google caps
    these at 7 days while an OAuth app sits in "Testing" publishing
    status, which a personal project's app does by default -- see
    docs/HOOKS_PLAN.md). Callers should surface this as a clear,
    actionable message, not a generic failure -- and where the caller is
    itself best-effort (e.g. a reminder's calendar-busy check), catching
    this and proceeding as if not busy is the right degrade, not
    breaking the reminder outright."""


def get_credentials(user_id: str) -> Credentials:
    """Loads this user's stored Google credentials and refreshes them if
    the access token is missing/expired, persisting the refreshed token
    back to oauth_credentials so the next call doesn't need to refresh
    again. Raises GoogleAuthError if there's nothing stored yet, or if a
    needed refresh fails. Shared by gmail_client.py and
    calendar_client.py -- both read the same row, since one consent flow
    grants scopes for both."""
    try:
        from ...memory.db import get_oauth_credentials, update_oauth_access_token
    except ImportError:
        from memory.db import get_oauth_credentials, update_oauth_access_token

    row = run_on_main_loop(get_oauth_credentials(user_id, SERVICE_NAME))
    if row is None:
        raise GoogleAuthError(
            "No Google credentials stored for this user -- run "
            "scripts/setup_google_oauth.py once to connect Gmail/Calendar."
        )
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise GoogleAuthError(
            "GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET are not set in .env -- "
            "needed to refresh Google access tokens."
        )

    creds = Credentials(
        token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_uri=_TOKEN_URI,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=row["scopes"],
    )
    expires_at = row["expires_at"]
    needs_refresh = (
        not creds.token
        or expires_at is None
        or expires_at <= datetime.now(timezone.utc)
    )
    if needs_refresh:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise GoogleAuthError(
                f"Google access token refresh failed -- the stored refresh "
                f"token is likely expired (Google caps these at 7 days for "
                f"apps in \"Testing\" status). Rerun "
                f"scripts/setup_google_oauth.py to reconnect. ({e})"
            ) from e
        run_on_main_loop(
            update_oauth_access_token(user_id, SERVICE_NAME, creds.token, creds.expiry)
        )
    return creds
