# ADR-0002: Delete the in-house agent loop

**Status:** Superseded by [ADR-0015](0015-rescope-to-literature-rag-tool.md) — 2026-10-10

## Context

ADR-0001 makes dcode the agent. That leaves `tgi.py` (the `GenerativeService`
tool loop), `pipeline.py`'s agent path, and `tasks/chat_tasks.py`'s `call_model`
and `count_prompt_tokens` with no caller.

Three of the 27 registered tools exist only to serve that loop:
`continue_research` and `discover_tools` are intercepted by `_run_tool_loop`
before dispatch, and `recall_tool_results` caches for it.

## Decision

Delete `tgi.py`, the agent path in `pipeline.py`, and the three loop-artifact
tools. Do not keep a dormant copy.

## Consequences

- One agent, one loop. This is the concrete form of the conceptual clarity this
  session set out to recover.
- Real capability is lost: the tool-result digest pass was tuned against an
  observed failure (a model reading a system-inserted digest as prompt
  injection), and the self-extending step budget has no dcode equivalent —
  dcode has `--recursion-limit`, which is a ceiling, not a grant. If these turn
  out to matter, they return as extension middleware, not as a second loop.
- `discover_tools` dying is a simplification rather than a loss: it existed to
  manage *our* schema-token budget, which is dcode's concern now.
- The code remains in git history. That is what git history is for.

## Alternatives rejected

- **Keep it for a "light chat" path.** Reintroduces two brains, with a rule
  about which one answers — the ambiguity this session exists to remove.
- **Keep it dormant, in case.** Every future change would have to consider a
  second code path nobody runs; dormant code is not free, it is deferred cost.
