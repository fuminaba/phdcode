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

Steering (ADR-0021): each question tests one axis of a set of AXIS_SETS
(competencies of the field, or the areas of a comprehensive exam) and one
of QUESTION_TYPES. The user chooses them, or the weakest are taken: the axis
of the chosen set with the fewest graded answers (then the lowest mean
score), and the least practised type. The first paper is then one of the
FOCUS_PAPERS of its tier nearest to the axis by the concept embedding. The
question model tags each question with one or two axes of every set, so each
set has its own radar, and writes the key points of a strong answer.

The score is 0 to 10, plus one bonus point for each library paper that the
answer cites correctly, at most MAX_BONUS. The grader reads the titles and
ids of the whole library, so a correct citation outside the question's
papers counts too.

The paper of the day (ADR-0024) is a brief of one library paper for the
user's PhD defence: what it is about, a summary, the key points to know and
the questions that an examiner could ask. The paper is one that was featured
least recently (never featured first), then, of those, one with a methods
summary (a full-text paper) if one is left, then a tier by TIER_WEIGHTS. The
brief is written once a day, when the user asks for it; "Another paper"
replaces it.

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

from .embeddings import embed
from .generation import SUMMARY_MODEL, generate
from .memory import get_pool
from .prompts import chat_prompt, daily_paper_prompt, grade_prompt, question_prompt
from .routes import ROUTE_NAMES

QUESTION_MODEL = os.getenv("QUESTION_MODEL") or SUMMARY_MODEL
GRADE_MODEL = os.getenv("GRADE_MODEL") or QUESTION_MODEL

# The axes of the radars: for each set, each axis with what it covers (the
# model and the embedding read the description). Labels are unique across
# the sets, because a tag names its axis only.
AXIS_SETS = {
    "Field competencies": {
        "Cancer biology and pathology": "prostate cancer progression, Gleason grading, nuclear and chromatin change, "
                                        "and why tissue carries prognostic signal",
        "Image data and its problems": "slide scanning, staining, stain and scanner variation, tiling and "
                                       "magnification, annotation, label noise, domain shift",
        "Hand-crafted vs learned features": "nuclear morphometry and texture features against deep features, "
                                            "and when each one wins",
        "Learning with weak labels": "multiple instance learning, self-supervised learning, foundation models, "
                                     "transfer learning, learning from few labels",
        "Architectures and inductive bias": "CNNs, transformers, graph networks and state-space models, and what "
                                            "each one assumes about tissue",
        "Evaluation and statistics": "metrics, survival analysis (C-index, Kaplan-Meier, Cox), calibration, "
                                     "confidence intervals, data leakage",
        "Study design and evidence": "cohorts, endpoints, confounders, bias, external validation, reporting standards",
        "Interpretability and translation": "explanations, failure modes, human-AI teamwork, regulation and "
                                            "clinical deployment",
    },
    "Exam areas": {
        "ML foundations": "optimization, generalization, regularization, the bias-variance trade-off, "
                          "probabilistic models",
        "Deep learning for vision": "convolutional and attention models for images, training, augmentation, "
                                    "segmentation and detection",
        "Medical imaging and pathology data": "how medical and pathology images are made, stored, annotated "
                                              "and normalized",
        "Cancer biology": "the biology of cancer, prostate cancer in particular, and its signs in tissue",
        "Biostatistics and survival analysis": "hypothesis tests, survival models, effect sizes, uncertainty",
        "Research design": "hypotheses, experiments, baselines, ablations, reproducibility",
        "Clinical translation and ethics": "validation for clinical use, regulation, fairness, privacy, "
                                           "patient impact",
        "Critical appraisal": "judging whether the claims of a paper follow from its evidence",
    },
}
QUESTION_TYPES = {
    "Explain": "explain a mechanism, or why a method works",
    "Compare": "compare two methods or results, and say when each one is better",
    "Critique": "judge a claim against its evidence, and name the limitations",
    "Design": "design an experiment or a study that would settle an open point",
    "Apply": "apply a method or a finding to a new case, such as a new prostate cancer cohort",
}
_SET_OF = {axis: name for name, axes in AXIS_SETS.items() for axis in axes}
FOCUS_PAPERS = 5
TIER_WEIGHTS = {"crucial": 0.7, "recommended": 0.2, "rest": 0.1}
NEIGHBOURS = 2
REPEAT_DAYS = 14
MAX_BONUS = 3

_LIBRARY = "(origin IS NULL OR importance IS NOT NULL) AND title IS NOT NULL AND deleted_at IS NULL"
# The columns that _paper_text reads.
_PAPER_FIELDS = "id, title, authors, published_date, abstract, methods_summary, results_summary"

QUESTION_SCHEMA = {
    "question": (str, ...),
    "model_answer": (str, ...),
    "key_points": (list[str], ...),
    # One flat list; a list for each axis set is accepted too.
    "tags": (list[str] | dict[str, list[str]], ...),
}

GRADE_SCHEMA = {
    "score": (int, ...),
    "cited_papers": (list[int], ...),
    "feedback": (str, ...),
}

DAILY_PAPER_SCHEMA = {
    "about": (str, ...),
    "summary": (str, ...),
    "key_points": (list[str], ...),
    "examiner_questions": (list[str], ...),
}


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


def _tier(rows: list[dict[str, Any]]) -> list[int]:
    """The ids of one tier of rows (id, importance), picked by TIER_WEIGHTS
    among the tiers that have rows."""
    tiers: dict[str, list[int]] = {}
    for r in rows:
        tiers.setdefault(r["importance"] if r["importance"] in TIER_WEIGHTS else "rest", []).append(r["id"])
    return tiers[random.choices(list(tiers), [TIER_WEIGHTS[t] for t in tiers])[0]]


async def _pick(conn, focus: list[float]) -> list[dict[str, Any]]:
    recent = await conn.fetchval(
        "SELECT coalesce(array_agg(DISTINCT x), '{}') FROM questions, unnest(paper_ids) x "
        "WHERE asked_on > $1::date - $2::int", date.today(), REPEAT_DAYS)
    rows = await conn.fetch(f"SELECT id, importance, id = ANY($1) AS recent FROM papers WHERE {_LIBRARY}", recent)
    if not rows:
        raise ValueError("The library is empty: add papers first.")
    fresh = [r for r in rows if not r["recent"]] or rows
    tier = _tier(fresh)
    near = await conn.fetch("SELECT id FROM papers WHERE id = ANY($1) AND concept_embedding IS NOT NULL "
                            "ORDER BY concept_embedding <=> $2 LIMIT $3", tier, focus, FOCUS_PAPERS)
    first = random.choice([r["id"] for r in near] or tier)
    ids = [first] + [r["id"] for r in await conn.fetch(
        f"SELECT id FROM papers WHERE {_LIBRARY} AND id <> $1 AND concept_embedding IS NOT NULL "
        "ORDER BY concept_embedding <=> (SELECT concept_embedding FROM papers WHERE id = $1) LIMIT $2",
        first, NEIGHBOURS)]
    rows = await conn.fetch(f"SELECT {_PAPER_FIELDS} FROM papers WHERE id = ANY($1)", ids)
    return sorted((dict(r) for r in rows), key=lambda r: ids.index(r["id"]))


def _weakest(rows: list[dict[str, Any]], axis_set: str) -> tuple[str, str]:
    """The axis of axis_set with the fewest graded answers (then the lowest
    mean score), and the least practised question type; ties at random."""
    s = stats(rows)

    def key(item):
        n, mean = item[1]
        return n, mean if mean is not None else 0, random.random()

    return (min(s["axes"][axis_set].items(), key=key)[0], min(s["types"].items(), key=key)[0])


async def ask(model: Optional[str] = None, axis_set: Optional[str] = None, axis: Optional[str] = None,
              question_type: Optional[str] = None) -> dict[str, Any]:
    """A new question on axis and question_type (the weakest of axis_set when
    None); a question that is not answered stays as skipped."""
    model = _check_model(model, QUESTION_MODEL)
    axis_set = axis_set or next(iter(AXIS_SETS))
    pool = await get_pool()
    async with pool.acquire() as conn:
        weak_axis, weak_type = _weakest([dict(r) for r in await conn.fetch("SELECT * FROM questions")], axis_set)
        axis, question_type = axis or weak_axis, question_type or weak_type
        description = AXIS_SETS[_SET_OF[axis]][axis]
        [focus] = await asyncio.to_thread(embed, [f"{axis}: {description}"])
        papers = await _pick(conn, focus)
    prompt = question_prompt("\n\n".join(_paper_text(p) for p in papers), f"{axis} ({description})",
                             f"{question_type}: {QUESTION_TYPES[question_type]}",
                             {name: list(axes) for name, axes in AXIS_SETS.items()})
    reply = await asyncio.to_thread(generate, prompt, model, schema=QUESTION_SCHEMA)
    labels = reply["tags"] if isinstance(reply["tags"], list) else [a for v in reply["tags"].values() for a in v]
    tags = [f"{_SET_OF[a]}: {a}" for a in dict.fromkeys([axis, *labels]) if a in _SET_OF]
    async with pool.acquire() as conn:
        return dict(await conn.fetchrow(
            "INSERT INTO questions (asked_on, paper_ids, topics, question_type, question, model_answer, key_points, "
            "question_model) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
            date.today(), [p["id"] for p in papers], tags, question_type, reply["question"],
            reply["model_answer"], reply["key_points"], model))


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
        papers = await conn.fetch(f"SELECT {_PAPER_FIELDS} FROM papers WHERE id = ANY($1)", q["paper_ids"])
        library = await conn.fetch(f"SELECT id, title, authors, published_date FROM papers WHERE {_LIBRARY} ORDER BY id")
    titles = "\n".join(f"P{r['id']}: {r['title']} ({(r['authors'] or ['?'])[0]} {(r['published_date'] or '')[:4]})"
                       for r in library)
    prompt = grade_prompt(q["question"], q["model_answer"], q["key_points"] or [],
                          "\n\n".join(_paper_text(dict(p)) for p in papers), titles, q["answer"])
    reply = await asyncio.to_thread(generate, prompt, model, schema=GRADE_SCHEMA)
    known = {r["id"] for r in library}
    cited = sorted(set(reply["cited_papers"]) & known)
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "UPDATE questions SET score = $2, bonus = $3, cited = $4, feedback = $5, grade_model = $6, "
            "graded_at = now() WHERE id = $1 RETURNING *",
            question_id, max(0, min(10, reply["score"])), min(MAX_BONUS, len(cited)), cited, reply["feedback"],
            model)
        await conn.execute(
            "INSERT INTO attempts (question_id, answer, answered_at, score, bonus, cited, feedback, grade_model, "
            "graded_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)", *[row[k] for k in (
                "id", "answer", "answered_at", "score", "bonus", "cited", "feedback", "grade_model", "graded_at")])
        return dict(row)


async def chat(question_id: int, message: str, model: Optional[str] = None) -> str:
    """The tutor's reply to the student's message about a question
    (ADR-0023). Before a grade the tutor clarifies but does not answer;
    after it, the tutor discusses the model answer and the student's answer.
    Both messages are saved only when the reply comes."""
    model = _check_model(model, GRADE_MODEL)
    pool = await get_pool()
    async with pool.acquire() as conn:
        q = await conn.fetchrow("SELECT * FROM questions WHERE id = $1", question_id)
        if q is None:
            raise LookupError(f"No question has id {question_id}.")
        papers = await conn.fetch(f"SELECT {_PAPER_FIELDS} FROM papers WHERE id = ANY($1)", q["paper_ids"])
        history = await conn.fetch("SELECT role, content FROM question_chat WHERE question_id = $1 ORDER BY id",
                                   question_id)
    graded = None if q["graded_at"] is None else (
        f"Model answer: {q['model_answer']}\nKey points:\n" + "\n".join(f"- {k}" for k in q["key_points"] or [])
        + f"\nStudent's answer: {q['answer']}\nScore: {q['score']}/10. Feedback: {q['feedback']}")
    conversation = "\n\n".join(f"{h['role'].capitalize()}: {h['content']}" for h in history)
    prompt = chat_prompt(q["question"], "\n\n".join(_paper_text(dict(p)) for p in papers), graded,
                         conversation, message)
    reply = await asyncio.to_thread(generate, prompt, model)
    async with pool.acquire() as conn:
        await conn.executemany("INSERT INTO question_chat (question_id, role, content, model) VALUES ($1, $2, $3, $4)",
                               [(question_id, "student", message, None), (question_id, "tutor", reply, model)])
    return reply


def choose_daily_paper(rows: list[dict[str, Any]]) -> int:
    """The id of the next paper of the day; rows: id, importance,
    summarized, last (the last day it was featured, or None)."""
    if not rows:
        raise ValueError("The library is empty: add papers first.")
    oldest = min(r["last"] or date.min for r in rows)
    rows = [r for r in rows if (r["last"] or date.min) == oldest]
    rows = [r for r in rows if r["summarized"]] or rows
    return random.choice(_tier(rows))


async def daily_paper() -> Optional[dict[str, Any]]:
    """Today's brief, or None."""
    row = await (await get_pool()).fetchrow("SELECT * FROM daily_papers WHERE day = $1", date.today())
    return dict(row) if row else None


async def write_daily_paper(model: Optional[str] = None) -> dict[str, Any]:
    """A new brief for today, of the next paper; it replaces today's brief."""
    model = _check_model(model, QUESTION_MODEL)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.id, p.importance, p.methods_summary IS NOT NULL AS summarized, "
            "       (SELECT max(day) FROM daily_papers d WHERE d.paper_id = p.id) AS last "
            f"FROM papers p WHERE {_LIBRARY}")
        paper = dict(await conn.fetchrow(f"SELECT {_PAPER_FIELDS} FROM papers WHERE id = $1",
                                         choose_daily_paper([dict(r) for r in rows])))
    reply = await asyncio.to_thread(generate, daily_paper_prompt(_paper_text(paper)), model,
                                    schema=DAILY_PAPER_SCHEMA)
    return dict(await pool.fetchrow(
        "INSERT INTO daily_papers (day, paper_id, about, summary, key_points, examiner_questions, model) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (day) DO UPDATE SET paper_id = $2, about = $3, "
        "summary = $4, key_points = $5, examiner_questions = $6, model = $7, created_at = now() RETURNING *",
        date.today(), paper["id"], reply["about"], reply["summary"], reply["key_points"],
        reply["examiner_questions"], model))


def stats(rows: list[dict[str, Any]], attempts: list[dict[str, Any]] = (), weeks: int = 26,
          today: Optional[date] = None) -> dict[str, Any]:
    """The statistics of the questions: points and the radars by the latest
    graded attempt of each question (rows); the attempts of each day (local
    date of the answer), with the current and the longest streak of days
    with an attempt, from all attempts (a redo is practice too)."""
    today = today or date.today()
    graded = [q for q in rows if q["graded_at"]]
    per_day = Counter(a["answered_at"].astimezone().date() for a in attempts)
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

    def summary(scores: list[int]) -> tuple[int, Optional[float]]:
        return len(scores), sum(scores) / len(scores) if scores else None

    return {
        "points": sum(q["score"] + q["bonus"] for q in graded),
        "answered": len(graded),
        "today": per_day[today],
        "mean": sum(q["score"] for q in graded) / len(graded) if graded else None,
        "current": current,
        "longest": longest,
        "calendar": calendar,
        "axes": {name: {a: summary([q["score"] for q in graded if f"{name}: {a}" in (q["topics"] or [])])
                        for a in axes} for name, axes in AXIS_SETS.items()},
        "types": {k: summary([q["score"] for q in graded if q.get("question_type") == k]) for k in QUESTION_TYPES},
    }


if __name__ == "__main__":
    from datetime import datetime

    t = date(2026, 10, 14)  # a Wednesday

    def q(days_ago: int, score: int, topics: list[str], kind: str = "Explain") -> dict[str, Any]:
        at = datetime.combine(t - timedelta(days=days_ago), datetime.min.time()).replace(hour=12).astimezone()
        return {"graded_at": at, "answered_at": at, "score": score, "bonus": 1, "topics": topics,
                "question_type": kind}

    weak = "Field competencies: Learning with weak labels"
    rows = [q(0, 8, [weak]), q(0, 6, [], "Compare"), q(1, 4, [weak, "Exam areas: ML foundations"]),
            q(5, 9, []), q(6, 9, []), q(7, 9, []), {"graded_at": None}]
    attempts = [r for r in rows if r["graded_at"]]
    s = stats(rows, attempts, today=t)
    assert (s["points"], s["answered"], s["today"]) == (51, 6, 2), s
    assert (s["current"], s["longest"]) == (2, 3), s
    # A redo on day 2 (of the question of day 1) joins the two runs; points stay by the latest attempt.
    s = stats(rows, attempts + [q(2, 3, [])], today=t)
    assert (s["points"], s["current"], s["longest"], s["today"]) == (51, 3, 3, 2), s
    assert s["axes"]["Field competencies"]["Learning with weak labels"] == (2, 6.0)
    assert s["axes"]["Exam areas"]["ML foundations"] == (1, 4.0)
    assert s["types"]["Explain"] == (5, 7.8) and s["types"]["Compare"] == (1, 6.0)
    assert s["calendar"][-1][-1] == (t, 2) and len(s["calendar"][-1]) == 3  # Mon to Wed
    assert all(len(w) == 7 for w in s["calendar"][:-1]) and s["calendar"][0][0][0].weekday() == 0
    assert stats([], today=t)["current"] == 0
    assert stats([q(1, 5, [])], [q(1, 5, [])], today=t)["current"] == 1  # an answer yesterday keeps the streak
    # The weakest: an axis with no answers, never the answered one; a type never practised.
    for _ in range(20):
        axis, kind = _weakest(rows, "Field competencies")
        assert axis != "Learning with weak labels" and kind not in ("Explain", "Compare"), (axis, kind)
    assert _weakest(rows, "Exam areas")[0] != "ML foundations"
    # The paper of the day: never featured first, then a summarized paper: 3 is the only one.
    d = date(2026, 10, 1)
    rows = [{"id": 1, "importance": "crucial", "summarized": True, "last": d},
            {"id": 2, "importance": None, "summarized": False, "last": None},
            {"id": 3, "importance": None, "summarized": True, "last": None},
            {"id": 4, "importance": "crucial", "summarized": False, "last": None}]
    assert {choose_daily_paper(rows) for _ in range(20)} == {3}
    # With no summarized paper left among the oldest, the crucial tier mostly.
    picks = [choose_daily_paper(rows[:2] + rows[3:]) for _ in range(200)]
    assert set(picks) == {2, 4} and picks.count(4) > picks.count(2), picks.count(4)
    # All featured: the least recent.
    assert choose_daily_paper([{**rows[0], "last": d}, {**rows[2], "last": date(2026, 9, 1)}]) == 3
    print("ok")
