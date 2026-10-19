# ADR-0026: Notes, a read mark and a bin for each paper

**Status:** Accepted — 2026-10-18

## Context

The user wants to remove papers from the papers list, with a question
before each delete, and to restore a deleted paper for 30 days or another
period that the user selects. On the page of a paper, the user wants
notes of their own and a mark that they read it.

## Decision

1. **Notes and a read mark are columns of `papers`**: `notes` (text) and
   `read_at` (when the user ticked "I have read this paper"; NULL: not
   read). The paper page saves the notes with a button and warns before it
   leaves unsaved notes. The papers list shows the read mark.
2. **A delete is a soft delete:** `papers.deleted_at`. The list asks first
   (the browser's `confirm`), then moves the paper to the bin. A paper in
   the bin keeps its row, its chunks and its files, so that a restore is
   one `UPDATE`.
3. **A paper in the bin leaves every use:** the lists and counts of the
   interface, `search_papers`, `search_chunks`, the library of triage, the
   PDF queue, the pending summaries and the study pages (`questions._LIBRARY`).
   The lookups (`find_paper`, `find_paper_by_doi` and the others) still see
   it, so that the sweep and the import do not save it again.
4. **The page Bin** (`/bin`) lists the deleted papers with the date when
   each is deleted for good, a Restore button, and the bin days (7 to 365,
   default 30) in the new table `settings` (key `bin_days`). Fewer days ask
   first, because they delete the older papers at once.
5. **The delete for good is lazy:** the papers page and the bin page delete
   each paper older than the bin days, with its folder in `logs/papers/`.
   The tables of the paper cascade; `writing_prompts.paper_id` becomes NULL.
   A timer was rejected: nothing shows a paper in the bin, so the date of
   the final delete does not matter.

## Consequences

- After the delete for good, `triaged_hits` still holds the hit, so the
  sweep does not save the paper again.
- A daily question keeps the ids of a paper that is deleted for good; the
  question page shows "(deleted)" for its title.
- The MCP tool `get_paper` returns a paper in the bin by its id, with its
  `deleted_at`, `notes` and `read_at`.
