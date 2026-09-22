from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    from .registry import tool
    from ..context import current_chat_id, current_user_id, run_on_main_loop
except ImportError:
    from registry import tool
    from context import current_chat_id, current_user_id, run_on_main_loop


@tool
def schedule_reminder(
        intent: str,
        first_trigger_in_minutes: int,
        repeat_every_minutes: Optional[int] = None,
) -> str:
    """
    Schedules a reminder or habit that fires on its own later, independent
    of this conversation -- the user doesn't need to be chatting for it to
    go off. At fire time, a fresh model call composes the actual message
    using this intent plus the user's full plan and their recent activity,
    so phrase intent as a short description of what this is about, not
    the literal text to send -- e.g. "remind me to take my vitamins", not
    "Don't forget your vitamins!".

    first_trigger_in_minutes: minutes from now until the first firing.
    repeat_every_minutes: if set, refires this often after each firing
        (e.g. 1440 for daily); omit for a one-time reminder.
    """
    user_id = current_user_id.get()
    chat_id = current_chat_id.get()
    if not user_id or not chat_id:
        return "No active user/chat context."

    try:
        from ..memory.db import create_hook
        from ..memory.plans import render_plan_markdown
    except ImportError:
        from memory.db import create_hook
        from memory.plans import render_plan_markdown

    next_trigger_at = datetime.now(timezone.utc) + timedelta(minutes=first_trigger_in_minutes)
    recurrence_seconds = repeat_every_minutes * 60 if repeat_every_minutes else None

    new_id = run_on_main_loop(
        create_hook(
            user_id, chat_id, {"intent": intent}, next_trigger_at, recurrence_seconds,
            kind="reminder",
        )
    )
    run_on_main_loop(render_plan_markdown(user_id))
    return f"Scheduled (id={new_id}): {intent!r}, first firing at {next_trigger_at.isoformat()}."


@tool
def list_scheduled_reminders() -> list[dict[str, Any]] | str:
    """
    Lists every active reminder/habit currently scheduled for this user --
    use this before scheduling a new one to avoid duplicates, or whenever
    the user asks what's currently set up.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."

    try:
        from ..memory.db import get_active_hooks
    except ImportError:
        from memory.db import get_active_hooks

    rows = run_on_main_loop(get_active_hooks(user_id, kind="reminder"))
    return [
        {
            "id": r["id"],
            "intent": (r["payload"] or {}).get("intent"),
            "next_trigger_at": r["next_trigger_at"].isoformat(),
            "recurrence_seconds": r["recurrence_seconds"],
        }
        for r in rows
    ]


@tool
def cancel_reminder(id: int) -> str:
    """
    Cancels one scheduled reminder/habit by its id (from
    list_scheduled_reminders). Has no effect on reminders belonging to
    other users.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."

    try:
        from ..memory.db import deactivate_hook
        from ..memory.plans import render_plan_markdown
    except ImportError:
        from memory.db import deactivate_hook
        from memory.plans import render_plan_markdown

    cancelled = run_on_main_loop(deactivate_hook(id, user_id=user_id, kind="reminder"))
    if not cancelled:
        return f"No active reminder with id={id} found for this user."
    run_on_main_loop(render_plan_markdown(user_id))
    return f"Cancelled reminder id={id}."
