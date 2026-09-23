# ADR-0003: Runs are the unit of work

**Status:** Accepted — 2026-09-22

## Context

Two things need to make the agent act: a user sending a message, and a timer
firing for scheduled work ("keep me up to date on these keywords"). Previously
these were separate code paths — `handle_incoming_message` and the hook ticker —
both ending in `generate_with_tools`.

dcode is a CLI that runs a thread and exits. It has no daemon mode, but it does
have durable threads (`dcode threads`, `-r/--resume`), so continuity across
invocations does not require a long-lived process.

## Decision

A **run** is the unit of work: one invocation of the agent, recorded in a `runs`
table. A message and a timer are two *triggers* for the same thing.

Each run invokes `DEEPAGENTS_HOME=<user dir> dcode -n "<prompt>" --resume
<thread>` as a subprocess, and records trigger, prompt, thread id, exit status,
and output.

## Consequences

- No agent state lives in process memory, so nothing is lost on reboot or crash.
  Durable state is in Postgres and on disk, which is where it already was.
- The `runs` table is a record, not a queue. Its value is audit and retry: the
  first silently-failed 3am literature sweep is what it exists for.
- Streaming becomes a presentation flag on a run, not a second pipeline.
- Per-invocation process startup costs a fresh Postgres connection and credential
  load. Measured against dcode's ~12,868-token system prompt at ~1,774 tok/s
  (~7s of prefill), connection setup is noise.

## Alternatives rejected

- **A long-lived daemon** holding an agent in memory. Fights dcode's actual
  shape, and is the thing that breaks on reboot and needs a supervisor.
- **Separate interactive and scheduled paths.** Two pipelines to keep in step,
  for a difference that is presentational.
