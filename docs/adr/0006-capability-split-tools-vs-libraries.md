# ADR-0006: Capability split — tools vs libraries

**Status:** Accepted — 2026-09-22

## Context

This session's stated problem was conceptual: no rule for where a new capability
belongs. The two motivating use cases pull in opposite directions.

"Add a recurring keyword search and collect papers" is a *tool call*: one
invocation, returns quickly, no GPU.

"Look at these pathology images and note each one" is not. The agent should
write a script that queues patches through MedGemma and logs findings — because
per-image tool calls would reload an 8.2GB model repeatedly, whereas one script
loads once and processes N patches.

A rule is needed that answers "where does this go" without a case-by-case debate.

## Decision

> A capability is a **tool** if the agent should discover and invoke it by name.
> It is a **library/CLI** if the agent may need to compose or customize it.
> Most are both — a thin tool wrapping the library. Anything touching the GPU
> takes the lock inside the library, never in the wrapper; a tool wrapping a GPU
> library is long-running by contract and says so in its description.

The practical test is one question: **can this block someone else's turn?**

By this rule: literature search, document search, SQL, calendar, Gmail,
reminders are tools. MedGemma batch analysis, marker-pdf extraction, and
embedding backfills are libraries — each with a thin discoverable tool wrapper
and a CLI, because `execute` is how the agent reaches for them first.

## Consequences

- Discoverability is the default. A capability that exists only as a library the
  agent has never heard of is not a capability.
- The GPU lock lives in exactly one layer, which keeps ADR-0007 enforceable.
- Tool descriptions carry the cost signal, so the agent can act on it rather
  than discovering duration by experiencing it.
- `search_documents` is a tool, but the ingestion that populates it is a library.
  That split already existed in the code; this names it.

## Alternatives rejected

- **Everything as tools.** Forces per-item calls for batch work, which for
  MedGemma means reloading 8.2GB per image.
- **Everything as libraries.** Nothing is discoverable; the agent must be told
  what exists in the system prompt, which does not scale.
- **A simpler rule — "GPU or not".** Nearly right, and it is the enforceable
  core, but it leaves no room for the thin wrapper that makes GPU work findable.
