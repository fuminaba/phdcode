# Status

**Updated:** 2026-10-12 (after `4ae78de`)

<!--
CONVENTION -- read before editing.

This file holds only what the repository cannot tell you: what is half-done,
what is blocked on a human, what was tried and failed, and what is running
where. Anything a commit, an ADR, or the code already says belongs there, and
is linked from here instead.

Rules:
  - One file. Always this path. Always OVERWRITTEN, never appended.
  - No history. `git log docs/STATUS.md` is the history.
  - Update it when you stop work, not when you start.
  - A stale entry is worse than a missing one, because it reads as current.
    Delete anything you cannot confirm.

Not here: design rationale (docs/adr/), vocabulary (docs/GLOSSARY.md),
what changed and why (commit messages), the design (docs/adr/0015 to 0017).
-->

## Blocked on a human

- **The sweep timer is not installed.** The commands are in `llm_engine/systemd/phdcode-sweep.timer`. The service runs the sweep, then `phdcode summarize`.
- **The real saved searches** in the live database. Fumi plans to build an interest profile from a Zotero reference list first.
- **The labelled set** (about 100 papers that Fumi labels) selects the triage threshold, and compares Qwen3-Reranker-0.6B with 4B. Until then, the threshold of each saved search is a guess (default 0.5).
- **The old rows in Postgres.** The old tables hold 2 documents, 51 chunks, 24 conversations and 7 hooks. Fumi decides: export them, or drop the old tables. The new tables do not touch them.
- **The server needs a reboot** for the NVIDIA driver (the library is 595.99, the kernel module 595.91). Only NVML fails (`nvidia-smi`); CUDA works. Until the reboot, `gpu.free_vram_mib()` reads the free VRAM through a PyTorch child process (3 s instead of an instant `nvidia-smi`).

## In progress

Step 5 (ADR-0017, revised) is written and tested, not committed:
- **llama-server starts on demand:** the units `phdcode-llm-proxy.socket`, `phdcode-llm-proxy.service` and `phdcode-llm.service` are linked in `~/.config/systemd/user/`, and the socket is enabled. The first request to port 8080 started Bonsai 2 27B in 8 to 11 s; the proxy exits after 30 idle minutes, and llama-server stops with it.
- **`phdcode gpu clear` took 3 s** (llama-server and its socket stop, a request fails at once); `restore` starts the socket again.
- **`gpu.short_job()`:** tested with a second process that holds the lock: a job that the GPU saves 4 s used the CPU after 4.0 s; a job that it saves 20 s waited and used the GPU. While llama-server runs or the GPU is cleared, a short job uses the CPU at once.
- **Real jobs:** `phdcode summarize` started llama-server through the socket (paper 16, 94 s with the load); triage of 37 hits 18.8 s on the GPU in bf16 (0.008 from the CPU scores); the local-PDF ingest through MCP 44 s with the parse on the GPU.

Not done: the `balanced` mode (Surya, marker 2), deferred by ADR-0017 item 9. `embed()` sends all chunks in one request (600 s timeout); add batches before a book or a thesis goes in.

Earlier tests (steps 3 and 4) are in the commit messages. The test scripts are not in the repo (no `tests/` folder yet).

## Known traps

- **`pg_stat_user_tables.n_live_tup` showed 0 rows for tables that hold rows.** Count with `count(*)`.
- **The Docker Compose project is `alira2`**, from the old repository name; `docker-compose.yml` pins `name: alira2`. Without it, `docker compose up` makes a new, empty project `phdcode`, and port 5432 is already in use.
- **mcp 2.x is not the API of most examples.** `FastMCP` is `MCPServer` (`mcp.server.mcpserver`); client results use `structured_content` and `is_error`. Only a `ToolError` shows its message to the agent.
- **OpenAlex with no key has a budget of $0.10 a day: 100 searches.** A lookup by ID is free. A bad filter or a spent budget comes back as an HTTP 400 or 429 with the reason in the body; the client raises it as a `ValueError`. The filter `from_created_date` needs a paid plan. A key goes into `OPENALEX_API_KEY`.
- **The reranker downloads 1.2 GB from Hugging Face at its first load** (`TRIAGE_RERANKER`, into `~/.cache/huggingface`). With no network and no cache, triage uses the LLM fallback.
- **PubMed's `pdf_url` is the PMC article page, not a PDF.** `ingest_pdf` refuses it and asks for a local path.
- **`phdcode-mcp` uses the live database** (`DATABASE_URL` in `.env`). Its first start creates the new tables there, beside the old ones. A test sets `DATABASE_URL` to `phdcode_test` in the environment, which now wins over `.env`.
- **Headless dcode (`dcode -n`) rejects each MCP tool without `readOnlyHint`**, because it has no approval prompt. The read-only tools declare it; `ingest_pdf`, `save_paper` and the saved-search writes need the dcode TUI, which asks.
- **An output schema stops each dcode run at the first tool result** with `BlockingError` (dcode 0.1.73): the MCP client validates the result and imports `jsonschema` on the event loop of the LangGraph server. The tools send JSON text with no schema. `LANGGRAPH_ALLOW_BLOCKING` does not help, because dcode sets it. The traceback is in the server log that `DEEPAGENTS_CODE_DEBUG=1` keeps.
- **The overview page of the interface takes about 3 s** while NVML fails (the driver mismatch before the reboot): the free VRAM then comes from a PyTorch child process.
- **Do not nest `gpu.short_job()`.** The lock is not re-entrant: an inner job cannot take the lock that the outer one holds, so it waits, then runs on the CPU.
- **A systemd stop of llama-server sent SIGTERM to the router and its model process at once, and the router hung until the 90-s stop timeout.** `KillMode=mixed` sends it to the router only; a stop now takes about 3 s.
- **A start condition that skips llama-server does not fail the proxy that requires it**; a stale proxy then refused each request. The proxy uses `BindsTo=`, and `phdcode gpu clear` stops the socket.

## Tried and rejected

Recorded here only where the ADRs do not cover it:

- **dcode hung once for 7 minutes with zero model requests**, then did not reproduce. Diagnostic: `grep -c launch_slot_` in the llama-server log. No requests means client-side, not slow inference.
- **Low-bit quantization does not break exact-match editing.** Bonsai 2 27B at PQ2_0 passed 4/4 search-and-replace edits through dcode.

## Running where

- **`phdcode-llm-proxy.socket`** (systemd user unit, enabled) listens on port 8080. It starts llama-server with Bonsai 2 27B at the first request; llama-server holds 14.4 GB of VRAM and `/tmp/gpu.lock` until 30 minutes after the last request. Check: `systemctl --user is-active phdcode-llm-proxy.socket phdcode-llm.service`. Registered in the vault: `Resources/Agents/Monitoring.md`, `server-phdcode-llm`.
- **`phdcode-dashboard.service`** (systemd user unit, enabled): the read-only web interface at http://100.110.224.47:8620, on the Tailscale address only. It reads `.env` at start: restart it after a change. Registered in the vault: `Resources/Agents/Monitoring.md`, `server-phdcode-dashboard`.
- Postgres (`alira2-postgres-1`) runs in Docker, with the test database `phdcode_test`. Redis (`alira2-redis-1`) still runs, but nothing uses it.
- The MCP server is registered in Claude Code (`claude mcp add phdcode`) and in dcode (`~/.deepagents/.mcp.json`, user level, outside the repo); each session starts its own `phdcode-mcp`.
- The live database holds paper 1 (the nuclei local/global PDF, full text, 17 chunks) from the dcode RAG test on 2026-10-12.

Python environment is the conda env `alira`, with phdcode installed editable (`pip install --no-deps -e .`) and `mcp` 2.3.0. It has no pytest.
