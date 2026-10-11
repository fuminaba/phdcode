# ADR-0001: Adopt dcode as the agent harness

**Status:** Superseded by [ADR-0015](0015-rescope-to-literature-rag-tool.md) — 2026-10-10

## Context

ALiRA2 has its own agent loop (`tgi.py`, 1,082 lines) with a self-extending step
budget, progressive tool revelation, and a tool-result digest pass. Separately,
`dcode` (deepagents-code 0.1.72, on the deepagents SDK 0.7.15) was found already
installed on the machine and verified working against the local llama-server
backend: 4/4 exact-match edit tasks passed on Bonsai 2 27B in 11–21s each.

The goal is an agent that can drive a filesystem and a shell — write a script
that batches pathology patches through MedGemma, run a recurring literature
sweep — while reaching ALiRA2's domain capabilities.

dcode already ships every layer that would otherwise have to be built: shell
allowlist, filesystem tool allowlist, sandboxes, subagents, context compaction,
managed ripgrep, a classifier-backed approval mode, skills, MCP, hooks,
extensions, headless and ACP modes, and rubric grading.

## Decision

dcode is the agent. ALiRA2 stops being an agent and becomes the capabilities,
the scheduler, the adapters, and the knowledge store around it.

## Consequences

- The agent loop is no longer ours to maintain or tune (see ADR-0002).
- We inherit dcode's release cadence. Its version is pinned and upgrades are
  tested rather than taken blindly, because the extension trust path is labelled
  experimental (see ADR-0005).
- Bonsai 2 27B drives it. MedGemma cannot: its `chat_template.jinja` handles only
  system/user/assistant and contains no tool-calling construct at all, so it can
  never drive a loop (see ADR-0006 for where it goes instead).
- Anything dcode does not do — scheduling above all — stays ours (ADR-0004).

## Alternatives rejected

- **Build our own deepagents app.** Same building blocks, full control, but it
  re-acquires the agent loop we are trying to stop maintaining, against a target
  maintained by the framework's own authors.
- **Run both** — our loop for chat, dcode for tasks. Rejected explicitly: two
  agents is the conceptual sprawl that prompted this session.
- **Aider / OpenHands / other coding agents.** Better at coding today, but the
  novel thing here is the *combination* — one agent that reads the user's
  papers, remembers their conversations, and processes their images. If coding
  alone were the goal, installing one of those would be the correct answer and
  none of this would be needed.
