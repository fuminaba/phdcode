# ADR-0021: The daily question is steered by a competency and a question type

**Status:** Accepted — 2026-10-14
**Supersedes:** item 4 (Topics) of [ADR-0020](0020-daily-question.md)

## Context

ADR-0020 tagged each question with the topics of the interest profile: what
the papers are about. The radar then showed what was asked, not what the
user must know. A PhD needs competencies (for example, evaluation and
statistics) and exam areas (for example, research design), and the skill
to use them (to explain, compare, critique, design, apply). The papers were
picked first and the topic was only a label, so the radar did not steer the
practice.

## Decision

1. **Sets of axes** (`questions.AXIS_SETS`): "Field competencies" (8
   competencies of computational pathology for cancer) and "Exam areas" (8
   areas of a comprehensive exam). Each axis has a description that the
   model and the embedding read. The page shows the radar of the set that
   the user selects.
2. **Each question tests one axis and one question type**
   (`questions.QUESTION_TYPES`: Explain, Compare, Critique, Design, Apply).
   The user chooses them on the page, or the weakest are taken: the axis of
   the selected set with the fewest graded answers (then the lowest mean
   score), and the least practised type.
3. **The papers follow the axis:** the tier is picked as before (ADR-0020),
   then the first paper is one of the 5 papers of the tier nearest to the
   axis description by the concept embedding, then its 2 nearest library
   papers.
4. **The model tags every set:** one or two axes of each set, in
   `questions.topics` as `<set>: <axis>`; the steered axis is always one.
   A label outside the sets is dropped.
5. **Key points:** the question model writes the 3 to 5 points that a
   strong answer must contain. The page shows them with the model answer,
   and the grader scores mostly by them.
6. The question names no paper, no author and no id; it gives the facts
   that the student needs. The prompt states the rule last (one of two
   Haiku 5.5 questions broke it when the rule was in the middle).

## Consequences

- An axis that the library seldom covers (for example, ML foundations)
  gets weak questions until the library has sources for it, such as
  textbooks.
- The axes are code (`questions.AXIS_SETS`). A new set is a new entry; its
  radar is empty until new questions are tagged with it.
- Questions of ADR-0020 keep their old topic tags, which no radar shows.
