"""The writing coach (ADR-0025): a model writes a task of one or two
paragraphs, of one of KINDS, with a library paper as the subject and its
facts in the task, so that the task tests the writing, not the memory. The
user answers; the model scores the answer on each of CRITERIA (0 to 10),
names the strengths, the most useful improvements and a few sentence edits,
and rewrites the answer. The user can revise the answer; each attempt stays,
and the review of a revision sees the one before.

The kind is the user's choice, or the least practised one. The models are
those of questions.models().
"""
import asyncio
import random
from collections import Counter
from datetime import date
from typing import Any, Optional

from .generation import generate
from .memory import get_pool
from .prompts import writing_prompt_prompt, writing_review_prompt
from .questions import GRADE_MODEL, QUESTION_MODEL, _STUDY_PAPERS, _PAPER_FIELDS, _check_model, _paper_text

WRITING_PROMPT_SCHEMA = {
    "prompt": (str, ...),
    "audience": (str, ...),
    "max_words": (int, ...),
}

WRITING_REVIEW_SCHEMA = {
    "scores": (dict[str, int], ...),
    "strengths": (str, ...),
    "improvements": (list[str], ...),
    "edits": (list[dict[str, str]], ...),
    "revision": (str, ...),
}

KINDS = {
    "Plain-language summary": "explain a finding to a reader with no science background, such as a patient",
    "Abstract": "one paragraph with the background, the method, the main result and the conclusion",
    "Motivation": "the first paragraph of an introduction: why the problem matters and what is missing",
    "Methods": "describe a method so that another researcher can repeat it",
    "Results": "report results with their numbers, exactly, and only the conclusions that the data support",
    "Argument": "take a position on an open question and defend it with evidence",
    "Reply to a reviewer": "answer a reviewer's criticism politely, with evidence, and say what changes",
}
CRITERIA = {
    "Clarity": "each sentence has one clear meaning, and the reader follows it at the first reading",
    "Conciseness": "no unnecessary words, sentences or repetition; within the word limit",
    "Structure": "a logical order, one idea for each paragraph, clear links between the sentences",
    "Precision": "exact terms and numbers; the claims are as strong as the evidence, not more or less",
    "Audience": "the vocabulary, the detail and the tone fit the reader",
    "Grammar and style": "correct grammar and punctuation, active voice, consistent tense",
}
RECENT = 5  # the attempts of the "recent" column of the statistics


def least_practised(attempts: list[dict[str, Any]]) -> str:
    """The kind with the fewest attempts; ties at random."""
    n = Counter(a["kind"] for a in attempts)
    return min(KINDS, key=lambda k: (n[k], random.random()))


def clean_scores(scores: dict[str, Any]) -> dict[str, int]:
    """The scores of the reply by CRITERIA, in their order, clamped to 0 to
    10; a key matches in any case. A missing criterion raises."""
    by_lower = {k.strip().lower(): v for k, v in scores.items()}
    missing = [c for c in CRITERIA if c.lower() not in by_lower]
    if missing:
        raise ValueError(f"The review has no score for {missing}.")
    return {c: max(0, min(10, int(by_lower[c.lower()]))) for c in CRITERIA}


def overall(scores: dict[str, int]) -> float:
    return sum(scores.values()) / len(scores)


async def ask(kind: Optional[str] = None, model: Optional[str] = None) -> dict[str, Any]:
    """A new writing task of kind (the least practised when None)."""
    model = _check_model(model, QUESTION_MODEL)
    if kind is not None and kind not in KINDS:
        raise ValueError(f"Unknown kind {kind!r}.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        kind = kind or least_practised([dict(r) for r in await conn.fetch(
            "SELECT p.kind FROM writing_attempts a JOIN writing_prompts p ON p.id = a.prompt_id")])
        # ponytail: any summarized library paper at random; steer by importance if the subjects repeat.
        paper = await conn.fetchrow(
            f"SELECT {_PAPER_FIELDS} FROM papers WHERE {_STUDY_PAPERS} ORDER BY methods_summary IS NULL, random() LIMIT 1")
    if paper is None:
        raise ValueError("The library is empty: add papers first.")
    reply = await asyncio.to_thread(generate, writing_prompt_prompt(f"{kind}: {KINDS[kind]}", _paper_text(dict(paper))),
                                    model, schema=WRITING_PROMPT_SCHEMA)
    async with pool.acquire() as conn:
        return dict(await conn.fetchrow(
            "INSERT INTO writing_prompts (asked_on, kind, paper_id, prompt, audience, max_words, model) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
            date.today(), kind, paper["id"], reply["prompt"], reply["audience"],
            max(100, min(300, reply["max_words"])), model))


async def review(prompt_id: int, text: str, model: Optional[str] = None) -> dict[str, Any]:
    """Reviews text as an answer to the prompt and saves it as an attempt;
    nothing is saved if the review fails."""
    model = _check_model(model, GRADE_MODEL)
    pool = await get_pool()
    async with pool.acquire() as conn:
        p = await conn.fetchrow("SELECT * FROM writing_prompts WHERE id = $1", prompt_id)
        if p is None:
            raise LookupError(f"No writing task has id {prompt_id}.")
        last = await conn.fetchrow("SELECT * FROM writing_attempts WHERE prompt_id = $1 ORDER BY id DESC LIMIT 1",
                                   prompt_id)
    earlier = None if last is None else (
        f"Earlier answer: {last['text']}\nScores: {last['scores']}\nImprovements:\n"
        + "\n".join(f"- {i}" for i in last["improvements"]))
    reply = await asyncio.to_thread(
        generate, writing_review_prompt(f"{p['kind']}: {KINDS.get(p['kind'], '')}", p["prompt"], p["audience"],
                                        p["max_words"], CRITERIA, text, earlier), model, schema=WRITING_REVIEW_SCHEMA)
    edits = [{k: e.get(k, "") for k in ("before", "after", "why")} for e in reply["edits"]]
    async with pool.acquire() as conn:
        return dict(await conn.fetchrow(
            "INSERT INTO writing_attempts (prompt_id, text, scores, strengths, improvements, edits, revision, model) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
            prompt_id, text, clean_scores(reply["scores"]), reply["strengths"], reply["improvements"], edits,
            reply["revision"], model))


def stats(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """attempts: oldest first, each with its kind. The mean overall score
    and the mean of each criterion: over all attempts, over the last RECENT,
    and the change of the last RECENT against the attempts before them
    (None until there are more than RECENT); the attempts and the mean
    overall score of each kind."""
    def mean(values: list[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    def means(score) -> tuple[Optional[float], Optional[float], Optional[float]]:
        recent, earlier = mean([score(a) for a in attempts[-RECENT:]]), mean([score(a) for a in attempts[:-RECENT]])
        return mean([score(a) for a in attempts]), recent, None if earlier is None else recent - earlier

    return {
        "attempts": len(attempts),
        "tasks": len({a["prompt_id"] for a in attempts}),
        "overall": means(lambda a: overall(a["scores"])),
        "criteria": {c: means(lambda a, c=c: a["scores"][c]) for c in CRITERIA},
        "kinds": {k: (len(s := [overall(a["scores"]) for a in attempts if a["kind"] == k]), mean(s)) for k in KINDS},
    }


if __name__ == "__main__":
    assert clean_scores({"clarity": 7, "Conciseness ": 11, "Structure": 5, "Precision": -1, "AUDIENCE": 6,
                         "Grammar and style": 8}) == {"Clarity": 7, "Conciseness": 10, "Structure": 5,
                                                      "Precision": 0, "Audience": 6, "Grammar and style": 8}
    try:
        clean_scores({"Clarity": 7})
        raise AssertionError("a missing criterion must raise")
    except ValueError:
        pass
    full = dict.fromkeys(CRITERIA, 6)
    attempts = [{"prompt_id": 1, "kind": "Abstract", "scores": full}] * 6 + [
        {"prompt_id": 2, "kind": "Methods", "scores": dict.fromkeys(CRITERIA, 9)}]
    s = stats(attempts)
    assert (s["attempts"], s["tasks"]) == (7, 2)
    assert s["criteria"]["Clarity"] == (sum([6] * 6 + [9]) / 7, (6 * 4 + 9) / 5, (6 * 4 + 9) / 5 - 6)
    assert s["overall"][2] == s["criteria"]["Clarity"][2]  # all criteria move together here
    assert s["kinds"]["Abstract"] == (6, 6.0) and s["kinds"]["Argument"] == (0, None)
    for _ in range(20):
        assert least_practised(attempts) not in ("Abstract", "Methods")
    assert stats([])["overall"] == (None, None, None)
    assert stats(attempts[:RECENT])["overall"][2] is None  # nothing before the last RECENT
    print("ok")
