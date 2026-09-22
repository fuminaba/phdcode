from pathlib import Path
from typing import Any, Optional
from uuid import UUID

try:
    from .registry import tool
except ImportError:
    from registry import tool

try:
    from ..context import current_conversation_id, current_user_id, run_on_main_loop
except ImportError:
    from context import current_conversation_id, current_user_id, run_on_main_loop

_MAX_TRANSCRIPT_CHARS = 8000


@tool
def search_memory(query: str, max_results: Optional[int] = 5) -> list[dict[str, Any]]:
    """
    Searches the current user's past, summarized conversations for ones
    relevant to `query` -- coarse, whole-conversation matching (hybrid:
    semantic + keyword). Returns each match's summary, when it happened,
    and its transcript file_path. If a summary doesn't have enough
    detail, call read_conversation with that file_path to read the full
    conversation, or search_turns for a specific remembered exchange
    instead of a whole conversation's summary. Only covers conversations
    that have already been archived -- for something from the still-open
    current conversation, use recall_conversation_history instead.
    """
    user_id = current_user_id.get()
    if not user_id:
        return []

    # Lazy, on-call imports: embeddings.py and memory/db.py both transitively
    # import tgi.py, which imports this module's package (tooling) as part of
    # ITS OWN module-level imports — importing them at module level here
    # would be a circular import. By call time, everything is fully loaded.
    try:
        from ..embeddings import embed
        from ..memory.db import search_conversations
    except ImportError:
        from embeddings import embed
        from memory.db import search_conversations

    query_embedding = embed(query)
    matches = run_on_main_loop(search_conversations(user_id, query, query_embedding, max_results or 5))
    return [
        {
            "conversation_id": m["id"],
            "summary": m["summary"],
            "file_path": m["file_path"],
            "created_at": m["created_at"].isoformat(),
            "relevance": round(m["score"], 4),
        }
        for m in matches
    ]


@tool
def search_turns(query: str, max_results: Optional[int] = 5) -> list[dict[str, Any]]:
    """
    Fine-grained search across individual past exchanges (both what the
    user said and what you answered), across every conversation --
    archived or still open. Use this for a specific remembered message or
    answer; use search_memory instead for "which past conversation was
    this in" at a whole-conversation level. Returns each match's turn_id,
    conversation_id, the full user_prompt and output for that exchange
    (whichever side matched, you get both, since the other side is the
    context that makes the match useful), when it happened, and a
    relevance score.
    """
    user_id = current_user_id.get()
    if not user_id:
        return []

    try:
        from ..embeddings import embed
        from ..memory.db import search_turns as _search_turns
    except ImportError:
        from embeddings import embed
        from memory.db import search_turns as _search_turns

    query_embedding = embed(query)
    matches = run_on_main_loop(_search_turns(user_id, query, query_embedding, max_results or 5))
    return [
        {
            "turn_id": str(m["turn_id"]) if m["turn_id"] else None,
            "conversation_id": m["conversation_id"],
            "user_prompt": m["user_prompt"],
            "output": m["output"],
            "created_at": m["created_at"].isoformat(),
            "relevance": round(m["score"], 4),
            "matched_side": m["matched_side"],  # 'prompt' or 'output' -- which side actually matched
        }
        for m in matches
    ]


@tool
def read_conversation(file_path: str) -> str:
    """
    Reads the full markdown transcript of one of the current user's past,
    already-archived conversations, given its file_path from a
    search_memory result, when that result's summary doesn't have enough
    detail. For something said earlier in THIS SAME, still-open
    conversation, use recall_conversation_history instead -- that's the
    live conversation's own history, not an archived one.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."

    try:
        from ..memory.transcript import TRANSCRIPTS_DIR
    except ImportError:
        from memory.transcript import TRANSCRIPTS_DIR

    # Defense in depth: even if the model hallucinates or is steered toward
    # a path outside its own user, never read outside that user's directory.
    try:
        resolved = Path(file_path).resolve()
        allowed_root = (TRANSCRIPTS_DIR / str(user_id)).resolve()
        resolved.relative_to(allowed_root)
    except (ValueError, OSError):
        return "Access denied: that file is not one of your conversations."

    if not resolved.exists():
        return "That conversation transcript no longer exists."

    text = resolved.read_text()
    if len(text) > _MAX_TRANSCRIPT_CHARS:
        text = text[:_MAX_TRANSCRIPT_CHARS] + f"\n\n...(truncated, {len(text)} chars total)"
    return text


@tool
def recall_conversation_history(skip_recent: int, limit: int = 10) -> list[dict[str, Any]] | str:
    """
    Looks up older turns from THIS SAME, still-open conversation that have
    fallen out of your live history window -- each with its user_prompt
    and final answer, not the full tool-by-tool trace (see
    recall_turn_detail for that). Use this when you need something
    discussed earlier in this conversation that you can no longer see.

    skip_recent: how many of the most recent turns to skip -- you'll be
    told this number whenever it matters, since it's exactly how many
    you already have in view. limit: how many turns before that point to
    return, oldest-first. Each result's turn_id can be passed to
    recall_turn_detail if a turn's final answer alone isn't enough detail.
    """
    user_id = current_user_id.get()
    conversation_id = current_conversation_id.get()
    if not user_id or not conversation_id:
        return "No active conversation context."

    try:
        from ..memory.db import get_older_turns
    except ImportError:
        from memory.db import get_older_turns

    turns = run_on_main_loop(get_older_turns(conversation_id, skip_recent, limit or 10))
    return [
        {
            "turn_id": str(t["turn_id"]) if t["turn_id"] else None,
            "user_prompt": t["user_prompt"],
            "output": t["output"],
            "created_at": t["created_at"].isoformat(),
        }
        for t in turns
    ]


@tool
def recall_turn_detail(turn_id: str) -> list[dict[str, Any]] | str:
    """
    Returns the full, step-by-step action trace (every model/tool call, in
    order) for one specific turn. Use this only when a turn's short final
    answer (from recall_conversation_history) isn't enough -- e.g. you
    need to see exactly what a tool call returned, not just the
    conclusion drawn from it.

    turn_id must come from a prior recall_conversation_history result.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."

    try:
        from ..memory.db import get_turn_actions_for_turn
    except ImportError:
        from memory.db import get_turn_actions_for_turn

    try:
        turn_uuid = UUID(turn_id)
    except (ValueError, AttributeError, TypeError):
        return f"Invalid turn_id: {turn_id!r}."

    actions = run_on_main_loop(get_turn_actions_for_turn(turn_uuid, user_id))
    if not actions:
        return "No recorded actions for that turn_id -- check it came from recall_conversation_history."
    return [
        {
            "action_type": a["action_type"],
            "payload": a["payload"],
            "created_at": a["created_at"].isoformat(),
        }
        for a in actions
    ]
