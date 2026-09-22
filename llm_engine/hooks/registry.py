"""Hook-kind dispatch registry -- modeled directly on
tooling/registry.py's @tool decorator, but simpler: a hook handler isn't
model-facing, so there's no schema to derive from type hints/docstrings,
just a plain kind -> async handler mapping.

Adding a new hook kind is: write an async handler function taking one due
`hooks` row (dict) and returning a HookResult, decorate it with
@hook_handler("your_kind"), import that module from this package's
__init__.py (same hardcoded-import convention tooling/__init__.py already
uses), and widen the CHECK constraint on hooks.kind in schema.sql. Nothing
else -- webhook_server.py's ticker dispatches by kind generically, it never
needs to know a new kind exists.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional

HOOK_HANDLERS: dict[str, Callable[[dict], Awaitable["HookResult"]]] = {}


@dataclass
class HookResult:
    """What a hook handler's firing produced. message is the text to send
    over Telegram, or None if there's nothing worth telling the user this
    time -- e.g. a gmail_poll hook that silently drafted a reply and has
    no need to interrupt anyone. Generalizes handle_reminder_hook's old
    ad hoc skip_send boolean into something every hook kind can use, not
    just reminders.

    reschedule_to overrides the ticker's normal recurrence math for this
    one firing -- set it when the handler deliberately did NOT do its
    real work this tick (e.g. the reminder hook finding the user in a
    calendar meeting right now) and wants to be tried again at a specific
    time instead, without consuming this cycle's normal recurrence step
    or being deactivated as if it fired for real. None (the default)
    means "use the normal recurrence/deactivation logic," unaffected --
    every existing hook kind that doesn't set this behaves exactly as
    before this field existed."""
    message: Optional[str] = None
    reschedule_to: Optional[datetime] = None


def hook_handler(kind: str) -> Callable:
    """Registers fn as the handler for hooks.kind == kind. Raises if kind
    is already registered -- a silent overwrite here would mean one kind's
    hooks quietly stop firing the way their creator expects, exactly the
    kind of bug that's easy to miss and painful to debug later."""
    def decorator(fn: Callable[[dict], Awaitable[HookResult]]) -> Callable:
        if kind in HOOK_HANDLERS:
            raise ValueError(f"Hook kind {kind!r} is already registered to {HOOK_HANDLERS[kind]!r}")
        HOOK_HANDLERS[kind] = fn
        return fn
    return decorator
