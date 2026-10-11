# Status

**Updated:** 2026-10-11 (after `4fa20d7`; the docs, step 1 and step 2 are not committed)

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

- **Fumi commits three diffs:** the docs, step 1 (deletions), step 2 (schema). The git index holds the docs and step 1; step 2 is unstaged. The commands are in the vault: `Projects/phdcode/agents/commits/`.
- **The old rows in Postgres.** The old tables hold 2 documents, 51 chunks, 24 conversations and 7 hooks. Fumi decides: export them, or drop the old tables. The new tables do not touch them.
- **`.env` has no `AGENT_BACKEND` and no `SUMMARY_MODEL`.** Until one is set, `generate()` raises, and an ingest stores the chunks but no summaries.
- **The server needs a reboot** for the NVIDIA driver (the library is 595.99, the kernel module 595.91). Until then, nothing can use the GPU.
- **The VRAM test of the capped Surya vLLM server.** ADR-0017 stays Proposed until it runs. Ask Fumi before you start it: training uses the GPU. The procedure is in the vault: `Projects/phdcode/agents/HANDOFF.md`, "Test the VRAM".

## In progress

Step 2 is written, with the fixes from a code review (2026-10-11). A smoke test passed against the database `phdcode_test` (same container): the schema twice, the upsert, the keywords, local PDFs, both searches and every facet, the upgrade, and the saved searches. The live database has no new tables yet. `python -m llm_engine.memory.document_index` checks the page tracking of the chunker. The ingest (marker) is not tested: the GPU needs the reboot. Not done from the review: `embed()` sends all chunks in one request (600 s timeout); add batches before a book or a thesis goes in.

**Next steps, in this order.** Each step is one reviewable diff.

3. The MCP server (ADR-0016). It replaces `llm_engine/tooling/registry.py`, `llm_engine/context.py` and the `@tool` functions in `literature_tools.py`. `ingest_pdf` calls `memory.ingest_full_text`.
4. OpenAlex, `decide()`, the sweep CLI and its timer.
5. Rewrite `gpu.py` as the tenant lock `/tmp/gpu.lock` with the admission check, `phdcode gpu clear` / `restore`, the drain, and marker 2 (after the VRAM test). Nothing calls `gpu.py` now.

## Known traps

- **`routes.py` runs `load_dotenv(override=True)`.** Any import of it replaces a `DATABASE_URL` that you set in the shell with the one in `.env`, which is the live database. A test sets `db.DATABASE_URL` again after its imports.
- **`pg_stat_user_tables.n_live_tup` showed 0 rows for tables that hold rows.** Count with `count(*)`.
- **The Docker Compose project is `alira2`**, from the old repository name; `docker-compose.yml` pins `name: alira2`. Without it, `docker compose up` makes a new, empty project `phdcode`, and port 5432 is already in use.
- **The ingest still runs marker 1.10.2 in this process, with its OCR models on the GPU.** This breaks ADR-0017 until step 5. Ingest only when the GPU is free.
- **The GPU lock in `gpu.py` is re-entrant, and must stay that way.** `flock` associates a lock with the open file description, so a nested acquisition opens a second fd and self-deadlocks with no other process involved. A per-thread depth counter handles it.
- **`pdftext_workers=1` exists because of Celery's prefork pool.** Verify ingestion after you remove it.

## Tried and rejected

Recorded here only where the ADRs do not cover it:

- **dcode hung once for 7 minutes with zero model requests**, then did not reproduce. Diagnostic: `grep -c launch_slot_` in the llama-server log. No requests means client-side, not slow inference.
- **Low-bit quantization does not break exact-match editing.** Bonsai 2 27B at PQ2_0 passed 4/4 search-and-replace edits through dcode.

## Running where

Nothing from phdcode runs. Postgres (`alira2-postgres-1`) runs in Docker. Redis (`alira2-redis-1`) still runs, but nothing uses it.

Python environment is the conda env `alira`. It does not have the `mcp` SDK or pytest.
