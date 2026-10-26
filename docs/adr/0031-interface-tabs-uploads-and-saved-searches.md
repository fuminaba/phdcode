# ADR-0031: Five tabs in the interface; uploads and saved searches change in it

**Status:** Accepted — 2026-10-22. Item 1 amended by [ADR-0033](0033-coding-and-stats-puzzles.md) (the sub-tabs of Skill development).

## Context

The interface had eleven top links, one for each page. The user wants fewer
tabs, the activity on the overview, the saved searches editable where they
are listed, and a way to give a PDF to a paper without a shell on the
server.

## Decision

1. **Five top tabs; a tab can have sub-tabs.** Overview; Papers (sub-tabs
   Papers, Interests, Labels, Digests, Bin); Ask; Skill development (sub-tabs
   Questions, Writing); How it works. The URLs of the pages do not change:
   `base.html` maps each path to its tab, and a link to an old page still
   works. The page Paper of the day is gone: its brief is on the overview,
   and its button still posts to `/paper-of-the-day/new`.
2. **The overview shows the activity:** the current and the longest streak of
   days with an activity, a calendar of 26 weeks, and the activities of each
   of the last 12 weeks by kind. An activity is an answered question (an
   attempt), an answered or revised writing task, or a paper marked as read.
   The streak and the calendar of the page Questions use the same function
   (`questions.days`). What runs and the configuration are in closed
   sections.
3. **The saved searches change on the page Interests:** the interest, the
   query and the threshold of an active search, a cancel, and a new search.
   The judged hits stay; a changed query runs from the last sweep on. The MCP
   tools for saved searches do not change.
4. **An uploaded PDF goes into the PDF queue, and the ingest starts.** The
   page Papers uploads several PDFs; the page of a paper uploads one for that
   paper. Each PDF is one request with the PDF as its body (no multipart
   parser), saved in `logs/uploads/`, and queued like `phdcode queue` does.
   The interface then starts the transient user unit `phdcode-ingest`:
   `phdcode ingest`, then `phdcode summarize`, which follow ADR-0017 for the
   GPU. A restart of the interface does not stop the unit.

## Consequences

- A PDF that is uploaded while `phdcode-ingest` runs waits for the next
  upload or for the daily sweep.
- The files in `logs/uploads/` stay after the ingest has copied them.
- The interface now writes `saved_searches` too, beside the tables of
  ADR-0019 to ADR-0027.

## Rejected

- **One page with client-side tabs:** each page keeps its own route and its
  own queries; a page that loads all of them is slower and breaks the links.
- **A multipart upload form:** it needs `python-multipart`; a fetch with the
  file as the body does the same with no new dependency.
- **The ingest inside the process of the interface:** a parse takes minutes
  and a restart of the interface would stop it.
