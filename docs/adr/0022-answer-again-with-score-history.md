# ADR-0022: A daily question can be answered again, with a score history

**Status:** Accepted — 2026-10-14

## Context

A question answered once tests what the user knew on that day. To learn,
the user wants to answer a question again later and see whether the score
improves. ADR-0020 kept one answer for each question and overwrote it.

## Decision

1. **A table `attempts`** holds each graded answer: the answer, the score,
   the bonus, the citations, the feedback, the grader and the times. A grade
   writes the attempt and the latest answer of the question in one
   transaction. The answers graded before this table were copied in.
2. **The question row keeps the latest attempt.** The points, the radars,
   the means and "Weakest first" use the latest score of each question, so
   they show what the user knows now, and a second attempt cannot add
   points twice.
3. **The calendar and the streaks count attempts:** an answer given again
   is practice on its own day.
4. **The page:** "Answer again" hides the model answer and the key points
   until the next grade; the score history (for example `4 → 7 → 9`) and
   the earlier attempts show with each question; `/questions?q=<id>` opens
   an earlier question to answer it again.

## Consequences

- The latest attempt is in two places (the question row and `attempts`);
  the grade writes both in one transaction.
