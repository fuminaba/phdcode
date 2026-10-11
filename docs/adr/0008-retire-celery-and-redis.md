# ADR-0008: Retire Celery and Redis

**Status:** Superseded by [ADR-0017](0017-gpu-main-slot.md) — 2026-10-10

## Context

Celery was introduced to serialize every GPU-touching call through one
`concurrency=1` queue, and to run marker-pdf out of the bot's process.

Two of its six capabilities were ever load-bearing: cross-process GPU
arbitration, and out-of-process execution for marker-pdf. Two were never used —
multi-machine distribution (designed for, never exercised) and the
chat/literature priority switch (built and verified, but nothing ever dispatched
to the `literature` queue). Two have cheaper substitutes.

ADR-0002 deletes `call_model` and `count_prompt_tokens`. ADR-0007 replaces GPU
arbitration with a lock that also covers agent-authored scripts. That leaves
Celery running document ingestion alone.

## Decision

Delete Celery and Redis. marker-pdf becomes a library and CLI (ADR-0006),
invoked as a subprocess holding the GPU lock.

## Consequences

- Removes Redis, the worker process, and `docker-compose`'s redis service.
- Removes two workarounds that exist *only* because of Celery: the
  `in_celery_worker()` deadlock guard, and `pdftext_workers=1` — that
  `AssertionError` came from Celery's prefork pool marking children daemonic, and
  a plain subprocess is not daemonic, so pdftext's own parallelism returns.
- Loses ingestion durability across a crash. Accepted: recovery is re-running
  the ingest, and the run is recorded in `runs` (ADR-0003).
- Loses the multi-machine affordance, including base64 payloads sized to cross a
  host boundary. Accepted: deployment target is one machine.
- Progress reporting moves from Redis pub/sub (`RedisRelayFeedback`) to the
  subprocess's stdout.

## Alternatives rejected

- **Keep it for ingestion durability.** A queue kept alive for one job, whose
  arbitration role has already moved elsewhere.
- **Keep it against future multi-machine use.** It has been available for that
  the whole time and never used; the cost is paid continuously, the benefit
  never realized.
