# ADR-0004: ALiRA2's hooks remain the scheduler

**Status:** Accepted — 2026-09-22

## Context

The motivating use case — "add a recurring search on these keywords, collect
papers, keep me up to date" — requires something that fires without the user
present. dcode has no scheduler; it runs when invoked.

ALiRA2 already has one: a `hooks` table, `hooks/registry.py`, and a ticker in
`webhook_server.py` that fires due hooks on an interval. It is database-backed
and in use for reminders and Gmail polling.

## Decision

ALiRA2's hooks remain the scheduler. A due hook creates a run (ADR-0003).

## Consequences

- This is the one distinctive thing built here that dcode has no answer for, so
  it is also the clearest "keep, don't rebuild" in the design.
- The ticker's host process must stay alive for scheduled work to happen. It
  currently lives in `webhook_server.py`; as interfaces multiply (ADR-0012) the
  ticker should move somewhere that does not depend on Telegram being up.
- Scheduling semantics stay ours to extend (intervals, daily resets, backoff)
  without waiting on dcode.

## Alternatives rejected

- **System cron / systemd timers.** Moves scheduling out of the database and out
  of the product: the agent could no longer create or cancel its own schedules
  in response to a request, which is precisely the motivating use case.
- **Build a scheduler into the new harness.** Rebuilding a tested, database-
  backed component for no gain.
