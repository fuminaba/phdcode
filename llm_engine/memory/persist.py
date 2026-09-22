import asyncio
import json
import logging
from typing import Any, Optional
from uuid import UUID

from .db import save_turn
from .transcript import ConversationTranscript

log = logging.getLogger(__name__)


def _embed_for_turn(user_prompt: str, result: dict[str, Any]) -> tuple[Optional[list[float]], Optional[list[float]]]:
    """Embeds the prompt and output separately (search_turns' whole
    reason for existing: either side of an exchange can be the match).
    Best-effort -- if embedding fails (Ollama down, whatever), the turn
    still gets saved via save_turn's own optional params, just without
    search_turns coverage until backfilled; a failure here must never
    lose the turn itself. output can be non-string (as_json=True
    responses) -- json.dumps it first, same fallback _turns_to_history
    already uses for that case."""
    try:
        from ..embeddings import embed
    except ImportError:
        from embeddings import embed

    try:
        prompt_embedding = embed(user_prompt)
    except Exception as e:
        log.warning(f"Couldn't embed turn prompt: {e}")
        prompt_embedding = None

    output = result.get("output")
    if not output:
        output_embedding = None
    else:
        output_text = output if isinstance(output, str) else json.dumps(output)
        try:
            output_embedding = embed(output_text)
        except Exception as e:
            log.warning(f"Couldn't embed turn output: {e}")
            output_embedding = None

    return prompt_embedding, output_embedding


async def persist_turn(
        transcript: ConversationTranscript,
        conversation_id: int,
        user_id: str,
        user_prompt: str,
        result: dict[str, Any],
        model_name: Optional[str] = None,
        turn_id: Optional[UUID] = None,
) -> None:
    """Writes a turn to the markdown transcript and its JSONB backup
    concurrently. Await this after replying to the user so persistence
    never adds to their perceived latency. turn_id links the saved row to
    its own detailed history in turn_actions (see memory.db.write_turn_action).

    Embeds user_prompt and result["output"] separately (off the event
    loop -- embed() is a blocking network call) before the DB write, so
    search_turns has fine-grained recall available immediately, not just
    once a conversation gets archived (see schema.sql's comment on
    conversation_turns' embedding columns)."""
    prompt_embedding, output_embedding = await asyncio.to_thread(_embed_for_turn, user_prompt, result)
    await asyncio.gather(
        transcript.append_turn(user_prompt, result, model_name),
        save_turn(
            conversation_id, user_id, user_prompt, result, model_name, turn_id=turn_id,
            prompt_embedding=prompt_embedding, output_embedding=output_embedding,
        ),
    )
