"""The puzzles (ADR-0033): daily practice of coding, and of statistics and
machine learning. One module and one pair of tables for both TRACKS.

A model writes a puzzle of one topic and one type of its track (the user's
choice, or the least practised). A coding puzzle comes with starter code,
tests and a reference solution; the tests must pass on the reference before
the puzzle is saved, so that a failed test is the user's, not the model's.
A stats puzzle comes with a model answer and its key points.

The user answers: code, or text. The tests of a coding puzzle run first
(run_tests), then a model scores the answer 0 to 10 with the test results as
evidence and gives feedback. Each answer stays as an attempt; the page and
the statistics use the latest attempt of each puzzle.

Python is the only language. Another one needs an entry in RUNNERS: a
function that runs the tests of that language on an answer.
"""
import asyncio
import json
import os
import random
import resource
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .generation import generate
from .memory import get_pool
from .prompts import puzzle_grade_prompt, puzzle_prompt
from .questions import GRADE_MODEL, QUESTION_MODEL, _check_model, clean_topics, days, known_topics

TRACKS = {
    "coding": {
        "name": "Coding",
        "topics": {
            "Algorithms": "a classic algorithm or data structure: arrays, strings, hashing, graphs, dynamic programming",
            "Numerics": "vectorised numpy: indexing, broadcasting, reductions, numerical stability",
            "Data wrangling": "parse, clean, group and join tabular or text data with the standard library or pandas",
            "Image processing": "operations on image arrays as in digital pathology: masks, connected components, "
                                "filters, tiles",
            "ML from scratch": "a small piece of machine learning in numpy: a loss, a metric, a layer, an optimizer step",
        },
        "types": {
            "Implement": "write a function from a specification",
            "Debug": "the starter code has bugs: find and fix them",
            "Refactor": "the starter code works but is unclear or slow: rewrite it with the same behaviour",
            "Edge cases": "a function whose difficulty is in the edge cases: empty inputs, ties, overflow, NaN",
        },
    },
    "stats": {
        "name": "Stats & ML",
        "topics": {
            "Probability": "conditional probability, Bayes' rule, distributions, expectation",
            "Inference": "estimation, confidence intervals, hypothesis tests, multiple comparisons",
            "Modelling": "regression, survival analysis, mixed effects, confounding",
            "Study design": "sampling, power, bias, leakage, train-validation-test splits",
            "Model evaluation": "metrics, calibration, ROC and PR curves, class imbalance, uncertainty",
            "Learning theory": "bias and variance, regularisation, generalisation, optimisation",
            "Deep learning": "architectures, training dynamics, weak supervision, multiple instance learning",
        },
        "types": {
            "Calculate": "compute a number from given data, with the working",
            "Explain": "explain why a method or a result behaves as it does",
            "Critique": "find the flaw in a described analysis or claim",
            "Design": "design an analysis or an experiment that answers a question",
            "Diagnose": "a described training run or analysis goes wrong: say why and what to change",
        },
    },
}
LANGUAGES = ("python",)
TEST_TIMEOUT_S = 20
MIN_TESTS = 3

PUZZLE_SCHEMA = {
    "prompt": (str, ...),
    "reference": (str, ...),
    "key_points": (list[str], ...),
    "starter": (str, ""),
    "tests": (str, ""),
}
GRADE_SCHEMA = {
    "score": (int, ...),
    "feedback": (str, ...),
    "review_topics": (list[str], []),
}

# Runs in the folder of the answer: imports solution.py and tests.py, calls
# each test_ function, and writes the results to results.json (not stdout,
# which the answer may print to).
_HARNESS = """
import json, sys, traceback
sys.path.insert(0, ".")
results = []
try:
    import solution, tests
except BaseException:
    results.append({"name": "import", "ok": False, "error": traceback.format_exc(limit=3)[-1500:]})
else:
    for name in sorted(n for n in dir(tests) if n.startswith("test_")):
        try:
            getattr(tests, name)()
            results.append({"name": name, "ok": True, "error": ""})
        except BaseException as e:
            results.append({"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"[:500]})
json.dump(results, open("results.json", "w"))
"""


def _limits() -> None:
    # ponytail: a CPU, memory and file-size limit, not a sandbox: the answer
    # runs as the user, with the network. The code is the user's own and the
    # page is on the Tailscale address only; use a container (docker run
    # --network none) if the page gets other users.
    resource.setrlimit(resource.RLIMIT_CPU, (TEST_TIMEOUT_S, TEST_TIMEOUT_S))
    resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 20, 64 << 20))


def _run_python(code: str, tests: str) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in (("solution.py", code), ("tests.py", tests), ("harness.py", _HARNESS)):
            Path(tmp, name).write_text(text)
        # One thread for numpy: the machine is shared.
        env = {"PATH": os.environ.get("PATH", ""), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
               "MKL_NUM_THREADS": "1"}
        try:
            subprocess.run([sys.executable, "-I", "harness.py"], cwd=tmp, env=env, preexec_fn=_limits,
                           capture_output=True, timeout=TEST_TIMEOUT_S + 5)
        except subprocess.TimeoutExpired:
            return [{"name": "all tests", "ok": False, "error": f"timeout after {TEST_TIMEOUT_S} s"}]
        results = Path(tmp, "results.json")
        if not results.is_file():  # killed by a limit, or the answer called sys.exit
            return [{"name": "all tests", "ok": False, "error": "the run stopped before the tests finished"}]
        return json.loads(results.read_text())


RUNNERS = {"python": _run_python}


def run_tests(language: str, code: str, tests: str) -> list[dict[str, Any]]:
    """[{"name", "ok", "error"}] of each test of tests on code."""
    return RUNNERS[language](code, tests)


async def ask(track: str, topic: Optional[str] = None, puzzle_type: Optional[str] = None,
              model: Optional[str] = None) -> dict[str, Any]:
    """A new puzzle of track, on topic and puzzle_type (the least practised
    when None). A coding puzzle whose tests fail on its own reference is
    asked once more, then refused."""
    model = _check_model(model, QUESTION_MODEL)
    spec = TRACKS[track]
    pool = await get_pool()
    rows = await pool.fetch("SELECT topic, puzzle_type FROM puzzles p WHERE track = $1 AND "
                            "EXISTS (SELECT 1 FROM puzzle_attempts a WHERE a.puzzle_id = p.id)", track)
    topic = topic or least_practised([r["topic"] for r in rows], spec["topics"])
    puzzle_type = puzzle_type or least_practised([r["puzzle_type"] for r in rows], spec["types"])
    language = "python" if track == "coding" else None
    prompt = puzzle_prompt(track, f"{topic}: {spec['topics'][topic]}",
                           f"{puzzle_type}: {spec['types'][puzzle_type]}", language)
    for _ in range(2):
        reply = await asyncio.to_thread(generate, prompt, model, schema=PUZZLE_SCHEMA)
        if language is None:
            break
        results = await asyncio.to_thread(run_tests, language, reply["reference"], reply["tests"])
        if len(results) >= MIN_TESTS and all(r["ok"] for r in results):
            break
    else:
        raise ValueError(f"The model's tests failed on its own solution twice: {results}")
    return dict(await pool.fetchrow(
        "INSERT INTO puzzles (asked_on, track, topic, puzzle_type, language, prompt, starter, tests, reference, "
        "key_points, model) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING *",
        date.today(), track, topic, puzzle_type, language, reply["prompt"], reply["starter"] or None,
        reply["tests"] or None, reply["reference"], reply["key_points"], model))


async def grade(puzzle_id: int, answer: str, model: Optional[str] = None) -> dict[str, Any]:
    """Runs the tests (coding), scores the answer, and saves it as an attempt;
    nothing is saved if the model fails."""
    model = _check_model(model, GRADE_MODEL)
    pool = await get_pool()
    p = await pool.fetchrow("SELECT * FROM puzzles WHERE id = $1", puzzle_id)
    if p is None:
        raise LookupError(f"No puzzle has id {puzzle_id}.")
    results = await asyncio.to_thread(run_tests, p["language"], answer, p["tests"]) if p["tests"] else None
    async with pool.acquire() as conn:
        known = await known_topics(conn, "SELECT a.review_topics FROM puzzle_attempts a JOIN puzzles z ON "
                                         "z.id = a.puzzle_id WHERE z.track = $2 ORDER BY a.id DESC LIMIT $1", p["track"])
    reply = await asyncio.to_thread(generate, puzzle_grade_prompt(dict(p), answer, results, known), model,
                                    schema=GRADE_SCHEMA)
    return dict(await pool.fetchrow(
        "INSERT INTO puzzle_attempts (puzzle_id, answer, tests, score, feedback, review_topics, model) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
        puzzle_id, answer, results, max(0, min(10, reply["score"])), reply["feedback"],
        clean_topics(reply["review_topics"]), model))


def least_practised(done: list[str], options: dict[str, str]) -> str:
    """The option with the fewest entries in done; ties at random."""
    n = Counter(done)
    return min(options, key=lambda k: (n[k], random.random()))


def stats(track: str, latest: list[dict[str, Any]], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """latest: the latest attempt of each puzzle, with its topic, puzzle_type
    and tests; attempts: every attempt (graded_at), for the streak. The count
    and mean score of each topic and type, the mean score, and the share of
    tests passed."""
    def summary(scores: list[int]) -> tuple[int, Optional[float]]:
        return len(scores), sum(scores) / len(scores) if scores else None

    tests = [t for a in latest for t in a["tests"] or []]
    return {
        **days(Counter(a["graded_at"].astimezone().date() for a in attempts)),
        "answered": len(latest),
        "mean": summary([a["score"] for a in latest])[1],
        "passed": sum(t["ok"] for t in tests) / len(tests) if tests else None,
        "topics": {k: summary([a["score"] for a in latest if a["topic"] == k]) for k in TRACKS[track]["topics"]},
        "types": {k: summary([a["score"] for a in latest if a["puzzle_type"] == k]) for k in TRACKS[track]["types"]},
    }


if __name__ == "__main__":
    from datetime import datetime

    tests = "from solution import add\n\ndef test_small():\n    assert add(1, 2) == 3\n\ndef test_zero():\n    assert add(0, 0) == 0\n"
    ok = run_tests("python", "def add(a, b):\n    print('noise')\n    return a + b\n", tests)
    assert [r["ok"] for r in ok] == [True, True], ok
    bad = run_tests("python", "def add(a, b):\n    return a - b\n", tests)
    assert [r["name"] for r in bad if not r["ok"]] == ["test_small"], bad
    assert run_tests("python", "def add(a, b) return", tests)[0]["name"] == "import"
    assert run_tests("python", "import sys\nsys.exit(0)\n", tests)[0]["name"] == "import"
    assert run_tests("python", "import os\nos._exit(0)\n", tests)[0]["name"] == "all tests"
    assert run_tests("python", "def add(a, b):\n    while True: pass\n", tests)[0]["ok"] is False
    for _ in range(20):
        assert least_practised(["Algorithms", "Numerics"], TRACKS["coding"]["topics"]) not in ("Algorithms", "Numerics")
    now = datetime.now().astimezone()
    latest = [{"topic": "Numerics", "puzzle_type": "Debug", "score": 8, "graded_at": now,
               "tests": [{"ok": True}, {"ok": False}]},
              {"topic": "Numerics", "puzzle_type": "Implement", "score": 6, "graded_at": now, "tests": None}]
    s = stats("coding", latest, latest)
    assert (s["answered"], s["mean"], s["passed"], s["today"]) == (2, 7.0, 0.5, 2), s
    assert s["topics"]["Numerics"] == (2, 7.0) and s["types"]["Debug"] == (1, 8.0) and s["topics"]["Algorithms"] == (0, None)
    assert stats("stats", [], [])["mean"] is None
    print("ok")
