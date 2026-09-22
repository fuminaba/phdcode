"""Raw Google Calendar API client -- listing/creating events on the
user's primary calendar, and a busy-right-now check used by the
calendar-aware reminder hook (hooks/reminder.py). Same split as
gmail_client.py: this is the raw client, tooling/calendar_tools.py has
the model-facing @tool wrappers. Credential loading/refresh is shared
with gmail_client.py via google_auth.py -- one consent flow grants both
connectors' scopes together.

Scoped to calendar.events only (see scripts/setup_google_oauth.py) --
read/write access to events specifically, not calendar list management
or settings, and no ability to touch any calendar other than the
primary one.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from .google_auth import GoogleAuthError, get_credentials
except ImportError:
    from google_auth import GoogleAuthError, get_credentials

load_dotenv()

log = logging.getLogger(__name__)


def _service(user_id: str):
    return build("calendar", "v3", credentials=get_credentials(user_id), cache_discovery=False)


def _parse_event_time(value: dict) -> datetime:
    """Calendar events use either dateTime (timed events) or date
    (all-day events, date-only, no time component) -- normalize both to
    a timezone-aware datetime, treating an all-day event's date as
    midnight UTC start-of-day for comparison purposes."""
    if "dateTime" in value:
        return datetime.fromisoformat(value["dateTime"])
    return datetime.fromisoformat(value["date"]).replace(tzinfo=timezone.utc)


def _normalize_event(e: dict) -> dict[str, Any]:
    return {
        "id": e["id"],
        "summary": e.get("summary", "(no title)"),
        "start": _parse_event_time(e["start"]).isoformat(),
        "end": _parse_event_time(e["end"]).isoformat(),
        "description": e.get("description"),
        "location": e.get("location"),
    }


def list_upcoming_events(
        user_id: str, time_min: Optional[datetime] = None,
        time_max: Optional[datetime] = None, max_results: int = 10,
) -> list[dict[str, Any]]:
    """Lists events on the primary calendar between time_min and
    time_max (defaults: now through 7 days out), soonest first,
    normalized to {id, summary, start, end, description, location}.
    singleEvents=True expands recurring events into their individual
    occurrences within the window, rather than returning one row for the
    whole recurring series."""
    time_min = time_min or datetime.now(timezone.utc)
    time_max = time_max or (time_min + timedelta(days=7))
    service = _service(user_id)
    try:
        listing = service.events().list(
            calendarId="primary",
            timeMin=time_min.isoformat(), timeMax=time_max.isoformat(),
            maxResults=max_results, singleEvents=True, orderBy="startTime",
        ).execute()
    except HttpError as e:
        raise GoogleAuthError(f"Calendar lookup failed: {e}") from e
    return [_normalize_event(e) for e in listing.get("items", [])]


def create_event(
        user_id: str, summary: str, start: datetime, end: datetime,
        description: Optional[str] = None, location: Optional[str] = None,
) -> dict[str, Any]:
    """Creates an event on the primary calendar. start/end must be
    timezone-aware. Returns the normalized created event (see
    list_upcoming_events for the shape), including its id for any later
    reference."""
    service = _service(user_id)
    body = {
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    try:
        created = service.events().insert(calendarId="primary", body=body).execute()
    except HttpError as e:
        raise GoogleAuthError(f"Couldn't create the event: {e}") from e
    return _normalize_event(created)


def get_busy_until(user_id: str, at: Optional[datetime] = None) -> Optional[datetime]:
    """Returns the end time of whatever event is happening AT `at`
    (default: now) on the primary calendar, or None if nothing is. Used
    by hooks/reminder.py to defer a reminder rather than interrupt an
    ongoing meeting -- a narrow +/-1-minute window around `at` is enough
    to catch any event whose start/end overlaps this instant, without
    needing the separate freebusy endpoint/scope (calendar.events
    already covers this). If more than one event overlaps, returns the
    LATEST end time among them, so a reminder doesn't get rescheduled
    into the middle of a second overlapping meeting."""
    at = at or datetime.now(timezone.utc)
    service = _service(user_id)
    try:
        listing = service.events().list(
            calendarId="primary",
            timeMin=(at - timedelta(minutes=1)).isoformat(),
            timeMax=(at + timedelta(minutes=1)).isoformat(),
            singleEvents=True, orderBy="startTime",
        ).execute()
    except HttpError as e:
        raise GoogleAuthError(f"Calendar busy-check failed: {e}") from e

    busy_until = None
    for e in listing.get("items", []):
        start = _parse_event_time(e["start"])
        end = _parse_event_time(e["end"])
        if start <= at <= end:
            busy_until = max(busy_until, end) if busy_until else end
    return busy_until
