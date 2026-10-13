"""The sweep: one pass over all saved searches (ADR-0015, item 6).

For each saved search, the sweep searches the source for the papers of the
last LOOKBACK_DAYS days, or since its last complete sweep if that is
earlier, and triage judges each hit that it has not judged before. A relevant hit (probability >= the threshold of the saved search) is
stored as an abstract-only paper and goes into the digest, in the order of
its embedding similarity to the interest statement. The digest is a Markdown
file for each day in DIGEST_DIR; a second sweep on the same day adds to it.

Triage is triage.decide(): a reranker, on the GPU only. A hit without a
triage row waits for the next sweep: every new hit of a search if the
reranker cannot run (the GPU is not free, the model does not load), and a
hit that the LLM could not judge when the LLM triages instead. The search
is then not marked as swept, so the next sweep looks back far enough. The
window of some days also catches a paper that a source indexes late.

Run it with `phdcode sweep`. It logs to stderr (the systemd journal).
"""
import asyncio
import logging
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import sources
from .embeddings import embed
from .memory import (
    close_pool,
    init_schema,
    list_saved_searches,
    mark_swept,
    record_triage,
    save_record,
    triaged_source_ids,
)
from .triage import decide

load_dotenv()

log = logging.getLogger(__name__)

# A relative DIGEST_DIR is relative to the repo root, as LOG_DIR is.
DIGEST_DIR = Path(__file__).resolve().parents[1] / Path(os.getenv("DIGEST_DIR", "logs/digests")).expanduser()
LOOKBACK_DAYS = 7
# ponytail: a saved search with more new hits in LOOKBACK_DAYS than this
# loses the oldest ones; page through the source if a query is that broad.
MAX_HITS = 50


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


def _hit_line(hit: dict[str, Any], probability: float) -> str:
    authors = ", ".join(hit["authors"][:3]) + (" et al." if len(hit["authors"]) > 3 else "")
    return (f"- [{hit['title']}]({hit['hyperlink']}): {authors}, {(hit['published_date'] or '')[:10]}. "
            f"`{hit['ref']}`, p = {probability:.2f}")


async def sweep_one(saved: dict[str, Any]) -> list[str]:
    """Sweeps one saved search. Returns its section of the digest."""
    lines = [f"## {saved['source']}: {saved['query']}", "",
             f"Saved search {saved['id']}, threshold {saved['threshold']:.2f}. Interest: {saved['interest']}", ""]
    # Back to the last complete sweep at least: hits that waited for triage
    # (the GPU busy for days) are still found.
    last = saved["last_swept_at"] or saved["created_at"]
    since = min(date.today() - timedelta(days=LOOKBACK_DAYS), last.date())
    try:
        hits = await asyncio.to_thread(sources.search, saved["source"], saved["query"], MAX_HITS, since)
    except Exception as e:
        log.exception("saved search %s: the search failed", saved["id"])
        return lines + [f"The search failed: {type(e).__name__}: {e}", ""]

    seen = await triaged_source_ids(saved["id"], [h["source_id"] for h in hits])
    new = [h for h in hits if h["source_id"] not in seen]
    relevant: list[tuple[dict[str, Any], float]] = []
    waiting, invalid = None, 0
    try:
        probabilities = await asyncio.to_thread(decide, new, saved["interest"]) if new else []
    except Exception as e:
        log.exception("saved search %s: triage failed", saved["id"])
        waiting = f"Hits that wait for triage: {len(new)} ({type(e).__name__}: {e})."
        probabilities = []
    for hit, probability in zip(new, probabilities):
        if probability is None:
            invalid += 1
            continue
        await record_triage(saved["id"], hit["source_id"], probability)
        if probability >= saved["threshold"]:
            relevant.append((hit, probability))

    if relevant:
        texts = [saved["interest"]] + [h["abstract_text"] or h["title"] for h, _ in relevant]
        interest_vector, *vectors = await asyncio.to_thread(embed, texts)
        for (hit, _), vector in zip(relevant, vectors):
            await save_record(hit, vector)
        order = sorted(range(len(relevant)), key=lambda i: -_cosine(interest_vector, vectors[i]))
        lines += [_hit_line(*relevant[i]) for i in order]
    else:
        lines.append("No new relevant hits.")
    lines.append("")
    lines.append(f"Hits since {since}: {len(hits)}; new: {len(new)}; "
                 f"relevant: {len(relevant)}.")
    if invalid:
        lines.append(f"Hits that wait for triage (the reply was not a probability): {invalid}.")
    if waiting:
        lines.append(waiting)
    else:
        await mark_swept(saved["id"])
    return lines + [""]


async def sweep() -> Path:
    """Sweeps all saved searches and writes the digest. Returns its path."""
    await init_schema()
    try:
        now = datetime.now()
        lines = [f"# Sweep {now:%Y-%m-%d %H:%M}", ""]
        saved_searches = await list_saved_searches()
        if not saved_searches:
            lines += ["No saved searches.", ""]
        for saved in saved_searches:
            lines += await sweep_one(saved)
    finally:
        await close_pool()

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGEST_DIR / f"{now:%Y-%m-%d}.md"
    with path.open("a") as f:
        f.write("\n".join(lines) + "\n")
    log.info("digest: %s", path)
    return path


if __name__ == "__main__":
    assert abs(_cosine([1, 0], [1, 0]) - 1) < 1e-9 and abs(_cosine([1, 0], [0, 1])) < 1e-9
    assert _cosine([0, 0], [1, 0]) == 0
    print("sweep self-check: ok")
