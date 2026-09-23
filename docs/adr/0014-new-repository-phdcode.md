# ADR-0014: New repository, named phdcode

**Status:** Accepted — 2026-09-22
**Supersedes:** [ADR-0013](0013-single-repository.md)

## Context

ADR-0013 chose to keep working in the existing repository, reasoning from a line
count: ~6,076 lines survive largely untouched against ~2,205 churned, which is a
refactor rather than a rewrite. That reasoning was about *code*, and it still
holds.

What it did not weigh is *identity*. ALiRA2 was named as a research assistant
with a tool-calling agent at its centre. After ADR-0001 and ADR-0002 that centre
is gone: the agent is dcode, and what remains — tools, scheduler, adapters,
knowledge store — is the supporting apparatus around someone else's agent. The
name now describes a component of the system rather than the system.

The work is PhD work: literature surveillance, document ingestion, pathology
image analysis, and the code that does it. `phdcode` names that.

## Decision

The project moves to a new repository named `phdcode`, created by **renaming the
existing GitHub repository** rather than by deleting it and initialising a fresh
one.

## Consequences

- Git history is preserved in full, including `b15f05e` — the commit that first
  tracked the 12,000 lines and every design document. ADR-0013's core benefit,
  the ability to diff against a version known to work, survives the rename.
- There is no window in which the project has no remote. Delete-and-recreate
  would have created one, and at the time of this decision the local repository
  was the only copy of anything that mattered.
- `ALiRA2` remains in commit messages, older documents and the learning log.
  That is accepted: renaming history would cost more than the inconsistency does.
  The glossary's retired-terms section serves the same purpose for vocabulary.
- `pyproject.toml` is rewritten regardless — it was never this project's file,
  carrying a digital-pathology dependency stack from an unrelated project.

## Alternatives rejected

- **Keep the ALiRA2 name** (ADR-0013 as written). The line-count argument is
  sound but answers a different question; a name that describes a subsystem
  invites the same "what lives where" confusion this design session existed to
  resolve.
- **Delete the GitHub repository and `git init` fresh.** Discards `b15f05e` as an
  ancestor, so the working system's state becomes unreachable, and creates an
  unbacked-up window during the restructure.
- **A second repository alongside ALiRA2.** Two repositories, one consumer, and
  a version lock between them — rejected in ADR-0013 and still rejected.
