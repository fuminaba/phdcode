"""One-time (or re-run-when-it-expires) Google OAuth setup, covering
every Google connector (Gmail, Calendar) in a single consent flow -- one
combined credential, one re-auth, rather than a separate script/consent
step per connector. Run this from your own machine, not the server -- it
opens a browser for Google's consent screen and needs a local redirect
target, neither of which are possible on the headless/Tailscale-only
server. It writes the resulting credentials straight into the server's
Postgres DB (via the same DATABASE_URL the app itself uses), so it needs
network access to that database, not to anything else on the server.

Before running this, register an OAuth 2.0 Client ID in Google Cloud
Console as type "Desktop app" (not "Web application" -- that's what makes
run_local_server's arbitrary loopback port work without pre-registering
an exact redirect URI), enable both the Gmail API and the Google Calendar
API for that project, and set GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET in .env
from the credentials it gives you (named for Gmail historically -- the
same client id/secret pair now covers Calendar too, not renamed to avoid
an unnecessary .env churn for existing setups).

Re-run this whenever Google access stops working (gmail_client.py/
calendar_client.py both raise GoogleAuthError) -- most likely cause is
Google's 7-day refresh-token cap for apps left in "Testing" publishing
status, see docs/HOOKS_PLAN.md.

Usage:
    python scripts/setup_google_oauth.py <alira_user_id>
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from google_auth_oauthlib.flow import InstalledAppFlow

from llm_engine.memory import init_schema, upsert_oauth_credentials
from llm_engine.tooling._tooling_deps.google_auth import SERVICE_NAME

# Least-privilege by design: Gmail can list/read mail and create drafts
# (never send/delete/modify existing mail -- see gmail_client.py's own
# note); Calendar can list/create events (never touch calendar list
# management or settings -- see calendar_client.py's own note). Widening
# this list means every already-stored credential needs re-consent
# (Google scopes are baked into the token at grant time), not just a code
# change here.
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
]


def _client_config() -> dict:
    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET must be set in .env before running this.")
        sys.exit(1)
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


async def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/setup_google_oauth.py <alira_user_id>")
        sys.exit(1)
    user_id = sys.argv[1]

    flow = InstalledAppFlow.from_client_config(_client_config(), _SCOPES)
    print("Opening a browser for Google's consent screen...")
    creds = flow.run_local_server(port=0)

    await init_schema()
    await upsert_oauth_credentials(
        user_id, SERVICE_NAME, creds.token, creds.refresh_token, creds.expiry, list(creds.scopes),
    )
    print(f"Google connected (Gmail + Calendar) for user_id={user_id!r}. Scopes: {', '.join(creds.scopes)}")


if __name__ == "__main__":
    asyncio.run(main())
