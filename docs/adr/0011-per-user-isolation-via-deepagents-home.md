# ADR-0011: Per-user isolation via DEEPAGENTS_HOME

**Status:** Superseded by [ADR-0015](0015-rescope-to-literature-rag-tool.md) — 2026-10-10

## Context

The deployment target is a single user on one machine. Multi-user is wanted as a
possibility, not a priority.

dcode keeps threads, config and credentials in a data directory that defaults to
`~/.deepagents`, overridable via the `DEEPAGENTS_HOME` environment variable.
Without an override, every ALiRA2 user on one host would share one thread space
and one credential store.

ALiRA2's data model is already user-scoped, with ownership enforced in SQL —
`document_chunks` joins `documents` to check `user_id` even for a guessed id.

## Decision

Every run (ADR-0003) sets `DEEPAGENTS_HOME=<datadir>/users/<user_id>`. Keep
ALiRA2's existing user scoping. Build no auth, quotas, or session management.

## Consequences

- Multi-user later is a configuration change rather than a migration, for the
  cost of one environment variable per invocation.
- Threads, config and per-user model credentials isolate naturally, without a
  separate mechanism.
- A web frontend reachable over Tailscale is *not* multi-user. Anyone on the
  tailnet is trusted; that remains true until real auth exists.
- Per-user directories must be created on first use and are not garbage
  collected.

## Alternatives rejected

- **Ignore it, retrofit later.** The retrofit would be a migration of thread
  state, for something that costs one env var today.
- **Build real multi-user now** — auth, quotas, credentials per user. Building
  for users who do not exist.
