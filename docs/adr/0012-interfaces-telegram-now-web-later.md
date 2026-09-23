# ADR-0012: Interfaces — Telegram now, web later

**Status:** Accepted — 2026-09-22

## Context

There are currently four surfaces: a Telegram bot, a read-only live viewer, the
dcode TUI, and a Python REPL for MedGemma. None shows a pathology image
usefully, a diff worth approving, or a browsable document library — and the
ROADMAP already wants a document management interface.

`pipeline.py` proved the adapter pattern: it takes plain `user_id`/`chat_id`
strings and never touches Telegram objects, so `webhook_server.py` is a thin
translation layer over it.

## Decision

Telegram stays as the first adapter over the run interface (ADR-0003). A web
application becomes the second adapter, subsuming `live_viewer.py`. Both are
adapters; neither is the core.

## Consequences

- The run interface must stay channel-agnostic, as `pipeline.py` already is.
- Telegram keeps what it is good at — "start this, tell me when it's done" — and
  stops being asked to do what it cannot: images, diffs, approvals, browsing.
- Telegram recompresses images sent via `filters.PHOTO`. For clinical images
  this destroys the fidelity that motivated running MedGemma unquantized, so
  images must be sent as *documents*, and the web app is the real answer.
- Telegram currently has no photo handler at all; images are silently ignored.
- The hook ticker (ADR-0004) presently lives in `webhook_server.py` and should
  move somewhere that does not require Telegram to be running.

## Alternatives rejected

- **Telegram only.** Cannot display the things the system now produces.
- **Web app first, immediately.** Telegram already works and is not the
  bottleneck; building the web app before the harness is settled would mean
  building it against an interface still in motion.
