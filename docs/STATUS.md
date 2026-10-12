# Status

**Updated:** 2026-10-11 (after `a1ceb1f`; step 4 is not committed)

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

- **Fumi commits step 4** (OpenAlex, triage, the sweep). The message is in the vault: `Projects/phdcode/agents/commits/5-code-step4-sweep.md`.
- **The labelled set** (about 100 papers that Fumi labels) selects the triage threshold, and compares Qwen3-Reranker-0.6B with 4B. Until then, the threshold of each saved search is a guess (default 0.5).
- **The sweep timer is not installed.** The commands are in `llm_engine/systemd/phdcode-sweep.timer`. Install it after the triage test, and set `DIGEST_DIR` in `.env` first (the default is `logs/digests` in the repo).
- **The old rows in Postgres.** The old tables hold 2 documents, 51 chunks, 24 conversations and 7 hooks. Fumi decides: export them, or drop the old tables. The new tables do not touch them.
- **`.env` has no `AGENT_BACKEND` and no `SUMMARY_MODEL`.** Until one is set, an ingest stores the chunks and reports the summaries as pending, and each hit of a sweep waits for triage.
- **The server needs a reboot** for the NVIDIA driver (the library is 595.99, the kernel module 595.91). Only NVML fails (`nvidia-smi`); CUDA works (verified 2026-10-11). Read the free VRAM with `torch.cuda.mem_get_info()` until the reboot.
- **The VRAM test of the capped Surya vLLM server.** ADR-0017 stays Proposed until it runs. Ask Fumi before you start it: training uses the GPU. The procedure is in the vault: `Projects/phdcode/agents/HANDOFF.md`, "Test the VRAM".

## In progress

Step 4 (OpenAlex, `decide()`, `phdcode sweep`, the timer units) is written. Tested against `phdcode_test`:
- the three sources with the 7-day window, live; the OpenAlex lookup, a missing work, and a bad filter;
- **a sweep of three saved searches with no model set: 37 hits, all wait for triage, and the digest says why;**
- triage of the same 37 hits: Qwen 3.5 9B (Ollama) median 19.6 s for each hit, 4 invalid replies (before the JSON Schema fix); Bonsai 2 27B (llama-server, 14.4 GB of VRAM) median 3.4 s, 0 invalid; **Qwen3-Reranker-0.6B 16 ms on the GPU (batch 8), 3.1 s on the CPU, the same order as Bonsai.** Results in `logs/triage-test/` (not in git);
- **two full sweeps with the reranker on the GPU: 19.6 s (37 hits triaged, 4 papers saved), then 3.6 s (0 new hits);**
- the MCP test of step 3 again: all nine tools pass.

The marker parse of step 3 took 3 min 18 s for 5 pages on 8 CPU threads, with a peak of 9.2 GB RAM. The test scripts are not in the repo (no `tests/` folder yet).

Not done from the step-2 review: `embed()` sends all chunks in one request (600 s timeout); add batches before a book or a thesis goes in.

**Next step.**

5. Rewrite `gpu.py` as the tenant lock `/tmp/gpu.lock` with the admission check, `phdcode gpu clear` / `restore`, the drain, and marker 2 for the balanced mode (after the VRAM test). Nothing calls `gpu.py` now.

## Known traps

- **`pg_stat_user_tables.n_live_tup` showed 0 rows for tables that hold rows.** Count with `count(*)`.
- **The Docker Compose project is `alira2`**, from the old repository name; `docker-compose.yml` pins `name: alira2`. Without it, `docker compose up` makes a new, empty project `phdcode`, and port 5432 is already in use.
- **mcp 2.x is not the API of most examples.** `FastMCP` is `MCPServer` (`mcp.server.mcpserver`); client results use `structured_content` and `is_error`. Only a `ToolError` shows its message to the agent.
- **OpenAlex with no key has a budget of $0.10 a day: 100 searches.** A lookup by ID is free. A bad filter or a spent budget comes back as an HTTP 400 or 429 with the reason in the body; the client raises it as a `ValueError`. The filter `from_created_date` needs a paid plan. A key goes into `OPENALEX_API_KEY`.
- **The reranker downloads 1.2 GB from Hugging Face at its first load** (`TRIAGE_RERANKER`, into `~/.cache/huggingface`). With no network and no cache, triage uses the LLM fallback.
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
