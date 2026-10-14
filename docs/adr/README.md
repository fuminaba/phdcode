# Architecture Decision Records

Each ADR records one decision, the context that forced it, and the alternatives
that were rejected — so a future reader can tell a deliberate choice from an
accident, and knows what to re-open if the context changes.

These were produced in a single design session on 2026-09-22, after the system
had grown to three loosely-related parts (ALiRA2, dcode, MedGemma) and the
stated problem was *conceptual*: no one-sentence answer to "what lives where".
ADR-0015 (2026-10-10) rescoped the project to a literature RAG tool and
superseded most of them. Read 0006 and 0014 to 0021 for the current design.

| # | Decision | Status |
|---|----------|--------|
| [0001](0001-adopt-dcode-as-agent-harness.md) | Adopt dcode as the agent harness | Superseded by 0015 |
| [0002](0002-delete-in-house-agent-loop.md) | Delete the in-house agent loop | Superseded by 0015 |
| [0003](0003-runs-are-the-unit-of-work.md) | Runs are the unit of work | Superseded by 0015 |
| [0004](0004-alira-hooks-remain-the-scheduler.md) | ALiRA2's hooks remain the scheduler | Superseded by 0015 |
| [0005](0005-integrate-via-extensions-not-mcp.md) | Integrate via dcode extensions, not MCP | Superseded by 0016 |
| [0006](0006-capability-split-tools-vs-libraries.md) | Capability split: tools vs libraries | Accepted |
| [0007](0007-gpu-arbitration-via-os-level-lock.md) | GPU arbitration via an OS-level lock | Superseded by 0017 |
| [0008](0008-retire-celery-and-redis.md) | Retire Celery and Redis | Superseded by 0017 |
| [0009](0009-memory-split-conversation-vs-knowledge.md) | Memory split: conversation vs knowledge | Superseded by 0015 |
| [0010](0010-observability-via-extension-middleware.md) | Observability via extension middleware | Superseded by 0015 |
| [0011](0011-per-user-isolation-via-deepagents-home.md) | Per-user isolation via DEEPAGENTS_HOME | Superseded by 0015 |
| [0012](0012-interfaces-telegram-now-web-later.md) | Interfaces: Telegram now, web later | Superseded by 0015 |
| [0013](0013-single-repository.md) | Single repository | Superseded by 0014 |
| [0014](0014-new-repository-phdcode.md) | New repository, named phdcode | Accepted |
| [0015](0015-rescope-to-literature-rag-tool.md) | Rescope phdcode to a literature RAG tool | Accepted |
| [0016](0016-mcp-server-is-the-only-integration.md) | An MCP server over stdio is the only integration | Accepted |
| [0017](0017-gpu-main-slot.md) | GPU policy: embeddings on the CPU, one tenant on the GPU | Accepted (revised 2026-10-11; `balanced` deferred) |
| [0018](0018-triage-by-the-library.md) | Triage asks whether the student with this library wants the paper | Accepted |
| [0019](0019-importance-of-a-paper.md) | The user sets the importance of a paper in the interface | Accepted |
| [0020](0020-daily-question.md) | Daily questions, graded by a model, in the interface | Accepted (item 4 superseded) |
| [0021](0021-questions-steered-by-competency.md) | The daily question is steered by a competency and a question type | Accepted |

An ADR is valid while its premises are true. To re-open one, name the premise
that changed, and supersede it in a new ADR. Edit only the status line of the
old ADR.

See [../GLOSSARY.md](../GLOSSARY.md) for the vocabulary these use.
