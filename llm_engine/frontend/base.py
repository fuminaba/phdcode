from typing import Optional


class ChannelFeedback:
    """Channel-agnostic progress/activity signal for a long-running
    operation (document ingestion, an agent turn, ...). Callers in
    pipeline.py / memory/document_index.py know nothing about *how* this
    gets surfaced -- they just call start()/progress()/finish() at the
    right points. Every method here is a no-op: this class doubles as the
    default "no feedback wired up" instance (call sites can do
    `feedback = feedback or ChannelFeedback()` and call unconditionally,
    no `if feedback:` checks scattered around), and as the base every real
    channel subclasses, overriding only what it actually surfaces.

    Telegram implements this today (see telegram.py). A future Django
    front end would implement the same interface however suits it (a
    websocket push, a polled status row, ...) -- pipeline.py and
    document_index.py would need zero changes.
    """

    async def start(self, label: str) -> None:
        """Called once, before the tracked operation begins."""

    async def progress(self, label: str, fraction: float) -> None:
        """Called at each stage boundary. fraction is in [0, 1]."""

    async def finish(self, label: Optional[str] = None) -> None:
        """Called once when the operation ends, success or failure.
        Callers should call this from a finally block."""
