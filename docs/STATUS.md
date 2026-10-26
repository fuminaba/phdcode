# Status

**Updated:** 2026-10-24 (after `928ec4d`, with ADR-0032 not committed)

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

- **Fumi drops the 9 tables of the old general assistant** (`conversations`, `conversation_turns`, `turn_actions`, `documents`, `document_chunks`, `document_keywords`, `hooks`, `oauth_credentials`, `tool_result_cache`). No code uses them. The dump is `logs/backup/alira-old-tables-2026-10-22.sql.gz` (all 9 tables). The auto mode of Claude Code refuses a `DROP TABLE`.
- **Fumi deletes the 181 old CSV files** in `logs/papers/*/tables/` (the parse stopped writing them in `30bde75`; nothing reads them): `find logs/papers -path '*/tables/*.csv' -delete && find logs/papers -type d -name tables -empty -delete`.
- **2 PDFs of the import still fail** (ASCO answers 403): paper 30 (DNA methylation signatures, JCO 2012) and paper 43 (active surveillance cohort, JCO 2014). Fumi downloads them; then `phdcode queue FILES`.
- **11 Zotero PDFs are on the Mac** (`/Users/fuminaba/Zotero/storage`). With a copy on the server, `phdcode queue FILES` puts each into the queue of its paper.
- **BM25 (ADR-0029) is not measured on quality.** The test: Fumi labels the top 10 of the old and the new search for about 20 logged queries (`tool_calls`), and nDCG@10 compares them.
- **The page Ask (ADR-0030) is tested with Claude Haiku 5.5 only** (14 s, 8 of 8 quotes found, twice). The dcode run with Bonsai 2 is not tested on the page: on 2026-10-22 another GPU process held 5.9 GB, and llama-server needs 14.4 GB. Test it when the GPU is free.
- **Fumi labels the hits on the page Labels** (ADR-0027). 81 hits have labels (the week-1 import). The threshold 0.3 rests on them (ADR-0018).

## In progress

- **The sweep timer works:** the sweeps of 2026-10-23 and 2026-10-24 at 06:00 PDT finished (`journalctl --user -u phdcode-sweep.service`).
- **The briefs (ADR-0032)** are tested on `phdcode_test` with a fake model, and live with Claude Haiku 5.5 (`BRIEF_MODEL`, the default): the interests and triage briefs, 10 to 11 s each. No digest summary is written yet: the first comes from the sweep of 2026-10-25 06:00 PDT, through `claude -p` in the sweep unit (not tested from that unit; the page Ask runs it from the interface unit).
- The page Triage holds the 98 papers that the sweep saved since 2026-10-13; the page Papers holds the 104 of the user's library.
- The library: 117 papers, 31 with full text (2,171 chunks) before that ingest.
- **The puzzles (ADR-0033)** are tested on `phdcode_test` with a fake model (the check of the tests, the run, the score, both pages) and live with Claude Haiku 5.5: one coding puzzle (7 tests, passing on its reference, 50 s) and one stats puzzle (36 s), both from 2026-10-24 and not answered. Not tested: a score from a real model saved through the page. The review topics (brush up on) are tested on `phdcode_test` with a fake model, and with one Haiku grade that was not saved (3 exact MIL concepts for a wrong answer).
- The paper of the day (ADR-0024, now on the overview) and the writing coach (ADR-0025) are tested with a fake model only, not yet with a real model.
- **The upload of a PDF (ADR-0031) is tested up to the queue only** (`phdcode_test`, the ingest unit replaced by a stub). The first real upload starts `phdcode-ingest`, which also parses the other PDFs in the queue. Check it with `journalctl --user -u phdcode-ingest`.

Not done (Fumi's plans, 2026-10-14): textbooks as sources (for axes such as ML foundations): `embed()` needs batches first, a book has no methods or results summary, and its questions need chapters, not the abstract. A thesis section: the thesis in LaTeX on Overleaf, synced to GitHub; phdcode would read the `.tex` files of a clone (no PDF parse) and chunk them by section. The user's own papers as a separate kind.

Not done: tracking of authors, research groups and labs (Fumi's wish, 2026-10-12). Each paper stores all of its authors; no saved search follows an author yet. Groups and labs need a web search (Tavily, an API key) on top of the per-author searches.

Not done: the `balanced` mode (Surya, marker 2), deferred by ADR-0017 item 9. `embed()` sends all chunks in one request (600 s timeout); add batches before a book or a thesis goes in.

Not done: the systemd install steps are written in 4 places (the comments of the units and the docs).

The test scripts are not in the repo (no `tests/` folder yet).

## Known traps

- **`pg_stat_user_tables.n_live_tup` showed 0 rows for tables that hold rows.** Count with `count(*)`.
- **The Docker Compose project is `alira2`**, from the old repository name; `docker-compose.yml` pins `name: alira2`. Without it, `docker compose up` makes a new, empty project `phdcode`, and port 5432 is already in use.
- **mcp 2.x is not the API of most examples.** `FastMCP` is `MCPServer` (`mcp.server.mcpserver`); client results use `structured_content` and `is_error`. Only a `ToolError` shows its message to the agent.
- **OpenAlex with no key has a budget of $0.10 a day: 100 searches.** A lookup by ID is free. A bad filter or a spent budget comes back as an HTTP 400 or 429 with the reason in the body; the client raises it as a `ValueError`. The filter `from_created_date` needs a paid plan. A key goes into `OPENALEX_API_KEY`.
- **Triage needs llama-server** (ADR-0018): during training (`phdcode gpu clear`) the hits wait, and the next sweep looks back to the last complete sweep of each search. Another GPU process with too little VRAM left also makes them wait.
- **PubMed's `pdf_url` is the PMC article page, not a PDF.** `ingest_pdf` refuses it and asks for a local path.
- **`phdcode-mcp` uses the live database** (`DATABASE_URL` in `.env`). A test sets `DATABASE_URL` to `phdcode_test` in the environment, which wins over `.env`.
- **A running `phdcode-mcp` keeps the code of its start.** Each agent session starts its own; a change to the search reaches only new sessions.
- **The keyword side ignores the websearch syntax** (`"phrase"`, `-word`) since ADR-0029: BM25 ranks a bag of words.
- **Headless dcode (`dcode -n`) rejects each MCP tool without `readOnlyHint`**, because it has no approval prompt. The read-only tools declare it; `ingest_pdf`, `save_paper` and the saved-search writes need the dcode TUI, which asks.
- **An output schema stops each dcode run at the first tool result** with `BlockingError` (dcode 0.1.73): the MCP client validates the result and imports `jsonschema` on the event loop of the LangGraph server. The tools send JSON text with no schema. `LANGGRAPH_ALLOW_BLOCKING` does not help, because dcode sets it. The traceback is in the server log that `DEEPAGENTS_CODE_DEBUG=1` keeps.
- **Ollama is outside the GPU rules.** A model that Ollama loads (for example gpt-oss for a test) holds VRAM that `phdcode gpu check` and the short jobs then miss; with Bonsai loaded, a second model does not fit. Unload it after a test: `ollama stop <model>`.
- **The test database and the live database share `logs/papers/`**, and their paper IDs overlap. A test must also set `LOG_DIR` to another folder. The old test folders are in `logs/test-papers/`. An ingest empties the folder of its paper first. The bin (ADR-0026) deletes `logs/papers/<id>` for good: a TestClient test of the interface sets `dashboard.PAPERS_DIR` to a temporary folder.
- **Do not nest `gpu.short_job()`.** The lock is not re-entrant: an inner job cannot take the lock that the outer one holds, so it waits, then runs on the CPU.
- **A systemd stop of llama-server sent SIGTERM to the router and its model process at once, and the router hung until the 90-s stop timeout.** `KillMode=mixed` sends it to the router only; a stop now takes about 3 s.
- **A start condition that skips llama-server does not fail the proxy that requires it**; a stale proxy then refused each request. The proxy uses `BindsTo=`, and `phdcode gpu clear` stops the socket.
- **The arXiv API refuses after many queries in a short time** (HTTP 429, and read timeouts of 15 s). A test script must cache its hits and wait between arXiv requests. The sweep does not mark a failed search as swept, so the next sweep looks back far enough.
- **`/tmp` on fuminaba-server is a 16 GB RAM disk (tmpfs).** Put large downloads and environments under the home folder.

## Tried and rejected

Recorded here only where the ADRs do not cover it:

- **dcode hung once for 7 minutes with zero model requests**, then did not reproduce. Diagnostic: `grep -c launch_slot_` in the llama-server log. No requests means client-side, not slow inference.
- **Low-bit quantization does not break exact-match editing.** Bonsai 2 27B at PQ2_0 passed 4/4 search-and-replace edits through dcode.

## Running where

- **`phdcode-llm-proxy.socket`** (systemd user unit, enabled) listens on port 8080 of the Tailscale address and of `127.0.0.1` only (the local drop-in `~/.config/systemd/user/phdcode-llm-proxy.socket.d/override.conf`, 2026-10-22, with `FreeBind=true`). It starts llama-server with Bonsai 2 27B at the first request; llama-server holds 14.4 GB of VRAM and `/tmp/gpu.lock` until 30 minutes after the last request. Check: `systemctl --user is-active phdcode-llm-proxy.socket phdcode-llm.service`. Registered in the vault: `Resources/Agents/Monitoring.md`, `server-phdcode-llm`.
- **`phdcode-sweep.timer`** (systemd user unit, enabled 2026-10-22): `phdcode-sweep.service` each day at 06:00, `Persistent=true`. Registered in the vault: `Resources/Agents/Monitoring.md`, `server-phdcode-sweep`.
- **`phdcode-dashboard.service`** (systemd user unit, enabled): the web interface on port 8620 of the Tailscale address only (it writes `papers.importance`, `papers.notes`, `papers.read_at`, `papers.deleted_at`, the tables `questions`, `settings` and `saved_searches`, and the PDF queue; uploads go to `logs/uploads/`, and it starts the transient unit `phdcode-ingest` (`phdcode ingest`, then `phdcode summarize`); it deletes the papers of the bin for good after the bin days, with their folders). It reads `.env` at start: restart it after a change. Registered in the vault: `Resources/Agents/Monitoring.md`, `server-phdcode-dashboard`.
- Postgres (`alira2-postgres-1`) runs in Docker, with the test database `phdcode_test`. Redis (`alira2-redis-1`) still runs, but nothing uses it.
- The MCP server is registered in Claude Code (`claude mcp add phdcode`) and in dcode (`~/.deepagents/.mcp.json`, user level, outside the repo); each session starts its own `phdcode-mcp`.
- The repo `fuminaba/phdcode` is public, and the docs are at `https://phdcode.fuminaba.com` (the Cloudflare Worker `phdcode-docs`).

Python environment is the conda env `alira`, with phdcode installed editable (`pip install --no-deps -e .`) and `mcp` 2.3.0. It has no pytest and no polars.
