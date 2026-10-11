# ADR-0007: GPU arbitration via an OS-level lock

**Status:** Superseded by [ADR-0017](0017-gpu-main-slot.md) — 2026-10-10

## Context

One RTX 5070 Ti with 16,303 MiB serves every local workload. Measured
footprints: Bonsai 2 27B at 98,304 context occupies 14,424 MiB in llama-server
router mode; MedGemma 1.5 4B in bf16 peaks at 8,343 MiB; marker-pdf needs room
of its own. No two of these fit together.

Contention here is not hypothetical. A previously documented incident cost a
352-second reply delay when two unqueued GPU paths competed for leftover VRAM.

The decisive new fact is ADR-0006's batch pattern: **the agent writes scripts
that use the GPU.** A script dcode authored and executed is a process that no
Python-level queue can see. Celery arbitrated only callers that imported it.

## Decision

A single OS-level advisory lock — `flock` on a lockfile — that every GPU
consumer acquires: llama-server requests, MedGemma batch runs, marker-pdf
extraction. Provided as a `with gpu_lock():` helper in Python and an equivalent
shell one-liner, so agent-authored scripts in any language can take it.

## Consequences

- This is the first arbiter that covers code the agent writes itself, which no
  in-process mechanism can.
- It is advisory: a consumer that does not take the lock is not stopped. The
  mitigation is that every GPU-using capability is a library (ADR-0006), and the
  lock lives inside those libraries, so the correct path is also the easy one.
- It replaces Celery's arbitration role entirely (ADR-0008).
- Lock granularity is the whole GPU. A batch run holds it for the batch's
  duration, which is intended: that is one 8.2GB load instead of N.

## Alternatives rejected

- **Keep Celery as the arbiter.** Cannot see agent-authored subprocesses, which
  is now a primary workload rather than an edge case.
- **Convention** — scripts call `unload_all_models()` first and hope. Unenforced
  invariants hold only until the assumption under them changes, which this
  project has already learned once at the cost of 352 seconds.
- **A per-model semaphore in a chat-model adapter.** Correct for model calls
  only; blind to marker-pdf and to MedGemma batch scripts.
