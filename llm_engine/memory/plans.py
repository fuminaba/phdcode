"""Per-user plan markdown -- a human-readable rendering of their active
*reminder* hooks (see schema.sql's comment on the hooks table), not a
separate source of truth, and not every hook kind -- a gmail_poll hook
has no business showing up in a "reminders and habits" plan.
render_plan_markdown regenerates the file from the DB every time the
schedule changes (tooling/scheduling_tools.py's schedule_reminder/
cancel_reminder); read_plan_markdown just reads whatever's there, as
context for a firing reminder's own model call
(llm_engine.hooks.reminder / pipeline.handle_reminder_hook) -- never
hand-edited directly.
"""
import os
from pathlib import Path

try:
    from .db import get_active_hooks
except ImportError:
    from db import get_active_hooks

PLANS_DIR = Path(os.getenv("LOG_DIR", "logs")) / "plans"

_EMPTY_PLAN_TEXT = "No reminders or habits are currently scheduled.\n"


def plan_path(user_id: str) -> Path:
    return PLANS_DIR / f"{user_id}.md"


def read_plan_markdown(user_id: str) -> str:
    """Reads this user's current plan markdown fresh off disk, or a clear
    placeholder if nothing's been scheduled yet. Never cached in memory --
    always the latest render."""
    path = plan_path(user_id)
    if not path.exists():
        return _EMPTY_PLAN_TEXT
    return path.read_text() or _EMPTY_PLAN_TEXT


async def render_plan_markdown(user_id: str) -> str:
    """Regenerates this user's plan markdown from their live reminder
    hooks and writes it to disk. Returns the rendered text (callers don't
    need it today, but it's handy for tests/logging)."""
    rows = await get_active_hooks(user_id, kind="reminder")
    path = plan_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        text = _EMPTY_PLAN_TEXT
    else:
        lines = ["# Scheduled reminders and habits", ""]
        for r in rows:
            intent = (r["payload"] or {}).get("intent", "(no description)")
            when = r["next_trigger_at"].strftime("%Y-%m-%d %H:%M %Z")
            seconds = r["recurrence_seconds"]
            if not seconds:
                recurrence = "one-time"
            elif seconds == 86400:
                recurrence = "repeats daily"
            else:
                recurrence = f"repeats every {seconds / 3600:.1f}h"
            lines.append(f"- [{r['id']}] {intent} -- next: {when} ({recurrence})")
        text = "\n".join(lines) + "\n"

    path.write_text(text)
    return text
