import asyncio
from contextvars import ContextVar
from typing import Any, Coroutine, Optional

# Set by whoever is currently talking to the agent (e.g. webhook_server's
# handle_message) before invoking generate_with_tools, and read by any tool
# that needs to know "who is asking" (e.g. search_memory) — never exposed as
# a tool parameter, so the model itself can never set or override it.
current_user_id: ContextVar[Optional[str]] = ContextVar("current_user_id", default=None)

# Same idea, for tools that need to know which conversation is live right
# now (e.g. recall_conversation_history) without it ever being a
# model-settable tool parameter -- a conversation_id the model could point
# anywhere would defeat the point of scoping recall to "this conversation".
current_conversation_id: ContextVar[Optional[int]] = ContextVar("current_conversation_id", default=None)

# Same idea, for tools that need to know which channel thread to act on
# (e.g. schedule_reminder, which must store a chat_id the ticker can later
# send to -- independent of user_id, since a group chat's chat_id differs
# from any individual user_id within it) -- never model-settable, same as
# the other two.
current_chat_id: ContextVar[Optional[str]] = ContextVar("current_chat_id", default=None)

_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Call once, from inside the app's main event loop at startup (e.g. a
    post_init hook), so run_on_main_loop() has somewhere to dispatch to."""
    global _main_loop
    _main_loop = loop


def run_on_main_loop(coro: Coroutine) -> Any:
    """Runs an async coroutine on the main event loop and blocks for its
    result. For calling async code (e.g. asyncpg queries, bound to the main
    loop's connection pool) from inside a synchronous @tool function, which
    runs in a worker thread since generate_with_tools is invoked via
    asyncio.to_thread."""
    if _main_loop is None:
        raise RuntimeError("Main event loop not set — call set_main_loop() at startup.")
    return asyncio.run_coroutine_threadsafe(coro, _main_loop).result()
