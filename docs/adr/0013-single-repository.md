# ADR-0013: Single repository

**Status:** Superseded by [ADR-0014](0014-new-repository-phdcode.md) — 2026-09-22

## Context

ALiRA2 becomes four things: capability tools, a scheduler, channel adapters, and
a knowledge database — plus dcode configuration, the extension, and the run
runner.

A line count taken during this session found ~6,076 lines surviving largely
untouched (`tooling/` 2,359, `memory/` 1,667, `live_viewer.py` 613,
`webhook_server.py` 366, plus prompts, frontend and hooks) against ~2,205
churned. That is a refactor, not a rewrite.

## Decision

One repository. `llm_engine/` keeps tools, memory and hooks; a new `harness/`
holds the dcode extension, the middlewares, and the run runner. `tgi.py` and
`tasks/` are deleted.

`pyproject.toml` is the one file that gets a clean start: it is currently a
different project's file, named `MIL-Instance-Scoring` and carrying tiffslide,
openslide, napari, monai, zarr, pyvips and other digital-pathology dependencies
this project never imports.

## Consequences

- Diffing against the working version stays possible throughout the refactor.
- The four parts share a database, configuration and documentation; splitting
  them would mean version-locking a library with exactly one consumer.
- Internal boundaries carry the separation instead of repository boundaries, so
  they must be maintained by discipline rather than enforced by packaging.

## Alternatives rejected

- **A new repository.** Would mean copying ~6,000 lines across and losing the
  ability to diff against what worked — a rename with extra steps.
- **Two repositories**, ALiRA2 as a library plus a harness that depends on it.
  Version-locking overhead for a single consumer.
