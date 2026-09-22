"""One-time (or re-runnable) backfill: embeds prompt_embedding/
output_embedding for every conversation_turns row that predates those
columns, so search_turns (llm_engine/tooling/memory_tools.py) covers
existing history, not just turns saved going forward.

Idempotent -- only ever touches rows where either embedding is still
NULL, so a partial/interrupted run can just be rerun. Safe to run
against a live system: embed() calls Ollama directly (never through
Celery -- no tool's embedding calls ever have, this isn't special),
same as every other search tool already does, so it doesn't contend
with the chat worker's GPU serialization.

Usage:
    python scripts/backfill_turn_embeddings.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from llm_engine.embeddings import embed
from llm_engine.memory import get_turns_missing_embeddings, init_schema, update_turn_embeddings

_BATCH_SIZE = 100


def _embed_or_none(text: str, label: str, row_id: int) -> list[float] | None:
    if not text:
        return None
    try:
        return embed(text)
    except Exception as e:
        print(f"  row {row_id}: couldn't embed {label}: {e}")
        return None


async def main() -> None:
    await init_schema()
    total_done = 0
    while True:
        rows = await get_turns_missing_embeddings(_BATCH_SIZE)
        if not rows:
            break
        for row in rows:
            output = row["output"]
            output_text = output if isinstance(output, str) else (json.dumps(output) if output else None)
            prompt_embedding = await asyncio.to_thread(_embed_or_none, row["user_prompt"], "prompt", row["id"])
            output_embedding = await asyncio.to_thread(_embed_or_none, output_text, "output", row["id"]) if output_text else None
            await update_turn_embeddings(row["id"], prompt_embedding, output_embedding)
            total_done += 1
            print(f"  backfilled row {row['id']} ({total_done} so far)")

    print(f"Done. {total_done} row(s) backfilled.")


if __name__ == "__main__":
    asyncio.run(main())
