"""The 'reminder' hook kind -- a thin adapter over pipeline.handle_reminder_hook,
which does the real reminder-composing work (unchanged from before hooks
existed as a concept). This file's own job is twofold: translating that
function's ad hoc skip_send boolean into the generic HookResult every
hook kind returns, and -- if Google Calendar is connected -- checking
whether the user is in a meeting right now before ever calling the model,
deferring instead of interrupting one."""
import asyncio
import logging
from datetime import timedelta

try:
    from .registry import hook_handler, HookResult
    from .. import pipeline
    from ..tooling._tooling_deps.calendar_client import GoogleAuthError, get_busy_until
except ImportError:
    from registry import hook_handler, HookResult
    import pipeline
    from tooling._tooling_deps.calendar_client import GoogleAuthError, get_busy_until

log = logging.getLogger(__name__)

# Fire just after a meeting actually ends, not exactly on the boundary --
# calendar event end times are frequently a little optimistic relative to
# when a meeting really wraps up.
_POST_MEETING_BUFFER = timedelta(minutes=1)


@hook_handler("reminder")
async def handle_reminder(row: dict) -> HookResult:
    user_id = row["user_id"]

    # Best-effort: Calendar not connected, or any lookup failure, means
    # "proceed as if not busy" -- a calendar-awareness feature must never
    # be able to break the reminder it's layered on top of. Only a
    # genuine busy-right-now result changes behavior.
    try:
        busy_until = await asyncio.to_thread(get_busy_until, user_id)
    except GoogleAuthError:
        busy_until = None
    except Exception as e:
        log.warning(f"calendar busy-check failed for user={user_id}, proceeding as not busy: {e}")
        busy_until = None

    if busy_until is not None:
        return HookResult(reschedule_to=busy_until + _POST_MEETING_BUFFER)

    intent = (row["payload"] or {}).get("intent", "(no description)")
    result = await pipeline.handle_reminder_hook(
        user_id=user_id, chat_id=row["chat_id"], intent=intent,
    )
    return HookResult(message=None if result.get("skip_send") else result.get("output"))
