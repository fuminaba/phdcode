"""The 'gmail_poll' hook kind -- checks for new mail matching a
user-configured query (see tooling/gmail_tools.py's schedule_gmail_check)
and, depending on payload["on_match"], notifies the user, silently drafts
a reply via pipeline.handle_gmail_reply_draft, or both. Listing mail is a
plain HTTPS call (asyncio.to_thread, no Celery/GPU involved); drafting a
reply is the one path that reaches the model, via the same
generate_with_tools -> Celery "chat" queue everything else already uses.
"""
import asyncio
from datetime import datetime, timedelta, timezone

try:
    from .registry import hook_handler, HookResult
    from .. import pipeline
    from ..memory.db import update_hook_payload
    from ..tooling._tooling_deps.gmail_client import GmailAuthError, list_matching_messages
except ImportError:
    from registry import hook_handler, HookResult
    import pipeline
    from memory.db import update_hook_payload
    from tooling._tooling_deps.gmail_client import GmailAuthError, list_matching_messages

# Caps both API quota use and how many reply-drafting model calls one tick
# can trigger -- any excess simply waits for next tick, since the
# last_seen_internal_date watermark only advances past what was actually
# processed this time.
_MAX_MATCHES_PER_TICK = 5
# How long to stay quiet about a persistent auth failure (e.g. an expired
# refresh token) before saying so again -- once per tick would be spam,
# never again would mean silently going dark. See docs/HOOKS_PLAN.md's
# note on Google's 7-day refresh-token cap for "Testing"-status apps.
_AUTH_ERROR_RENOTIFY_AFTER = timedelta(hours=24)


@hook_handler("gmail_poll")
async def handle_gmail_poll(row: dict) -> HookResult:
    payload = dict(row["payload"] or {})
    query = payload.get("query", "")
    on_match = payload.get("on_match", "notify")
    last_seen_str = payload.get("last_seen_internal_date")
    last_seen = datetime.fromisoformat(last_seen_str) if last_seen_str else None

    try:
        matches = await asyncio.to_thread(
            list_matching_messages, row["user_id"], query, _MAX_MATCHES_PER_TICK,
        )
    except GmailAuthError as e:
        return await _handle_auth_error(row["id"], payload, str(e))

    if last_seen is not None:
        matches = [m for m in matches if m["internal_date"] > last_seen]
    if not matches:
        return HookResult(message=None)

    # Advance the watermark to the newest match actually seen this tick --
    # if more than _MAX_MATCHES_PER_TICK arrived, the rest wait for next
    # tick rather than being skipped outright.
    matches.sort(key=lambda m: m["internal_date"])
    payload["last_seen_internal_date"] = matches[-1]["internal_date"].isoformat()
    payload.pop("last_auth_error_notified_at", None)
    await update_hook_payload(row["id"], payload)

    if on_match in ("draft", "both"):
        for m in matches:
            await pipeline.handle_gmail_reply_draft(row["user_id"], row["chat_id"], m)

    if on_match == "draft":
        # Silent by design -- the user asked to be left alone unless
        # drafting itself needs their attention, which surfaces via a
        # GmailAuthError above, not here.
        return HookResult(message=None)

    lines = [f"\U0001F4EC {len(matches)} new email(s) matching {query!r}:"]
    for m in matches:
        lines.append(f"- {m['subject']!r} from {m['from']}")
    if on_match == "both":
        lines.append("(drafted a reply to each -- review in Gmail before sending, nothing was sent)")
    return HookResult(message="\n".join(lines))


async def _handle_auth_error(hook_id: int, payload: dict, error_text: str) -> HookResult:
    last_notified_str = payload.get("last_auth_error_notified_at")
    now = datetime.now(timezone.utc)
    if last_notified_str and now - datetime.fromisoformat(last_notified_str) < _AUTH_ERROR_RENOTIFY_AFTER:
        return HookResult(message=None)
    payload["last_auth_error_notified_at"] = now.isoformat()
    await update_hook_payload(hook_id, payload)
    return HookResult(message=f"⚠️ Gmail check failed: {error_text}")
