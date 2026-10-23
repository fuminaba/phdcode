# ADR-0030: The page Ask: an agent answers in the interface, with the local read-only tools only

**Status:** Accepted — 2026-10-22. Amends [ADR-0015](0015-rescope-to-literature-rag-tool.md), item 1.

## Context

ADR-0015 item 1 gives phdcode no harness of its own: any MCP client is the
agent. The user wants to ask the library a question in the interface, not
only in a terminal. A fixed pipeline (one search, one model call) was the
other option; the user chose an agent, which searches again when the hits
are weak.

A test on 2026-10-19 (dcode with Bonsai 2, 163 s, 13 tool calls) quoted 13
of 13 passages word for word.

dcode 0.1.73 in local mode builds its `read_file` tool with
`FilesystemBackend(virtual_mode=False)`: the agent can read any path, also
outside its working folder (`~/tokens.txt`, `.env`). `--allow-fs-tools`
must include `read_file`.

## Decision

1. **The interface starts a harness; phdcode still has no agent loop.** The
   page Ask (`llm_engine/ask.py`) runs `dcode -n` with Bonsai 2, or
   `claude -p` with a Claude route of `LLM_ROUTING`, in an empty temporary
   folder, with no shell, no interpreter and no settings of the user.
2. **The agent has the local read-only tools only:** `search_papers`,
   `search_chunks` and `get_chunks`. The run has its own MCP config, which
   starts `phdcode-mcp` with `PHDCODE_TOOLS`; the server then registers only
   those tools. No tool writes, and no tool calls an outside service
   (`search_sources` and `get_paper` look up a reference at the source). A
   secret that dcode's `read_file` reads can reach only the answer on the
   page, which the Tailscale address only serves.
3. **Each quote of the answer is checked** against the title, the abstract
   and the chunks of each paper that the answer cites (`[P<id>]`). The check
   ignores case, punctuation and Markdown links, and checks each part between
   two `...`. The page shows the count, and each quote that it did not find.
4. **One run at a time** (llama-server serves one request at a time). The
   progress lines come from `tool_calls` (ADR-0028). v1 has no history and no
   follow-up question.

## Consequences

- The user-level MCP config of dcode and of Claude Code keeps all tools; only
  the runs of the page have fewer.
- The progress lines show each call of the three tools since the start of the
  run, also a call of another agent session at the same time.
- The last answer stays in the process of the interface: a restart loses it.

## Rejected

- **A fixed pipeline** (one search and one model call): it cannot search
  again or read the text around a passage.
- **A sandbox for dcode** (bubblewrap, a remote sandbox): the MCP server in
  the same process tree needs `.env`, so the sandbox must still show the
  secret; with no outside tool, the gain is small for one user.
- **A history of runs in a table:** not needed for v1.
