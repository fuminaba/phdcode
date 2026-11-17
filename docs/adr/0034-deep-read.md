# ADR-0034: Deep read, an exam on one paper by the skills of critical reading

**Status:** Accepted — 2026-10-31

## Context

The user wants to read one paper thoroughly and to find out how well they
understand and can critique it. Literature (ADR-0020) asks one question on
two or three papers and grades one paragraph. The paper of the day
(ADR-0024) writes examiner questions, but nothing asks them or grades the
answers. Nothing stays on one paper, follows up on a weak answer, or
measures the skills of critical reading.

## Decision

1. **A sub-tab Deep read (`/deepread`) of Skill development**: the user
   picks a library paper with full text (chunks), reads it, and starts a
   session. The paper page and the paper of the day link to it. A paper
   with the abstract only is not offered: the methods and the results are
   not in it.
2. **The examiner model reads the whole text and asks one question at a
   time**, in a mix of four areas: comprehension, methods, results and
   statistics, critique. After an answer, it asks a follow-up on the same
   point if the answer is weak or vague (at most two), or a new question.
   If the user names a limitation, it checks the paper and says where the
   paper deals with it. The user can ask a question back; the examiner
   answers it from the paper.
3. **Each new question tests one of 8 skills of critical reading** (the
   wheel, `SKILLS`): state the claim, follow the method, read the numbers,
   test the claim against the evidence, find bias and leakage, judge
   validity and generalization, judge the baselines and ablations, check
   reproducibility and reporting. The code picks the skill: one not asked
   yet in the session, then the weakest of the past sessions (the fewest
   graded questions, then the lowest mean score), as Literature picks its
   axis (ADR-0021). The model picks the area, with the counts of the
   session. After 10 questions it asks nothing new.
4. **"Finish" grades each answered question 0 to 10 in one call**, with
   feedback that quotes the paper; the quote check of Ask (ADR-0030) marks
   each quote as found or not. The same call writes a summary and 0 to 3
   concepts to brush up on (ADR-0033, item 6), and the paper is marked as
   read. Finish again grades again.
5. **The page shows a radar of the mean score of each skill** (scale 0 to
   10, not the count of answers as on Literature: the wheel measures the
   skill), a table of the areas, the brush-up panel and the earlier
   sessions.
6. **Two tables:** `deepread_sessions`, and `deepread_messages` with the
   number of its item (a question with its follow-ups and answers) on each
   message; the examiner message that opens an item holds its area, skill,
   score, feedback and quotes.
7. **The default model is `claude-sonnet-5-5`** (`DEEPREAD_MODEL`); the page
   can choose another route. One model examines and grades a session.

## Consequences

- Each turn sends the whole paper again, because `claude -p` keeps no
  session: the median full text is about 78 000 characters, the longest
  350 000 (about 90 000 tokens). The paper goes in the system message
  (`generate(context=...)`), so that the prompt cache reuses it: a second
  turn on paper 51 read 22 075 of 22 560 tokens from the cache (6 s). In
  the user message it read none.
- The questions target the parts that carry the main claim, not details
  that only test memory. "Start over" deletes a session with its grades
  and starts a new one on the same paper.
- A session that the user leaves unfinished stays open, and the page
  continues the latest one.
- The overview counts a deep read as a paper read, not as a separate habit.

## Alternatives rejected

- **A reading companion that only answers the user.** Ask (ADR-0030)
  covers questions to the library; the aim here is to test the user.
- **Fixed stages in order, and a stage for the connection to the user's
  PhD.** The user chose a mix of the four areas.
- **A grade after each answer.** It would give away the answer to the
  follow-ups; one grade at the end sees the whole exchange.
- **The model picks the skill.** The code picks it, so that the weakest
  skills come first and each session covers different skills.
