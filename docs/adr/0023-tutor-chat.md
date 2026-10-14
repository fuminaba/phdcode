# ADR-0023: A tutor chat under the daily question

**Status:** Accepted — 2026-10-14

## Context

The user wants to ask for clarification while working on a daily question,
and to discuss the grade afterwards. A chat must not give the answer away
before the user answers, and must not lose an answer draft on the page.

## Decision

1. **A chat under each question** ("Ask the model"), with the grader model
   of the page (`questions.chat`). The conversation is saved in a table
   `question_chat` (role `student` or `tutor`), with the model of each reply.
2. **Two rules by the state of the question** (`prompts.chat_prompt`):
   before a grade, the tutor clarifies the question, its terms and its
   background, and names useful papers, but does not give the answer or its
   key points (the prompt does not contain them); after a grade, the prompt
   has the model answer, the key points, the latest answer, the score and
   the feedback, and the tutor discusses them freely.
3. **One prompt per reply:** the papers, the rule, the conversation so far
   and the new message, so that each route works, also `claude -p`.
4. **No reload:** the route returns the reply as HTML (Markdown without raw
   HTML; `[P12]` links to the paper), and the page appends it, so that an
   answer draft stays. Both messages are saved only when the reply comes.

## Consequences

- While the user answers a graded question again, the chat follows the
  "after a grade" rule: it can show the model answer if the user asks.
- The chat does not change the score.
