# Status

**Updated:** 2026-10-11 (after `ed0834a`; step 3 is not committed)

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

- **Fumi commits step 3** (the MCP server). The message is in the vault: `Projects/phdcode/agents/commits/4-code-step3-mcp-server.md`.
- **The old rows in Postgres.** The old tables hold 2 documents, 51 chunks, 24 conversations and 7 hooks. Fumi decides: export them, or drop the old tables. The new tables do not touch them.
- **`.env` has no `AGENT_BACKEND` and no `SUMMARY_MODEL`.** Until one is set, an ingest stores the chunks and reports the summaries as pending.
- **The server needs a reboot** for the NVIDIA driver (the library is 595.99, the kernel module 595.91). The CPU parse does not need it.
- **The VRAM test of the capped Surya vLLM server.** ADR-0017 stays Proposed until it runs. Ask Fumi before you start it: training uses the GPU. The procedure is in the vault: `Projects/phdcode/agents/HANDOFF.md`, "Test the VRAM".

## In progress

Step 3 (the MCP server) is done. An end-to-end test over stdio passed against `phdcode_test`: all nine tools, live against arXiv and PubMed, and the error messages. **`ingest_pdf` of `arxiv:2112.03998` (5 pages) took 3 min 18 s on 8 CPU threads, with a peak of 9.2 GB RAM in the marker subprocess.** It stored 17 chunks with pages 1 to 5, and reported the summaries as pending. The test scripts are not in the repo (no `tests/` folder yet).

Not done from the step-2 review: `embed()` sends all chunks in one request (600 s timeout); add batches before a book or a thesis goes in.

**Next steps, in this order.** Each step is one reviewable diff.

4. OpenAlex, `decide()`, the sweep CLI and its timer.
5. Rewrite `gpu.py` as the tenant lock `/tmp/gpu.lock` with the admission check, `phdcode gpu clear` / `restore`, the drain, and marker 2 for the balanced mode (after the VRAM test). Nothing calls `gpu.py` now.

## Known traps

- **`pg_stat_user_tables.n_live_tup` showed 0 rows for tables that hold rows.** Count with `count(*)`.
- **The Docker Compose project is `alira2`**, from the old repository name; `docker-compose.yml` pins `name: alira2`. Without it, `docker compose up` makes a new, empty project `phdcode`, and port 5432 is already in use.
- **mcp 2.x is not the API of most examples.** `FastMCP` is `MCPServer` (`mcp.server.mcpserver`); client results use `structured_content` and `is_error`. Only a `ToolError` shows its message to the agent.
- **PubMed's `pdf_url` is the PMC article page, not a PDF.** `ingest_pdf` refuses it and asks for a local path.
- **`phdcode-mcp` uses the live database** (`DATABASE_URL` in `.env`). Its first start creates the new tables there, beside the old ones. A test sets `DATABASE_URL` to `phdcode_test` in the environment, which now wins over `.env`.
- **The GPU lock in `gpu.py` is re-entrant, and must stay that way.** `flock` associates a lock with the open file description, so a nested acquisition opens a second fd and self-deadlocks with no other process involved. A per-thread depth counter handles it.

## Tried and rejected

Recorded here only where the ADRs do not cover it:

- **dcode hung once for 7 minutes with zero model requests**, then did not reproduce. Diagnostic: `grep -c launch_slot_` in the llama-server log. No requests means client-side, not slow inference.
- **Low-bit quantization does not break exact-match editing.** Bonsai 2 27B at PQ2_0 passed 4/4 search-and-replace edits through dcode.

## Running where

Nothing from phdcode runs. Postgres (`alira2-postgres-1`) runs in Docker, with the test database `phdcode_test`. Redis (`alira2-redis-1`) still runs, but nothing uses it.

Python environment is the conda env `alira`, with phdcode installed editable (`pip install --no-deps -e .`) and `mcp` 2.3.0. It has no pytest.
