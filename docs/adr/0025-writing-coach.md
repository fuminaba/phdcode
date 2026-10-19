# ADR-0025: A writing coach in the interface

**Status:** Accepted — 2026-10-17

## Context

The user wants to practise scientific writing: a task of one or two
paragraphs, a review of the answer by criteria such as clarity and
conciseness, and advice that improves the writer, not only the text.

## Decision

1. **A page "Writing"** (`/writing`, `writing.py`). The model writes a
   task of one of seven kinds (`writing.KINDS`: plain-language summary,
   abstract, motivation, methods, results, argument, reply to a reviewer),
   chosen by the user or the least practised. The task names the reader
   and a word limit (100 to 300).
2. **A library paper is the subject, and the task gives its facts**, so
   that the task tests the writing, not the memory. The paper is a random
   library paper, with a methods summary first.
3. **The review** (`prompts.writing_review_prompt`): a score from 0 to 10
   for each of six criteria (`writing.CRITERIA`: clarity, conciseness,
   structure, precision, audience, grammar and style), the strengths, the 2
   or 3 improvements that help the writer most, 2 to 5 sentence edits
   (before, after, why) and a rewrite of the whole answer. The overall
   score is the mean of the criteria. The prompt asks for 5 to 8 for a
   capable writer, so that the scores can show progress.
4. **Revise:** the user can answer the same task again; each answer is a
   row of `writing_attempts`, and the review of a revision sees the earlier
   answer and its improvements. The page shows the change of each score.
5. **Statistics:** the mean overall score and the mean of each criterion
   over all answers and over the last 5, with the change of the last 5
   against the answers before them (shown after more than 5 answers), and
   the answers and the mean score of each kind.
6. **JSONB for the scores and the edits**, with a codec in
   `memory.db._init_connection`, because the criteria can change and the
   old rows must stay readable.

## Consequences

- An answer is saved only with its review; if the review fails, the text
  stays in the page.
- The scores of different reviewer models are not comparable; the page
  names the reviewer of each answer.
