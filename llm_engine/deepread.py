"""Deep read (ADR-0034): an exam on one full-text paper of the library, which
trains the skills of critical reading.

The examiner model reads the whole text of the paper and asks one question
at a time. Each question is in one of AREAS and tests one of SKILLS (the
wheel of critical reading). The code picks the skill of each new question:
one not asked yet in this session, then the weakest of the past sessions
(the fewest graded questions, then the lowest mean score). The model picks
the area, with the counts of the session, so that the areas mix. After an
answer, the model asks a follow-up on the same item if the answer is weak or
vague, or a new question. The student can ask a question back; the examiner
answers it. After QUESTIONS items the examiner asks nothing new.

"Finish" grades each answered item 0 to 10 in one call, with quotes of the
paper that the page checks (ask.check_quotes), names 0 to 3 concepts to
brush up on, and marks the paper as read. Finish again grades again.

An item is a new question with its follow-ups and the student's messages
until the next new question. Each message stores its item number; the
examiner row that opens an item holds its area, skill, score, feedback and
quotes.
"""
import asyncio
import os
import random
from collections import Counter
from typing import Any, Optional

from .ask import check_quotes
from .generation import generate
from .memory import get_pool
from .prompts import deepread_grade_prompt, deepread_prompt
from .questions import _STUDY_PAPERS, _check_model, clean_topics, known_topics

DEEPREAD_MODEL = os.getenv("DEEPREAD_MODEL") or "claude-sonnet-5-5"
QUESTIONS = 10
# Each turn sends the whole text again (claude -p keeps no session), in the
# system message, so that the prompt cache reuses it (generate's context).
# The longest paper now is 350 000 characters, about 90 000 tokens.
MAX_PAPER_CHARS = 400_000

AREAS = {
    "Comprehension": "the problem, the research question, the main claim and the contribution",
    "Methods": "what the authors did, and why: data, model, training, the evaluation protocol",
    "Results and statistics": "what the tables and figures show, the metrics, effect sizes and uncertainty",
    "Critique": "the limitations, the threats to validity, and whether the conclusions follow",
}
SKILLS = {
    "State the claim": "state the research question and the main claim in exact terms",
    "Follow the method": "tell what the authors did, in enough detail to reproduce it",
    "Read the numbers": "read the tables, figures, metrics, effect sizes and confidence intervals",
    "Test the claim against the evidence": "judge whether the conclusion follows, or the authors claim too much",
    "Find bias and leakage": "selection bias, confounders, leakage between the patient splits, label noise",
    "Judge validity and generalization": "internal against external validation, the cohort, domain shift",
    "Judge the baselines and ablations": "fair comparisons, missing controls and ablations",
    "Check reproducibility and reporting": "the code, the data, missing details, the reporting standards "
                                           "(TRIPOD+AI, CLAIM)",
}
ASKS = ("new", "follow-up", "none")

EXAMINER_SCHEMA = {
    "message": (str, ...),
    "asks": (str, ...),
    "area": (str, ""),
}
GRADE_SCHEMA = {
    "items": (list[dict[str, Any]], ...),
    "summary": (str, ...),
    "review_topics": (list[str], []),
}


async def papers() -> list[dict[str, Any]]:
    """The library papers with full text (chunks), with their count of sessions."""
    rows = await (await get_pool()).fetch(
        f"SELECT p.id, p.title, (SELECT count(*) FROM deepread_sessions s WHERE s.paper_id = p.id) AS sessions "
        f"FROM papers p WHERE {_STUDY_PAPERS} AND EXISTS (SELECT 1 FROM chunks c WHERE c.paper_id = p.id) "
        "ORDER BY p.title")
    return [dict(r) for r in rows]


async def _paper(conn, paper_id: int) -> tuple[str, list[str]]:
    """The text of the paper for the prompts, and its parts for the quote
    check (the title with the abstract, and each chunk)."""
    p = await conn.fetchrow(f"SELECT id, title, abstract FROM papers WHERE id = $1 AND {_STUDY_PAPERS}", paper_id)
    chunks = await conn.fetch("SELECT section, content FROM chunks WHERE paper_id = $1 ORDER BY chunk_index", paper_id)
    if p is None or not chunks:
        raise LookupError(f"Paper {paper_id} is not a library paper with full text.")
    parts, section = [f"[P{p['id']}] {p['title']}"], None
    for c in chunks:
        if c["section"] and c["section"] != section:
            section = c["section"]
            parts.append(f"## {section}")
        parts.append(c["content"])
    return "\n\n".join(parts)[:MAX_PAPER_CHARS], [f"{p['title']} {p['abstract'] or ''}", *(c["content"] for c in chunks)]


def transcript(messages: list[dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        tag = f" [question {m['item']}: {m['area']}; {m['skill']}]" if m["area"] else ""
        lines.append(f"{m['role'].capitalize()} (item {m['item']}){tag}: {m['content']}")
    return "\n\n".join(lines)


def weakest_skill(graded: list[dict[str, Any]], asked: list[str]) -> str:
    """The skill not asked yet in this session (asked), then with the fewest
    graded items of the past (graded: skill, score), then the lowest mean
    score; ties at random."""
    n, total = Counter(g["skill"] for g in graded), Counter()
    for g in graded:
        total[g["skill"]] += g["score"]
    return min(SKILLS, key=lambda s: (asked.count(s), n[s], total[s] / n[s] if n[s] else 0, random.random()))


async def _examine(conn, text: str, messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """The examiner's next message after messages (with the student's last
    one), with its item, area and skill (area and skill only when it opens an
    item)."""
    opened = [m for m in messages if m["area"]]
    skill = None
    if len(opened) < QUESTIONS:
        graded = await conn.fetch("SELECT skill, score FROM deepread_messages WHERE score IS NOT NULL")
        skill = weakest_skill([dict(g) for g in graded], [m["skill"] for m in opened])
    areas = Counter({a: 0 for a in AREAS})
    areas.update(m["area"] for m in opened)
    reply = await asyncio.to_thread(
        generate, deepread_prompt(transcript(messages), AREAS, dict(areas),
                                  skill and f"{skill}: {SKILLS[skill]}", len(opened), QUESTIONS),
        model, schema=EXAMINER_SCHEMA, context=f"The paper:\n{text}")
    asks = reply["asks"] if reply["asks"] in ASKS else "none"
    if not opened:
        asks = "new"
    elif asks == "new" and skill is None:  # past the limit
        asks = "none"
    item = max((m["item"] for m in messages), default=0)
    if asks != "new":
        return {"content": reply["message"], "item": item, "area": None, "skill": None}
    area = reply["area"] if reply["area"] in AREAS else min(AREAS, key=areas.__getitem__)
    return {"content": reply["message"], "item": item + 1, "area": area, "skill": skill}


async def _save(conn, session_id: int, *rows: dict[str, Any]) -> None:
    await conn.executemany(
        "INSERT INTO deepread_messages (session_id, role, content, item, area, skill) VALUES ($1, $2, $3, $4, $5, $6)",
        [(session_id, r["role"], r["content"], r["item"], r.get("area"), r.get("skill")) for r in rows])


async def start(paper_id: int, model: Optional[str] = None) -> int:
    """A new session on the paper, with the examiner's first question; nothing
    is saved if the model fails."""
    model = _check_model(model, DEEPREAD_MODEL)
    pool = await get_pool()
    async with pool.acquire() as conn:
        text, _ = await _paper(conn, paper_id)
        first = await _examine(conn, text, [], model)
        async with conn.transaction():
            session_id = await conn.fetchval(
                "INSERT INTO deepread_sessions (paper_id, model) VALUES ($1, $2) RETURNING id", paper_id, model)
            await _save(conn, session_id, {"role": "examiner", **first})
    return session_id


async def restart(session_id: int) -> int:
    """A new session on the paper and with the model of session_id, which is
    then deleted with its messages and grades; the old one stays if the
    model fails."""
    pool = await get_pool()
    s = await pool.fetchrow("SELECT paper_id, model FROM deepread_sessions WHERE id = $1", session_id)
    if s is None:
        raise LookupError(f"No session has id {session_id}.")
    new_id = await start(s["paper_id"], s["model"])
    await pool.execute("DELETE FROM deepread_sessions WHERE id = $1", session_id)
    return new_id


async def _session(conn, session_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    s = await conn.fetchrow("SELECT * FROM deepread_sessions WHERE id = $1", session_id)
    if s is None:
        raise LookupError(f"No session has id {session_id}.")
    messages = await conn.fetch("SELECT * FROM deepread_messages WHERE session_id = $1 ORDER BY id", session_id)
    return dict(s), [dict(m) for m in messages]


async def reply(session_id: int, message: str) -> None:
    """Saves the student's message with the examiner's reply; nothing is
    saved if the model fails."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        s, messages = await _session(conn, session_id)
        if s["finished_at"]:
            raise ValueError("This session is finished.")
        text, _ = await _paper(conn, s["paper_id"])
        student = {"role": "student", "content": message, "item": messages[-1]["item"], "area": None, "skill": None}
        examiner = await _examine(conn, text, messages + [student], s["model"])
        async with conn.transaction():
            await _save(conn, session_id, student, {"role": "examiner", **examiner})


async def finish(session_id: int) -> None:
    """Grades each answered item, writes the summary and the concepts to
    brush up on, and marks the paper as read."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        s, messages = await _session(conn, session_id)
        answered = {m["item"] for m in messages if m["role"] == "student"}
        items = {m["item"]: m for m in messages if m["area"] and m["item"] in answered}
        if not items:
            raise ValueError("Answer at least one question first.")
        text, sources = await _paper(conn, s["paper_id"])
        known = await known_topics(conn, "SELECT review_topics FROM deepread_sessions WHERE review_topics IS NOT NULL "
                                         "ORDER BY id DESC LIMIT $1")
        r = await asyncio.to_thread(
            generate, deepread_grade_prompt(transcript(messages),
                                            [f"item {i}: {m['skill']}: {SKILLS[m['skill']]}" for i, m in items.items()],
                                            known),
            s["model"], schema=GRADE_SCHEMA, context=f"The paper:\n{text}")
        grades = {g.get("item"): g for g in r["items"] if g.get("item") in items and isinstance(g.get("score"), int)}
        said = [m["content"] for m in messages if m["role"] == "student"]

        def quotes(feedback: str) -> list[dict[str, Any]]:
            # A quote of the student's own words is not a claim about the paper.
            return [q for q, s in zip(check_quotes(feedback, sources), check_quotes(feedback, said))
                    if q["found"] or not s["found"]]

        async with conn.transaction():
            await conn.executemany(
                "UPDATE deepread_messages SET score = $2, feedback = $3, quotes = $4 WHERE id = $1",
                [(items[i]["id"], max(0, min(10, g["score"])), str(g.get("feedback") or ""),
                  quotes(str(g.get("feedback") or ""))) for i, g in grades.items()])
            await conn.execute("UPDATE deepread_sessions SET finished_at = now(), summary = $2, review_topics = $3 "
                               "WHERE id = $1", session_id, r["summary"], clean_topics(r["review_topics"]))
            await conn.execute("UPDATE papers SET read_at = coalesce(read_at, now()) WHERE id = $1", s["paper_id"])


def stats(graded: list[dict[str, Any]]) -> dict[str, Any]:
    """graded: each graded item (skill, area, score). The count and mean score
    of each skill and area, and the mean score."""
    def summary(scores: list[int]) -> tuple[int, Optional[float]]:
        return len(scores), sum(scores) / len(scores) if scores else None

    return {
        "answered": len(graded),
        "mean": summary([g["score"] for g in graded])[1],
        "skills": {k: summary([g["score"] for g in graded if g["skill"] == k]) for k in SKILLS},
        "areas": {k: summary([g["score"] for g in graded if g["area"] == k]) for k in AREAS},
    }


if __name__ == "__main__":
    for _ in range(20):
        # A skill asked in this session comes last; then the fewest graded.
        s = weakest_skill([{"skill": "Read the numbers", "score": 9}], ["State the claim"])
        assert s not in ("State the claim", "Read the numbers"), s
    every = list(SKILLS)
    assert weakest_skill([{"skill": k, "score": 9} for k in every if k != "Find bias and leakage"]
                         + [{"skill": "Find bias and leakage", "score": 2}], []) == "Find bias and leakage"
    for _ in range(20):  # 2 graded with a low mean, every other 1: the fewest wins
        assert weakest_skill([{"skill": "Follow the method", "score": 3}] + [{"skill": k, "score": 8} for k in every],
                             []) != "Follow the method"
    s = stats([{"skill": "Read the numbers", "area": "Methods", "score": 6},
               {"skill": "Read the numbers", "area": "Critique", "score": 8}])
    assert (s["answered"], s["mean"], s["skills"]["Read the numbers"], s["areas"]["Methods"]) == (2, 7.0, (2, 7.0), (1, 6.0))
    assert s["skills"]["State the claim"] == (0, None)
    t = transcript([{"role": "examiner", "content": "Why?", "item": 1, "area": "Methods", "skill": "Follow the method"},
                    {"role": "student", "content": "Because.", "item": 1, "area": None, "skill": None}])
    assert t == "Examiner (item 1) [question 1: Methods; Follow the method]: Why?\n\nStudent (item 1): Because.", t
    print("ok")
