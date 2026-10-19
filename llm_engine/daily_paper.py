"""The paper of the day: a brief of one library paper for the user's PhD
defence (ADR-0024): what it is about, a summary, the key points to know and
the questions that an examiner could ask.

The paper is one that was featured least recently (never featured first),
then, of those, one with a methods summary (a full-text paper) if one is
left, then a tier by questions.TIER_WEIGHTS, as for the daily question. The
brief is written once a day, when the user asks for it; "Another paper"
replaces it.
"""
import asyncio
import random
from datetime import date
from typing import Any, Optional

from .generation import generate
from .memory import get_pool
from .prompts import daily_paper_prompt
from .questions import QUESTION_MODEL, TIER_WEIGHTS, _LIBRARY, _check_model, _paper_text
from .schemas import DAILY_PAPER_SCHEMA


def choose(rows: list[dict[str, Any]]) -> int:
    """The id of the next paper of the day; rows: id, importance,
    summarized, last (the last day it was featured, or None)."""
    if not rows:
        raise ValueError("The library is empty: add papers first.")
    oldest = min(r["last"] or date.min for r in rows)
    rows = [r for r in rows if (r["last"] or date.min) == oldest]
    rows = [r for r in rows if r["summarized"]] or rows
    tiers: dict[str, list[int]] = {}
    for r in rows:
        tiers.setdefault(r["importance"] if r["importance"] in TIER_WEIGHTS else "rest", []).append(r["id"])
    tier = random.choices(list(tiers), [TIER_WEIGHTS[t] for t in tiers])[0]
    return random.choice(tiers[tier])


async def today() -> Optional[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM daily_papers WHERE day = $1", date.today())
    return dict(row) if row else None


async def write(model: Optional[str] = None) -> dict[str, Any]:
    """A new brief for today, of the next paper; it replaces today's brief."""
    model = _check_model(model, QUESTION_MODEL)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.id, p.importance, p.methods_summary IS NOT NULL AS summarized, "
            "       (SELECT max(day) FROM daily_papers d WHERE d.paper_id = p.id) AS last "
            f"FROM papers p WHERE {_LIBRARY}")
        paper = dict(await conn.fetchrow(
            "SELECT id, title, authors, published_date, abstract, methods_summary, results_summary "
            "FROM papers WHERE id = $1", choose([dict(r) for r in rows])))
    reply = await asyncio.to_thread(generate, daily_paper_prompt(_paper_text(paper)), model,
                                    schema=DAILY_PAPER_SCHEMA)
    async with pool.acquire() as conn:
        return dict(await conn.fetchrow(
            "INSERT INTO daily_papers (day, paper_id, about, summary, key_points, examiner_questions, model) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (day) DO UPDATE SET paper_id = $2, about = $3, "
            "summary = $4, key_points = $5, examiner_questions = $6, model = $7, created_at = now() RETURNING *",
            date.today(), paper["id"], reply["about"], reply["summary"], reply["key_points"],
            reply["examiner_questions"], model))


if __name__ == "__main__":
    d = date(2026, 10, 1)
    rows = [{"id": 1, "importance": "crucial", "summarized": True, "last": d},
            {"id": 2, "importance": None, "summarized": False, "last": None},
            {"id": 3, "importance": None, "summarized": True, "last": None},
            {"id": 4, "importance": "crucial", "summarized": False, "last": None}]
    # Never featured first, then a summarized paper: 3 is the only one.
    assert {choose(rows) for _ in range(20)} == {3}
    # With no summarized paper left among the oldest, the crucial tier mostly.
    picks = [choose(rows[:2] + rows[3:]) for _ in range(200)]
    assert set(picks) == {2, 4} and picks.count(4) > picks.count(2), picks.count(4)
    # All featured: the least recent.
    assert choose([{**rows[0], "last": d}, {**rows[2], "last": date(2026, 9, 1)}]) == 3
    print("ok")
