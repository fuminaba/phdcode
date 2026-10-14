# ADR-0020: Daily questions, graded by a model, in the interface

**Status:** Accepted — 2026-10-14

## Context

The user must know the papers of the library well enough to discuss them,
for example in a committee meeting or a comprehensive exam. Reading does not
test that; a question that needs a written answer does. The user marks the
papers to know best as crucial (ADR-0019).

ADR-0019 let the interface write one column. A question needs a text box and
a grade that comes back at once, so the interface must also write answers
and call the model.

## Decision

1. **A question when the user asks for one** on the page `/questions`: "Ask
   a question", "Skip: new question" (the open question stays as skipped),
   or "Next question" after a grade. No timer and no limit for each day. The
   table `questions` holds one row for each question, with the local date.
2. **The papers of a question** (`questions._pick`): a tier by the weights
   crucial 0.7, recommended 0.2, the rest of the library 0.1, among the tiers
   that have papers; one paper of the tier at random; then its 2 nearest
   library papers by the concept embedding. Papers of the questions of the
   last 14 days are not picked while others are left. The library is the
   papers that the user added or marked, not the sweep's.
3. **The question model and the grader** are chosen on the page from the
   routes of `LLM_ROUTING` (local or hosted); the defaults are
   `QUESTION_MODEL` and `GRADE_MODEL` (both default to `SUMMARY_MODEL`). Each
   row records the two models. The question model writes the question and a model answer that cites
   the papers as `[P<id>]`, and grades the answer: a score from 0 to 10, and
   the library papers that the answer cites correctly. The bonus is one point
   for each, at most 3. The grader reads the titles of the whole library, so
   a correct citation outside the question's papers counts.
4. **Topics:** the question model tags each question with one or two of the
   8 topics of the interest profile (`questions.TOPICS`, T1 to T8); a tag
   outside the list is dropped.
5. **Statistics** on the page, from the graded questions: points, answers,
   answers today, mean score, the current and the longest streak of days with
   an answer, a calendar of the answers of each day (26 weeks), and a radar of
   the answers in each topic, with a table of the count and the mean score.
6. **The model call runs in the interface process** (in a thread). If the
   model server cannot answer (the GPU is cleared for training), the page
   says so; a submitted answer stays saved and can be graded again.

## Rejected

- A daily note in the vault: the grade comes only at the next run.
- An MCP tool: it needs an open harness session for each answer.
- The Claude API as the grader: better grades, but a key, a cost, and the
  user's answers leave the server. The local model keeps it free and private.

## Consequences

- A hosted model on the page sends the papers and the answer to its
  provider, and can cost money; each device on the tailnet can choose it.
- Claude is a route with `'provider': 'claude-code'`: `generation._claude`
  runs `claude -p` on the user's Claude login (no API key; the use counts
  toward the plan's limits), with no tools, settings, hooks, plugins or MCP
  servers, in a neutral folder, so that no CLAUDE.md enters the prompt. The
  Claude Code login of the server must stay valid.
- `questions.TOPICS` is a copy of the topics of the interest profile: a
  change of the saved searches does not change it.
- The interface can now start llama-server through its socket (ADR-0017): a
  click uses the GPU unless it is cleared.
- The quality of the questions and the grades of Bonsai 2 27B is not yet
  measured against the user's own judgement.
