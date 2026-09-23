# Architecture Decision Records

Each ADR records one decision, the context that forced it, and the alternatives
that were rejected — so a future reader can tell a deliberate choice from an
accident, and knows what to re-open if the context changes.

These were produced in a single design session on 2026-09-22, after the system
had grown to three loosely-related parts (ALiRA2, dcode, MedGemma) and the
stated problem was *conceptual*: no one-sentence answer to "what lives where".
They are therefore best read in order — the later ones are consequences of
ADR-0001.

| # | Decision | Status |
|---|----------|--------|
| [0001](0001-adopt-dcode-as-agent-harness.md) | Adopt dcode as the agent harness | Accepted |
| [0002](0002-delete-in-house-agent-loop.md) | Delete the in-house agent loop | Accepted |
| [0003](0003-runs-are-the-unit-of-work.md) | Runs are the unit of work | Accepted |
| [0004](0004-alira-hooks-remain-the-scheduler.md) | ALiRA2's hooks remain the scheduler | Accepted |
| [0005](0005-integrate-via-extensions-not-mcp.md) | Integrate via dcode extensions, not MCP | Accepted |
| [0006](0006-capability-split-tools-vs-libraries.md) | Capability split: tools vs libraries | Accepted |
| [0007](0007-gpu-arbitration-via-os-level-lock.md) | GPU arbitration via an OS-level lock | Accepted |
| [0008](0008-retire-celery-and-redis.md) | Retire Celery and Redis | Accepted |
| [0009](0009-memory-split-conversation-vs-knowledge.md) | Memory split: conversation vs knowledge | Accepted |
| [0010](0010-observability-via-extension-middleware.md) | Observability via extension middleware | Accepted |
| [0011](0011-per-user-isolation-via-deepagents-home.md) | Per-user isolation via DEEPAGENTS_HOME | Accepted |
| [0012](0012-interfaces-telegram-now-web-later.md) | Interfaces: Telegram now, web later | Accepted |
| [0013](0013-single-repository.md) | Single repository | Superseded by 0014 |
| [0014](0014-new-repository-phdcode.md) | New repository, named phdcode | Accepted |

See [../GLOSSARY.md](../GLOSSARY.md) for the vocabulary these use.
