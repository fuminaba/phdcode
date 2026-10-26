"""The sweep: one pass over all saved searches (ADR-0015, item 6).

For each saved search, the sweep searches the source for the papers of the
last LOOKBACK_DAYS days, or since its last complete sweep if that is
earlier, and triage judges each hit that it has not judged before. A relevant hit (probability >= the threshold of the saved search) is
stored as an abstract-only paper and goes into the digest, in the order of
its embedding similarity to the interest statement. The digest is a Markdown
file for each day in DIGEST_DIR; a second sweep on the same day adds to it.

Triage is triage.decide(): the summary model judges whether the student
with the user's library wants each hit (ADR-0018). A hit without a triage
row waits for the next sweep: every new hit of a search if the model server
cannot answer (the GPU is cleared for training), and a hit whose reply was
not a probability. The search is then not marked as swept, so the next
sweep looks back far enough. The interest statement of a saved search only
orders its relevant hits in the digest. The
window of some days also catches a paper that a source indexes late.

A saved paper waits in triage (origin 'sweep') until the user keeps it or
deletes it. After the digest, the sweep writes the briefs (briefs.py,
ADR-0032).

Run it with `phdcode sweep`. It logs to stderr (the systemd journal).
"""
import asyncio
import logging
import math
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import sources
from .embeddings import embed
from .memory import (
    close_pool,
    init_schema,
    library_titles,
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


# parse_digest() reads the lines that _hit_line() and sweep_one() write:
# change the writer and the parser together (the self-check runs both).
def _hit_line(hit: dict[str, Any], probability: float) -> str:
    authors = ", ".join(hit["authors"][:3]) + (" et al." if len(hit["authors"]) > 3 else "")
    return (f"- [{hit['title']}]({hit['hyperlink']}): {authors}, {(hit['published_date'] or '')[:10]}. "
            f"`{hit['ref']}`, p = {probability:.2f}")


_HIT = re.compile(r"- \[(?P<title>.*)\]\((?P<url>[^)\s]*)\): (?P<authors>.*), (?P<date>[\d-]*)\. "
                  r"`(?P<ref>[^`]+)`, p = (?P<p>[\d.]+)$")
_STATS = re.compile(r"Hits (?:since \S+|in the last \d+ days): (?P<hits>\d+); new: (?P<new>\d+); "
                    r"relevant: (?P<relevant>\d+)\.$")
_SEARCH = re.compile(r"Saved search (?P<id>\d+), threshold (?P<threshold>[\d.]+)\. Interest: (?P<interest>.*)$")


def parse_digest(text: str) -> list[dict[str, Any]]:
    """The sweeps of a digest file, as written by sweep() and sweep_one(), for
    the interface: for each sweep its time and its saved searches, each with
    its query, interest, relevant hits and counts. A line of another form
    (a failed search, hits that wait) is kept in the notes of its search."""
    sweeps: list[dict[str, Any]] = []
    search: dict[str, Any] = {}
    for line in text.splitlines():
        if line.startswith("# Sweep "):
            sweeps.append({"time": line.removeprefix("# Sweep "), "searches": [], "notes": []})
        elif line.startswith("## ") and sweeps:
            source, _, query = line[3:].partition(": ")
            search = {"source": source, "query": query, "hits": [], "notes": [], "counts": None}
            sweeps[-1]["searches"].append(search)
        elif not line.strip() or line == "No new relevant hits." or not sweeps:
            continue
        elif not search:
            sweeps[-1]["notes"].append(line)
        elif m := _HIT.match(line):
            search["hits"].append({**m.groupdict(), "p": float(m["p"])})
        elif m := _STATS.match(line):
            search["counts"] = {k: int(v) for k, v in m.groupdict().items()}
        elif m := _SEARCH.match(line):
            search.update(id=int(m["id"]), threshold=float(m["threshold"]), interest=m["interest"])
        else:
            search["notes"].append(line)
    return sweeps


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
        probabilities = await asyncio.to_thread(decide, new, await library_titles()) if new else []
    except Exception as e:
        log.exception("saved search %s: triage failed", saved["id"])
        waiting = f"Hits that wait for triage: {len(new)} ({type(e).__name__}: {e})."
        probabilities = []
    for hit, probability in zip(new, probabilities):
        if probability is None:
            invalid += 1
            continue
        await record_triage(saved["id"], hit, probability)
        if probability >= saved["threshold"]:
            relevant.append((hit, probability))

    if relevant:
        texts = [saved["interest"]] + [h["abstract_text"] or h["title"] for h, _ in relevant]
        interest_vector, *vectors = await asyncio.to_thread(embed, texts)
        for (hit, _), vector in zip(relevant, vectors):
            await save_record(hit, vector, origin="sweep")
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

        DIGEST_DIR.mkdir(parents=True, exist_ok=True)
        path = DIGEST_DIR / f"{now:%Y-%m-%d}.md"
        with path.open("a") as f:
            f.write("\n".join(lines) + "\n")
        log.info("digest: %s", path)
        # The briefs read the digest file, so briefs imports this module.
        from . import briefs
        for failure in await briefs.write_after_sweep(now.date()):
            log.warning(failure)
    finally:
        await close_pool()
    return path


if __name__ == "__main__":
    assert abs(_cosine([1, 0], [1, 0]) - 1) < 1e-9 and abs(_cosine([1, 0], [0, 1])) < 1e-9
    assert _cosine([0, 0], [1, 0]) == 0
    # The parser reads what the writer writes.
    hit = {"title": "A [study] of nuclei", "hyperlink": "https://x.org/1", "authors": ["A B", "C D", "E F", "G H"],
           "published_date": "2026-10-10", "ref": "pubmed:1"}
    text = "\n".join(["# Sweep 2026-10-12 19:08", "", "## pubmed: a AND (b OR c)", "",
                      "Saved search 2, threshold 0.30. Interest: Deep learning on prostate slides (biopsies).", "",
                      _hit_line(hit, 0.834), "", "Hits since 2026-10-05: 7; new: 7; relevant: 1.",
                      "Hits that wait for triage: 2 (ConnectionError: down).", "",
                      "## arxiv: d", "", "Saved search 9, threshold 0.30. Interest: e", "", "No new relevant hits.",
                      "", "Hits in the last 7 days: 0; new: 0; relevant: 0.", ""])
    [s] = parse_digest(text)
    a, b = s["searches"]
    assert s["time"] == "2026-10-12 19:08" and (a["id"], a["threshold"], b["id"]) == (2, 0.3, 9), s
    assert a["hits"] == [{"title": "A [study] of nuclei", "url": "https://x.org/1", "authors": "A B, C D, E F et al.",
                          "date": "2026-10-10", "ref": "pubmed:1", "p": 0.83}], a["hits"]
    assert a["counts"] == {"hits": 7, "new": 7, "relevant": 1} and a["notes"] == [
        "Hits that wait for triage: 2 (ConnectionError: down)."], a
    assert b["hits"] == [] and b["counts"]["hits"] == 0 and b["query"] == "d"
    print("sweep self-check: ok")

