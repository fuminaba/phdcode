import asyncio
import json
from typing import Any, Optional

try:
    from ..embeddings import embed
    from ..prompts import summarize_prompt
    from ..tgi import GenerativeService
except ImportError:
    from embeddings import embed
    from prompts import summarize_prompt
    from tgi import GenerativeService

from .db import get_turns, update_conversation_summary


def _output_text(result: dict[str, Any]) -> str:
    output = result.get("output")
    return output if isinstance(output, str) else json.dumps(output)


async def summarize_and_embed_conversation(conversation_id: int, model_name: Optional[str] = None) -> str:
    """Summarizes a conversation's turns into the structured template (see
    prompts.summarize_prompt) and embeds that summary, writing both back
    onto its `conversations` row. Safe to call on an open conversation (e.g.
    a manual /summarize) or a just-closed one (e.g. archiving on /start) —
    it only ever overwrites summary/embedding, never touches the turns.

    Raises ValueError if the conversation has no turns yet.
    """
    turns = await get_turns(conversation_id)
    if not turns:
        raise ValueError(f"Conversation {conversation_id} has no turns to summarize.")

    conversation_text = "\n\n".join(
        f"User: {turn['user_prompt']}\nAssistant: {_output_text(turn['result'])}"
        for turn in turns
    )

    generation = await asyncio.to_thread(
        GenerativeService.generate_text,
        summarize_prompt(conversation_text),
        model_name=model_name or "qwen3:14b",
    )
    summary = generation["output"]

    embedding = await asyncio.to_thread(embed, summary)

    await update_conversation_summary(conversation_id, summary, embedding)
    return summary
