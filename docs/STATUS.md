# Status

**Updated:** 2026-09-22

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
what changed and why (commit messages), the plan (docs/ROADMAP.md).
-->

## Blocked on a human

- **`git push origin main`** — 6 commits exist on `fuminaba-server` only. `origin` holds the initial commit alone. Nothing else is backed up anywhere.
- **GitHub rename** `ALiRA2` → `phdcode`, then `git remote set-url origin`. Rename, do not delete and recreate. See [ADR-0014](adr/0014-new-repository-phdcode.md).
- `gh` CLI is not installed, so the rename needs the web UI.

## In progress

Nothing is part-written. The design phase is complete: [14 ADRs](adr/README.md) and a [glossary](GLOSSARY.md). The build has not started.

Next task is the prerequisite below, then the build order in [ROADMAP](ROADMAP.md).

## Known traps

- **`embeddings.embed()` blocks the deletions.** It reaches `GenerativeService.get_client` through `tasks/chat_tasks.embed_text`, so deleting `tgi.py` breaks embeddings, which the knowledge store needs. Rehome it to a standalone OpenAI-compatible client first. This is why `openai` survives in `pyproject.toml`; ADR-0002 does not mention it.
- **`pyproject.toml` describes the post-deletion state.** A fresh `uv sync` today removes celery and redis and breaks `llm_engine/tasks/`. Fine on the existing env.
- **`pdftext_workers=1` exists because of Celery's prefork pool.** Removing it with Celery is correct in theory. Verify ingestion afterwards; that workaround came from a real failure.
- **`alira-models.ini` understates VRAM by ~900 MiB.** Its figures were taken in single-model mode and omit the router parent process. Bonsai 2 27B measures 14,424 MiB of 16,303. The "131072 also fits" note is optimistic by the same margin and is unverified under router mode.

## Tried and rejected

Recorded here only where the ADRs do not cover it:

- **dcode hung once for 7 minutes with zero model requests**, then did not reproduce across four runs. Suspected ripgrep install lock; "Installing ripgrep..." prints on every invocation, which itself looks wrong. Diagnostic: `grep -c launch_slot_` in the llama-server log. No requests means client-side, not slow inference.
- **Low-bit quantization does not break exact-match editing.** Predicted it would; Bonsai 2 27B at PQ2_0 passed 4/4 search-and-replace edits through dcode in 11-21s. Do not redesign around this without new evidence.

## Running where

Nothing is running. llama-server is stopped and the GPU is at idle.

Services start with `scripts/start_services.sh` (tmux: llama_server, celery_server, live_viewer, telegram_server). Celery is scheduled for deletion; do not build on it.

Python environment is the conda env `alira`.
