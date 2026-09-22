import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

try:
    from .registry import tool
    from ..context import current_chat_id, current_user_id, run_on_main_loop
    from ._tooling_deps.gmail_client import GmailAuthError, create_draft_reply, list_matching_messages
except ImportError:
    from registry import tool
    from context import current_chat_id, current_user_id, run_on_main_loop
    from _tooling_deps.gmail_client import GmailAuthError, create_draft_reply, list_matching_messages


@tool
def check_gmail(query: str = "is:unread", max_results: Optional[int] = 10) -> list[dict[str, Any]] | str:
    """
    Searches this user's Gmail right now, on demand -- for "check my
    email"/"do I have anything from X" mid-conversation, not for ongoing
    monitoring (use schedule_gmail_check for that). query uses normal
    Gmail search syntax (e.g. "is:unread from:boss@example.com",
    "subject:invoice newer_than:2d"). Returns each match's id (pass to
    draft_gmail_reply), subject, sender, a short snippet, and when it
    arrived -- never the full body.

    Returns a plain string explaining what to do if this user hasn't
    connected Gmail yet (run scripts/setup_google_oauth.py) or if their
    access needs reconnecting, rather than an error -- surface that
    message to the user as-is.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."
    try:
        messages = run_on_main_loop(
            asyncio.to_thread(list_matching_messages, user_id, query, max_results or 10)
        )
        return [{**m, "internal_date": m["internal_date"].isoformat()} for m in messages]
    except GmailAuthError as e:
        return str(e)


@tool
def draft_gmail_reply(message_id: str, body: str) -> str:
    """
    Creates a draft reply to one Gmail message (by id, from check_gmail or
    a gmail_poll hook's match) -- ONLY ever creates a draft for the user
    to review and send themselves. This can never send an email; there is
    no send capability at all, by design.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."
    try:
        draft_id = run_on_main_loop(asyncio.to_thread(create_draft_reply, user_id, message_id, body))
        return f"Draft created (id={draft_id}) -- the user still needs to review and send it themselves."
    except GmailAuthError as e:
        return str(e)


@tool
def schedule_gmail_check(
        query: str,
        first_trigger_in_minutes: int,
        repeat_every_minutes: Optional[int] = None,
        on_match: Literal["notify", "draft", "both"] = "notify",
) -> str:
    """
    Sets up an ongoing Gmail check that fires on its own on a schedule,
    independent of this conversation -- for "let me know when I get an
    email from X" or "draft replies to anything matching Y automatically".
    query uses normal Gmail search syntax, same as check_gmail. Only ever
    considers mail that arrived since the last time this check fired --
    it won't re-notify about the same message twice.

    on_match controls what happens when new matching mail is found:
      "notify" -- just tell the user about it (default, no model call,
                  no draft).
      "draft"  -- silently draft a reply for each match (never sends,
                  never messages the user unless drafting itself fails).
      "both"   -- draft a reply AND tell the user it did so.

    repeat_every_minutes: how often to re-check (e.g. 15 for every 15
    minutes); omit for a one-time check. A very short interval wastes
    Gmail API quota for no benefit -- prefer 15+ minutes unless the user
    specifically asks for tighter latency.
    """
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()
    if not user_id or not chat_id:
        return "No active user/chat context."
    if on_match not in ("notify", "draft", "both"):
        return "on_match must be one of: notify, draft, both."

    try:
        from ..memory.db import create_hook
        from ..memory.plans import render_plan_markdown
    except ImportError:
        from memory.db import create_hook
        from memory.plans import render_plan_markdown

    next_trigger_at = datetime.now(timezone.utc) + timedelta(minutes=first_trigger_in_minutes)
    recurrence_seconds = repeat_every_minutes * 60 if repeat_every_minutes else None
    payload = {"query": query, "on_match": on_match, "last_seen_internal_date": None}

    new_id = run_on_main_loop(
        create_hook(
            user_id, chat_id, payload, next_trigger_at, recurrence_seconds, kind="gmail_poll",
        )
    )
    # Reminder-only in today's plan markdown (memory/plans.py) -- a Gmail
    # check has no business in the reminders-and-habits text, so this is
    # NOT a call to render_plan_markdown, unlike schedule_reminder.
    return (
        f"Scheduled (id={new_id}): Gmail check for {query!r}, on_match={on_match!r}, "
        f"first check at {next_trigger_at.isoformat()}."
    )


@tool
def list_gmail_hooks() -> list[dict[str, Any]] | str:
    """
    Lists every active scheduled Gmail check for this user -- use before
    scheduling a new one to avoid duplicates, or whenever the user asks
    what's currently set up.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."

    try:
        from ..memory.db import get_active_hooks
    except ImportError:
        from memory.db import get_active_hooks

    rows = run_on_main_loop(get_active_hooks(user_id, kind="gmail_poll"))
    return [
        {
            "id": r["id"],
            "query": (r["payload"] or {}).get("query"),
            "on_match": (r["payload"] or {}).get("on_match"),
            "next_trigger_at": r["next_trigger_at"].isoformat(),
            "recurrence_seconds": r["recurrence_seconds"],
        }
        for r in rows
    ]


@tool
def cancel_gmail_hook(id: int) -> str:
    """
    Cancels one scheduled Gmail check by its id (from list_gmail_hooks).
    Has no effect on reminders or on other users' hooks.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."

    try:
        from ..memory.db import deactivate_hook
    except ImportError:
        from memory.db import deactivate_hook

    cancelled = run_on_main_loop(deactivate_hook(id, user_id=user_id, kind="gmail_poll"))
    if not cancelled:
        return f"No active Gmail check with id={id} found for this user."
    return f"Cancelled Gmail check id={id}."
