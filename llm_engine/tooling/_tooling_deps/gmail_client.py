"""Raw Gmail API client -- listing matching messages and creating (never
sending) draft replies. Shared by both the on-demand @tool wrappers
(tooling/gmail_tools.py, for "check my email now" mid-chat) and the
autonomous gmail_poll hook (hooks/gmail.py) -- same split already used
for arXiv/PubMed (arxiv_search.py / literature_tools.py).

Scoped to gmail.readonly + gmail.compose only (see
scripts/setup_google_oauth.py) -- this module can list/read mail and
create drafts, but has no way to send, delete, or modify existing mail
even if it tried to. Credential loading/refresh itself lives in
google_auth.py, shared with calendar_client.py -- one consent flow grants
both connectors' scopes together.
"""
import base64
import email.mime.text
import email.utils
import logging
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from .google_auth import GoogleAuthError, get_credentials
except ImportError:
    from google_auth import GoogleAuthError, get_credentials

# Re-exported under its old name -- every existing call site in this
# codebase (gmail_tools.py, hooks/gmail.py) already catches GmailAuthError
# by name; this alias means calendar_client.py sharing the same
# GoogleAuthError under the hood doesn't force renaming those call sites
# too, while a NEW caller can import GoogleAuthError directly if it wants
# the connector-agnostic name.
GmailAuthError = GoogleAuthError

load_dotenv()

log = logging.getLogger(__name__)


def _service(user_id: str):
    return build("gmail", "v1", credentials=get_credentials(user_id), cache_discovery=False)


def list_matching_messages(
        user_id: str, query: str, max_results: int = 10,
) -> list[dict[str, Any]]:
    """Lists messages matching a Gmail search query (same syntax as the
    Gmail search box, e.g. "is:unread from:boss@example.com"), newest
    first, normalized to {id, thread_id, subject, from, snippet,
    internal_date} -- internal_date is a UTC datetime, for the caller
    (hooks/gmail.py) to compare against its own dedup watermark. Fetches
    each match with format='metadata' and a narrow header list to keep
    API quota cost low -- never the full message body here; drafting a
    reply (create_draft_reply) fetches whatever more it needs separately,
    only for the one message actually being replied to."""
    service = _service(user_id)
    try:
        listing = service.users().messages().list(
            userId="me", q=query, maxResults=max_results,
        ).execute()
    except HttpError as e:
        raise GmailAuthError(f"Gmail search failed: {e}") from e

    results = []
    for stub in listing.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=stub["id"], format="metadata",
            metadataHeaders=["Subject", "From"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        results.append({
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", "(unknown sender)"),
            "snippet": msg.get("snippet", ""),
            "internal_date": datetime.fromtimestamp(
                int(msg["internalDate"]) / 1000, tz=timezone.utc
            ),
        })
    return results


def create_draft_reply(user_id: str, message_id: str, body: str) -> str:
    """Creates an in-thread draft reply to message_id -- never sends.
    Fetches the original's Subject/From/Message-ID/References headers to
    build a proper threaded reply (In-Reply-To/References set correctly,
    Subject prefixed with "Re:" only if not already present, To set to
    the original sender). Returns the new draft's id."""
    service = _service(user_id)
    try:
        original = service.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["Subject", "From", "Message-ID", "References"],
        ).execute()
    except HttpError as e:
        raise GmailAuthError(f"Couldn't load the message being replied to: {e}") from e

    headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}
    subject = headers.get("Subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    original_message_id = headers.get("Message-ID", "")
    references = " ".join(filter(None, [headers.get("References", ""), original_message_id])).strip()

    mime = email.mime.text.MIMEText(body)
    mime["To"] = headers.get("From", "")
    mime["Subject"] = subject
    if original_message_id:
        mime["In-Reply-To"] = original_message_id
    if references:
        mime["References"] = references

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    try:
        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw, "threadId": original["threadId"]}},
        ).execute()
    except HttpError as e:
        raise GmailAuthError(f"Couldn't create the draft: {e}") from e
    return draft["id"]
