"""The daily question: questions that a PhD student in the field should
answer from the user's papers; the user answers in a paragraph, and a model
grades it (ADR-0020).

The papers of a question come mostly from the papers that the user marked
crucial (ADR-0019): a tier is picked by TIER_WEIGHTS among the tiers that
have papers, then one paper of it at random, then its NEIGHBOURS nearest
library papers by the concept embedding, so that a question can need more
than one paper. The library is the papers that the user added or marked
(not the sweep's). A paper of a question of the last REPEAT_DAYS days is
not picked again while others are left, so a new question after a skip
uses other papers.

The question model also tags the question with one or two of TOPICS, the
topics of the interest profile, for the statistics of the page.

The score is 0 to 10, plus one bonus point for each library paper that the
answer cites correctly, at most MAX_BONUS. The grader reads the titles and
ids of the whole library, so a correct citation outside the question's
papers counts too.

The interface chooses the models from models(); the defaults are
QUESTION_MODEL and GRADE_MODEL. A local model server is the GPU tenant
(ADR-0017). If it cannot answer (the GPU is cleared for training), ask()
and grade() raise; an answer stays saved and can be graded again.
"""
import asyncio
import os
import random
from collections import Counter
from datetime import date, timedelta
from typing import Any, Optional

from .generation import SUMMARY_MODEL, generate
from .memory import get_pool
from .prompts import grade_prompt, question_prompt
from .routes import ROUTE_NAMES
from .schemas import GRADE_SCHEMA, QUESTION_SCHEMA

QUESTION_MODEL = os.getenv("QUESTION_MODEL") or SUMMARY_MODEL
GRADE_MODEL = os.getenv("GRADE_MODEL") or QUESTION_MODEL

# The topics T1 to T8 of the interest profile (the saved searches).
TOPICS = ("Nuclear morphometry", "Prostate pathology AI", "Prostate cancer prognosis", "MIL on slides",
          "Foundation models", "Graph networks", "Clinical trust in AI", "New architectures")
TIER_WEIGHTS = {"crucial": 0.7, "recommended": 0.2, "rest": 0.1}
NEIGHBOURS = 2
REPEAT_DAYS = 14
MAX_BONUS = 3

_LIBRARY = "(origin IS NULL OR importance IS NOT NULL) AND title IS NOT NULL"


def models() -> list[str]:
    """The model names that the interface offers: each route of LLM_ROUTING."""
    return sorted({*ROUTE_NAMES, QUESTION_MODEL, GRADE_MODEL} - {None})


def _check_model(model: Optional[str], default: str) -> str:
    if model is None:
        return default
    if model not in models():
        raise ValueError(f"Unknown model {model!r}; expected one of {models()}.")
    return model


def _paper_text(p: dict[str, Any]) -> str:
    year = (p["published_date"] or "")[:4]
    parts = [f"[P{p['id']}] {p['title']} ({(p['authors'] or ['?'])[0]} {year})",
             f"Abstract: {p['abstract'] or '(none)'}"]
    if p["methods_summary"]:
        parts.append(f"Methods: {p['methods_summary']}")
    if p["results_summary"]:
        parts.append(f"Results: {p['results_summary']}")
    return "\n".join(parts)


async def _pick(conn) -> list[dict[str, Any]]:
    recent = await conn.fetchval(
        "SELECT coalesce(array_agg(DISTINCT x), '{}') FROM questions, unnest(paper_ids) x "
        "WHERE asked_on > $1::date - $2::int", date.today(), REPEAT_DAYS)
    rows = await conn.fetch(f"SELECT id, importance, id = ANY($1) AS recent FROM papers WHERE {_LIBRARY}", recent)
    if not rows:
        raise ValueError("The library is empty: add papers first.")
    fresh = [r for r in rows if not r["recent"]] or rows
    tiers = {t: [r["id"] for r in fresh if (r["importance"] if r["importance"] in TIER_WEIGHTS else "rest") == t]
             for t in TIER_WEIGHTS}
    tiers = {t: ids for t, ids in tiers.items() if ids}
    tier = random.choices(list(tiers), [TIER_WEIGHTS[t] for t in tiers])[0]
    first = random.choice(tiers[tier])
    ids = [first] + [r["id"] for r in await conn.fetch(
        f"SELECT id FROM papers WHERE {_LIBRARY} AND id <> $1 AND concept_embedding IS NOT NULL "
        "ORDER BY concept_embedding <=> (SELECT concept_embedding FROM papers WHERE id = $1) LIMIT $2",
        first, NEIGHBOURS)]
    rows = await conn.fetch("SELECT id, title, authors, published_date, abstract, methods_summary, "
                            "results_summary FROM papers WHERE id = ANY($1)", ids)
    return sorted((dict(r) for r in rows), key=lambda r: ids.index(r["id"]))


async def ask(model: Optional[str] = None) -> dict[str, Any]:
    """A new question; a question that is not answered stays as skipped."""
    model = _check_model(model, QUESTION_MODEL)
    pool = await get_pool()
    async with pool.acquire() as conn:
        papers = await _pick(conn)
    reply = await asyncio.to_thread(generate, question_prompt("\n\n".join(_paper_text(p) for p in papers), TOPICS),
                                    model, schema=QUESTION_SCHEMA)
    topics = [t for t in TOPICS if t in reply["topics"]]
    async with pool.acquire() as conn:
        return dict(await conn.fetchrow(
            "INSERT INTO questions (asked_on, paper_ids, topics, question, model_answer, question_model) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            date.today(), [p["id"] for p in papers], topics, reply["question"], reply["model_answer"], model))


async def grade(question_id: int, answer: Optional[str] = None, model: Optional[str] = None) -> dict[str, Any]:
    """Saves the answer (if given), then grades the saved answer."""
    model = _check_model(model, GRADE_MODEL)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if answer is not None:
            await conn.execute("UPDATE questions SET answer = $2, answered_at = now() WHERE id = $1",
                               question_id, answer)
        q = await conn.fetchrow("SELECT * FROM questions WHERE id = $1", question_id)
        if q is None or not q["answer"]:
            raise LookupError(f"Question {question_id} does not exist or has no answer.")
        papers = await conn.fetch("SELECT id, title, authors, published_date, abstract, methods_summary, "
                                  "results_summary FROM papers WHERE id = ANY($1)", q["paper_ids"])
        library = await conn.fetch(f"SELECT id, title, authors, published_date FROM papers WHERE {_LIBRARY} ORDER BY id")
    titles = "\n".join(f"P{r['id']}: {r['title']} ({(r['authors'] or ['?'])[0]} {(r['published_date'] or '')[:4]})"
                       for r in library)
    prompt = grade_prompt(q["question"], q["model_answer"], "\n\n".join(_paper_text(dict(p)) for p in papers),
                          titles, q["answer"])
    reply = await asyncio.to_thread(generate, prompt, model, schema=GRADE_SCHEMA)
    known = {r["id"] for r in library}
    cited = sorted(set(reply["cited_papers"]) & known)
    async with pool.acquire() as conn:
        return dict(await conn.fetchrow(
            "UPDATE questions SET score = $2, bonus = $3, cited = $4, feedback = $5, grade_model = $6, "
            "graded_at = now() WHERE id = $1 RETURNING *",
            question_id, max(0, min(10, reply["score"])), min(MAX_BONUS, len(cited)), cited, reply["feedback"],
            model))


def stats(rows: list[dict[str, Any]], weeks: int = 26, today: Optional[date] = None) -> dict[str, Any]:
    """The statistics of the graded questions: points, the answers of each
    day (local date of the answer), the current and the longest streak of
    days with an answer, and the count and the mean score of each topic."""
    today = today or date.today()
    graded = [q for q in rows if q["graded_at"]]
    per_day = Counter(q["answered_at"].astimezone().date() for q in graded)
    longest = run = 0
    for day in sorted(per_day):
        run = run + 1 if day - timedelta(days=1) in per_day else 1
        longest = max(longest, run)
    current, day = 0, today if today in per_day else today - timedelta(days=1)
    while day in per_day:
        current, day = current + 1, day - timedelta(days=1)
    # The calendar: whole weeks from Monday, the last one holds today.
    start = today - timedelta(days=today.weekday() + 7 * (weeks - 1))
    calendar = [[(d, per_day[d]) for d in (start + timedelta(days=7 * w + i) for i in range(7)) if d <= today]
                for w in range(weeks)]
    topics = {t: [q["score"] for q in graded if t in (q["topics"] or [])] for t in TOPICS}
    return {
        "points": sum(q["score"] + q["bonus"] for q in graded),
        "answered": len(graded),
        "today": per_day[today],
        "mean": sum(q["score"] for q in graded) / len(graded) if graded else None,
        "current": current,
        "longest": longest,
        "calendar": calendar,
        "topics": {t: (len(s), sum(s) / len(s) if s else None) for t, s in topics.items()},
    }


if __name__ == "__main__":
    from datetime import datetime, timezone

    t = date(2026, 10, 14)  # a Wednesday

    def q(days_ago: int, score: int, topics: list[str]) -> dict[str, Any]:
        at = datetime.combine(t - timedelta(days=days_ago), datetime.min.time()).replace(hour=12).astimezone()
        return {"graded_at": at, "answered_at": at, "score": score, "bonus": 1, "topics": topics}

    s = stats([q(0, 8, ["MIL on slides"]), q(0, 6, []), q(1, 4, ["MIL on slides", "Graph networks"]),
               q(5, 9, []), q(6, 9, []), q(7, 9, []), {"graded_at": None}], today=t)
    assert (s["points"], s["answered"], s["today"]) == (51, 6, 2), s
    assert (s["current"], s["longest"]) == (2, 3), s
    assert s["topics"]["MIL on slides"] == (2, 6.0) and s["topics"]["Graph networks"] == (1, 4.0)
    assert s["calendar"][-1][-1] == (t, 2) and len(s["calendar"][-1]) == 3  # Mon to Wed
    assert all(len(w) == 7 for w in s["calendar"][:-1]) and s["calendar"][0][0][0].weekday() == 0
    assert stats([], today=t)["current"] == 0
    assert stats([q(1, 5, [])], today=t)["current"] == 1  # an answer yesterday keeps the streak
    print("ok")
