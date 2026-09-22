import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

try:
    from .registry import tool
    from ..context import current_user_id, run_on_main_loop
    from ._tooling_deps.calendar_client import GoogleAuthError, create_event, list_upcoming_events
except ImportError:
    from registry import tool
    from context import current_user_id, run_on_main_loop
    from _tooling_deps.calendar_client import GoogleAuthError, create_event, list_upcoming_events


def _parse_local(value: str) -> datetime:
    """Parses an ISO 8601 datetime string; if it has no timezone offset,
    assumes this machine's own local timezone -- same convention
    prompts.current_datetime_note() already establishes (the model is
    always told the current time in local, not UTC, terms)."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


@tool
def list_calendar_events(
        time_min: Optional[str] = None, time_max: Optional[str] = None, max_results: Optional[int] = 10,
) -> list[dict[str, Any]] | str:
    """
    Lists this user's upcoming Google Calendar events -- for "what's on my
    calendar today/this week", or checking for conflicts before proposing
    a time for something. time_min/time_max are ISO 8601 datetimes
    (default: now through 7 days out) -- if given without a timezone
    offset, assumed to be this server's local time. Returns each event's
    id, summary, start, end, description, and location.

    Returns a plain string explaining what to do if this user hasn't
    connected Google Calendar yet (run scripts/setup_google_oauth.py) or
    if their access needs reconnecting, rather than an error -- surface
    that message to the user as-is.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."
    try:
        parsed_min = _parse_local(time_min) if time_min else None
        parsed_max = _parse_local(time_max) if time_max else None
        events = run_on_main_loop(
            asyncio.to_thread(list_upcoming_events, user_id, parsed_min, parsed_max, max_results or 10)
        )
        return events
    except GoogleAuthError as e:
        return str(e)


@tool
def create_calendar_event(
        summary: str, start: str, end: str,
        description: Optional[str] = None, location: Optional[str] = None,
) -> dict[str, Any] | str:
    """
    Creates an event on this user's Google Calendar -- for "block 2-3pm
    for the gym" or "add a meeting with X tomorrow at 10am". start/end
    are ISO 8601 datetimes -- if given without a timezone offset, assumed
    to be this server's local time. Consider calling list_calendar_events
    first to check for conflicts before creating something in a busy
    slot, unless the user has been explicit that they want it regardless.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."
    try:
        event = run_on_main_loop(
            asyncio.to_thread(
                create_event, user_id, summary, _parse_local(start), _parse_local(end),
                description, location,
            )
        )
        return event
    except GoogleAuthError as e:
        return str(e)
