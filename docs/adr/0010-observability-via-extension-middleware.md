# ADR-0010: Observability via extension middleware

**Status:** Superseded by [ADR-0015](0015-rescope-to-literature-rag-tool.md) — 2026-10-10

## Context

dcode is otherwise a black box: it prints tool names to a terminal and keeps
threads on disk. ALiRA2 has `live_viewer.py`, which renders `turn_actions` rows
written per action, and is the existing way to watch an agent work.

dcode offers two observation points. Shell hooks (`SessionStart`, `PreToolUse`,
`PostToolUse`, `Stop`) receive a JSON payload that includes `tool_name` and
`tool_output`, with `command`-type handlers. Extensions offer
`register_middleware`, which runs in-process with real objects.

ADR-0005 chose extensions, which makes middleware available.

## Decision

Three middlewares, in-process:

1. **Tool-call mirror** — write every tool call and result into `turn_actions`,
   so `live_viewer.py` keeps working unchanged against dcode.
2. **Web-search cache** — capture dcode's built-in Tavily `web_search` results
   into `tool_result_cache`. This is why ALiRA2's own `web_search` need not be
   registered: the built-in one is hooked instead.
3. **Thread summary on stop** — write summary, topics and embedding to Postgres
   (the ADR-0009 bridge).

## Consequences

- `live_viewer.py` survives the migration and becomes the observability surface
  for dcode, which is otherwise unobservable.
- Middleware sees objects rather than re-serialized JSON, so the mirror does not
  parse what the process already holds.
- Observability is coupled to the extension API. If it breaks on upgrade, the
  shell-hook path remains available as a fallback — but is deliberately not
  maintained in parallel.
- The GPU lock is explicitly *not* middleware on `execute`: locking every shell
  call would serialize `ls` behind a model load. It stays in the GPU libraries
  (ADR-0006, ADR-0007).

## Alternatives rejected

- **Shell hooks for all three.** Re-serialize objects already in memory, and
  hedge the extension dependency that ADR-0005 deliberately accepted.
- **Only the Stop summary.** Minimum to preserve recall, but leaves every tool
  call invisible — losing the thing `live_viewer.py` was built for.
- **LangSmith tracing.** Captures reasoning and tool calls well, but renders in
  someone else's UI and does not feed the existing viewer.
