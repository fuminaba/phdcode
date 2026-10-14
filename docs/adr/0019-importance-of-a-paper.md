# ADR-0019: The user sets the importance of a paper in the interface

**Status:** Accepted — 2026-10-14. The interface also writes the daily question since [ADR-0020](0020-daily-question.md).

## Context

The sweep adds papers each day, so the list of papers grows faster than the
user reads it. The user needs to mark, after reading a paper, how much a PhD
student in this field must know it. A model cannot set this: the user's
labels are the measure (ADR-0018).

The interface was read-only. Its premise: everything that changes the data
goes through the MCP tools or the `phdcode` command. A click on the paper
page is faster than a tool call for a value that the user sets by hand.

## Decision

1. **A new column `papers.importance`**: `interesting`, `recommended` or
   `crucial`, weakest first; NULL means not set. A CHECK constraint keeps
   other values out.
2. **The interface writes this one column**: `POST
   /papers/{id}/importance?level=…` (an empty level sets NULL). The paper
   page has three buttons, one for each level (a click on the set level
   clears it); the list of papers shows the value and filters by it.
3. **No default value.** NULL separates "not read" from "read, interesting".
4. `get_paper` returns the value, so the agent can read it.

## Consequences

- The interface has no login (it listens on the Tailscale address only), so each
  device on the tailnet can change the importance. It can change nothing
  else.
- Not done: search and triage do not use the importance yet. Each is a small
  change when the user wants it (for example, a filter in `search_papers`).
