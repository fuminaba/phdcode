# ADR-0033: Daily puzzles in coding and in statistics and machine learning

**Status:** Accepted — 2026-10-24

## Context

Skill development had two pages: the daily question on the library
(ADR-0020) and the writing coach (ADR-0025). The user wants to keep and
improve two more skills with a daily puzzle: coding (Python first, other
languages later), with an answer that runs against prepared tests; and
statistics and machine learning, with a written answer. Each needs types of
puzzles, as the daily question has.

## Decision

1. **One module (`puzzles.py`), one template (`puzzles.html`) and one pair
   of tables (`puzzles`, `puzzle_attempts`) serve both tracks**: `coding`
   (page Coding, `/coding`) and `stats` (page Stats & ML, `/stats`). Each
   track has topics and types in `TRACKS`; a new puzzle takes the user's
   choice, or the least practised topic and type of the track.
2. **A coding puzzle comes with starter code, 4 to 8 tests and a reference
   solution.** The tests must pass on the reference before the puzzle is
   saved; if they fail, the model writes the puzzle once more, then the
   request fails. So a failed test is a fault in the answer, not in the
   tests.
3. **The tests run without a model:** the answer is `solution.py`, the
   tests are `tests.py` (plain `assert` in `test_` functions), and a small
   harness calls each test in a subprocess of the interface's Python, in a
   temporary folder, with a time limit (20 s of CPU), a memory limit (4 GB),
   a file-size limit and one numpy thread. pytest is not needed.
4. **A model scores each answer 0 to 10** with the test results as
   evidence (a failed test caps the score at 6) and writes feedback. A stats
   answer is scored against the reference answer and its key points. Each
   answer stays as an attempt; the statistics use the latest attempt of
   each puzzle. The tests and the reference show after the first answer.
5. **Languages:** the puzzle stores its `language`. `RUNNERS` maps a
   language to the function that runs its tests; Python is the only entry.
6. **Each grade names the concepts to brush up on** (`review_topics`, 0 to
   3 short names) in the same model call, for the puzzles and for the daily
   question of Literature. The grader sees the names of the last 100
   attempts of the page and reuses one when it fits, so that a repeated gap
   gets one name. Each page shows the names under each score, and a panel
   "Brush up on" counts the names of the last 30 attempts, the most frequent
   first. Older answers have no names.
7. **The page Questions is now Literature** (the label of the sub-tab and
   the title); its URL stays `/questions`. The sub-tabs of Skill
   development are Literature, Writing, Coding, Stats & ML (amends
   ADR-0031, item 1).

## Consequences

- An answer runs as the user, with the network: the limits stop a runaway
  loop, not a hostile program. The interface is on the Tailscale address
  only and the code is the user's own. A page with other users needs a
  container (`docker run --network none`). Ubuntu blocks unprivileged
  network namespaces (`unshare -rn` fails), so that is not an option here.
- A puzzle costs one model call, two if the tests fail; an answer costs one.
  With Claude Haiku 5.5: about 50 s for a coding puzzle, 36 s for a stats
  puzzle (2026-10-24).
- The activity of the overview does not count puzzles yet.

## Alternatives rejected

- **Two modules and two pairs of tables.** The tracks differ only in the
  tests; the topics, types, attempts and statistics are the same.
- **An agent that runs the tests.** The tests are deterministic, so a
  subprocess runs them, and one model call scores the result (ADR-0016: no
  agent loop in this repo).
- **Tests that the model writes at the time of the answer.** They would
  change between answers, and nothing checks them.
