from typing import Optional
from uuid import UUID

try:
    from .base import ChannelFeedback
except ImportError:
    from base import ChannelFeedback


class LoggingFeedback(ChannelFeedback):
    """Wraps a real ChannelFeedback (TelegramFeedback, RedisRelayFeedback,
    ...) and, on every start()/progress()/finish() call, also writes a
    live turn_actions row (pipeline_start/pipeline_step/pipeline_end; see
    schema.sql's comment on turn_actions) before delegating to the inner
    feedback unchanged -- the actual user-facing behavior (a Telegram
    message, a relayed event, ...) is entirely untouched.

    Purpose-built for document_index.ingest_and_index_document specifically:
    it already calls start()/progress()/finish() at exactly the right
    pipeline-shaped moments, so wrapping its feedback argument once (see
    pipeline.handle_incoming_message) logs every stage with zero changes
    to document_index.py itself. Not used for the chat tool loop's own
    progress() calls -- those need richer, per-action payloads
    (tool name/arguments/result) than (label, fraction) can carry, so
    tgi._run_tool_loop logs llm_call/tool_call explicitly instead.
    """

    def __init__(self, turn_id: UUID, conversation_id: Optional[int], user_id: str, inner: ChannelFeedback):
        self._turn_id = turn_id
        self._conversation_id = conversation_id
        self._user_id = user_id
        self._inner = inner

    async def _log(self, action_type: str, payload: dict) -> None:
        try:
            from ..memory.db import write_turn_action
        except ImportError:
            from memory.db import write_turn_action
        # write_turn_action is itself already best-effort (never raises) --
        # see its own docstring.
        await write_turn_action(self._turn_id, self._conversation_id, self._user_id, action_type, payload)

    async def start(self, label: str) -> None:
        await self._log("pipeline_start", {"label": label})
        await self._inner.start(label)

    async def progress(self, label: str, fraction: float) -> None:
        await self._log("pipeline_step", {"label": label, "fraction": fraction})
        await self._inner.progress(label, fraction)

    async def finish(self, label: Optional[str] = None) -> None:
        status = "error" if label and label.startswith("❌") else "success"
        await self._log("pipeline_end", {"label": label, "status": status})
        await self._inner.finish(label)
