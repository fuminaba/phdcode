# ADR-0027: The user labels every judged hit

**Status:** Accepted — 2026-10-18

## Context

ADR-0018 chose triage by the library on Fumi's labels of one week: 81 hits
in a vault checklist, of which 71 could be fetched again. 24 relevant hits
cannot separate two scores (an AUC has an error of about ±0.1), and each
new scorer (another prompt, another model, a trained head) needs the same
test. The sweep keeps only the hits at or above the threshold: a hit below
it leaves its probability in `triaged_hits`, but not its title, abstract
or link. A later test must fetch those again, and on 2026-10-18 arXiv
refused (HTTP 429) the 36 abstracts that a test needed.

## Decision

1. **`triaged_hits` keeps the text of each hit**: `title`, `abstract` and
   `url`, written by the sweep with the probability. A judgement again
   keeps the label.
2. **The user's label is a column of the hit**: `label` (true: relevant,
   false: not relevant, NULL: not labelled) and `labelled_at`. A label
   belongs to the pair (saved search, hit), as in the week-1 checklist.
3. **The page Labels** (`/labels`) lists every judged hit, newest first,
   with its probability, its search, its title and abstract, and the guess
   (relevant at or above the threshold of its search). One click sets
   "relevant" or "not relevant"; a click on the label that is set clears
   it. The page shows the count of labels, the agreement of the guess and
   the AUC of the triage probability on the labels.
4. **The week-1 labels are imported** from the vault checklist
   (`Projects/phdcode/notes/2026-10-12 Triage labels week 1.md`), with the
   titles and links of its 81 lines.
5. **A label does not change the library.** Triage reads the library
   (ADR-0018); a label is a test of triage, so that it must not feed it.
   The user adds a paper to the library as before.

## Consequences

- Each sweep adds unlabelled hits; the page shows how far the labels are
  behind.
- Any scorer can be tested on the same labels, also offline (bonsai2-sysone
  `eval_own.py`).
- The hits of week 1 that were judged before this change have no abstract
  in `triaged_hits`; the page shows the abstract of a saved paper instead.
