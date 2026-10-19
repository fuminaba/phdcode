# ADR-0024: The paper of the day, a brief for the defence

**Status:** Accepted — 2026-10-17

## Context

The user wants to know the papers of the library well enough for the thesis
defence: for each paper, what it is about, a summary, the points to know and
the questions that an examiner could ask. Only 31 of the 104 library papers
have full text and methods and results summaries; the others have an
abstract only.

## Decision

1. **A page "Paper of the day"** (`/paper-of-the-day`, `daily_paper.py`).
   The user asks for it with a click; the model writes the brief once, and
   the page shows it for the rest of the day. "Another paper" replaces the
   brief of the day. No timer: a model call starts only on a click, as for
   the daily question, so the GPU tenant and the Claude usage stay the
   user's choice.
2. **The choice of the paper** (`daily_paper.choose`): the papers that were
   featured least recently (never featured first); of those, a paper with
   a methods summary if one is left; then a tier by
   `questions.TIER_WEIGHTS`, as for the daily question.
3. **The brief** (`prompts.daily_paper_prompt`): about, summary, 3 to 5 key
   points for the defence (the claim, the evidence, the limitations, the
   meaning for computational pathology) and 2 or 3 examiner questions. The
   input is the abstract and the summaries (`questions._paper_text`), not
   the full text, so that the local model fits.
4. **A table `daily_papers`**, one row for each day. The earlier briefs stay
   on the page.

## Consequences

- A paper with an abstract only gives a thin brief; the page says so.
- The model choice is shared with the page Questions (same list, same saved
  choice in the browser).
