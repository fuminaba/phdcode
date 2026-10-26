"""The briefs (ADR-0032): the prose that a model writes at the top of a page.
One row of the table briefs for each key:

  interests        the user's interests, from the library, with the keywords
                   in bold (the page Interests)
  triage           which papers in triage to read first, and why (the page Triage)
  digest:<date>    what happened in the field in the papers of that day of
                   the digest, and which matter most (the page Digests)

The sweep writes all three after its digest (BRIEF_MODEL); a button on each
page writes one again with a model of the menu. A brief that fails keeps the
old one.

The pertinence of a paper is its hybrid score against the interests brief
(db.pertinence); it orders the papers of the other two briefs and of their
pages.
"""
import asyncio
import os
import re
from datetime import date
from typing import Any, Optional

from .embeddings import embed
from .generation import generate
from .memory import get_pool, list_saved_searches, pertinence
from .memory.db import TRIAGE_OF_PAPER
from .prompts import digest_brief_prompt, interests_brief_prompt, triage_brief_prompt
from .sweep import DIGEST_DIR, parse_digest

# The model of the briefs: a route of LLM_ROUTING. A hosted model by default,
# so that the briefs do not wait for the GPU.
BRIEF_MODEL = os.getenv("BRIEF_MODEL") or "claude-haiku-5-5"
# The papers that a triage or digest brief reads, the most pertinent first.
MAX_PAPERS = 20
ABSTRACT_CHARS = 1500
_DAY = re.compile(r"digest:(\d{4}-\d{2}-\d{2})")


async def get(key: str) -> Optional[dict[str, Any]]:
    row = await (await get_pool()).fetchrow("SELECT key, text, model, written_at FROM briefs WHERE key = $1", key)
    return dict(row) if row else None


async def _save(key: str, text: str, model: str, embedding: Optional[list[float]] = None) -> None:
    await (await get_pool()).execute(
        "INSERT INTO briefs (key, text, model, embedding) VALUES ($1, $2, $3, $4) ON CONFLICT (key) DO UPDATE "
        "SET text = $2, model = $3, embedding = $4, written_at = now()", key, text, model, embedding)


async def scores(paper_ids: list[int]) -> dict[int, float]:
    """The pertinence of each paper; empty while there is no interests brief."""
    row = await (await get_pool()).fetchrow("SELECT text, embedding FROM briefs WHERE key = 'interests'")
    if row is None or row["embedding"] is None:
        return {}
    return await pertinence(row["text"].replace("**", ""), row["embedding"], paper_ids)


async def day_paper_ids(day: str) -> list[int]:
    """The stored papers of the relevant hits of one digest file."""
    path = DIGEST_DIR / f"{day}.md"
    if not path.is_file():
        return []
    refs = list({h["ref"] for sweep in parse_digest(path.read_text()) for s in sweep["searches"] for h in s["hits"]})
    rows = await (await get_pool()).fetch(
        "SELECT id FROM papers WHERE source || ':' || source_id = ANY($1) AND deleted_at IS NULL", refs)
    return [row["id"] for row in rows]


async def _papers_text(paper_ids: list[int]) -> str:
    """The MAX_PAPERS most pertinent of these papers, for a prompt."""
    pertinent = await scores(paper_ids)
    rows = await (await get_pool()).fetch(
        f"SELECT p.id, p.title, p.abstract, {TRIAGE_OF_PAPER} AS triage FROM papers p WHERE p.id = ANY($1)", paper_ids)
    rows = sorted(rows, key=lambda r: -pertinent.get(r["id"], 0))[:MAX_PAPERS]
    return "\n\n".join(
        f"[P{r['id']}] {r['title']}\nPertinence: {pertinent.get(r['id'], 0):.2f}; "
        f"triage: {'%.2f' % r['triage'] if r['triage'] is not None else 'none'}\n"
        f"Abstract: {(r['abstract'] or '(none)')[:ABSTRACT_CHARS]}" for r in rows)


async def _interests_text() -> str:
    brief = await get("interests")
    if brief is None:
        raise ValueError("Write the interests brief first (the page Interests).")
    return brief["text"]


async def write(key: str, model: Optional[str] = None) -> None:
    """Writes the brief of this key again, through model (default BRIEF_MODEL)."""
    model = model or BRIEF_MODEL
    pool = await get_pool()
    if key == "interests":
        rows = await pool.fetch("SELECT title, importance FROM papers "
                                "WHERE origin IS NULL AND title IS NOT NULL AND deleted_at IS NULL ORDER BY id")
        if not rows:
            raise ValueError("The library is empty.")
        library = "\n".join(f"- {'* ' if r['importance'] in ('crucial', 'recommended') else ''}{r['title']}"
                            for r in rows)
        searches = "\n".join(f"- {s['interest']}" for s in await list_saved_searches()) or "(none)"
        text = await asyncio.to_thread(generate, interests_brief_prompt(library, searches), model)
        [embedding] = await asyncio.to_thread(embed, [text.replace("**", "")])
        return await _save(key, text, model, embedding)
    if key == "triage":
        ids = [r["id"] for r in await pool.fetch("SELECT id FROM papers WHERE origin = 'sweep' AND deleted_at IS NULL")]
        if not ids:
            return await _save(key, "No papers wait in triage.", model)
        prompt = triage_brief_prompt(await _interests_text(), await _papers_text(ids))
    elif m := _DAY.fullmatch(key):
        ids = await day_paper_ids(m[1])
        if not ids:
            return await _save(key, "No relevant papers on this day.", model)
        prompt = digest_brief_prompt(m[1], await _interests_text(), await _papers_text(ids))
    else:
        raise ValueError(f"No brief has the key {key!r}.")
    await _save(key, await asyncio.to_thread(generate, prompt, model), model)


async def write_after_sweep(day: date) -> list[str]:
    """The three briefs of a sweep, the interests first: the other two read
    them. Returns one line for each brief that failed."""
    failed = []
    for key in ("interests", f"digest:{day}", "triage"):
        try:
            await write(key)
        except Exception as e:  # the model server is stopped, or the library is empty
            failed.append(f"brief {key}: {type(e).__name__}: {e}")
    return failed
