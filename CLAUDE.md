# phdcode

> dcode runs the agent; this project supplies capabilities, scheduling, adapters and knowledge; a run is the unit of work; a file lock arbitrates the GPU.

The repository directory is still named `ALiRA2` and the rename to `phdcode` is pending. See [ADR-0014](docs/adr/0014-new-repository-phdcode.md).

## Read these before you design anything

- **[docs/STATUS.md](docs/STATUS.md)** — what is blocked, what is half-done, what is running. Read it first. Update it when you stop work.
- [docs/adr/](docs/adr/README.md) — 14 ADRs. Every design decision with its rejected alternatives. **Do not re-derive a decision that has an ADR.** Supersede it instead.
- [docs/GLOSSARY.md](docs/GLOSSARY.md) — vocabulary, including retired terms so older documents stay readable.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/ROADMAP.md](docs/ROADMAP.md) — extensive, written before the ADRs, and **not yet reconciled with them**. Where they disagree, the ADRs win.

Commit messages here are long by design and carry reasoning. Read `git log` before you re-investigate something.

## Environment

Activate the conda env first:

```
source ~/miniconda3/etc/profile.d/conda.sh && conda activate alira
```

Services run in tmux: `scripts/start_services.sh`. Celery is scheduled for deletion ([ADR-0008](docs/adr/0008-retire-celery-and-redis.md)); do not build on it.

One RTX 5070 Ti with **16,303 MiB** serves every local workload. This constraint drives most of the design. Bonsai 2 27B takes 14,424 MiB and MedGemma 4B takes 8,343 MiB, so they cannot both be resident.

`llama-server` must run in router mode (`--models-preset`, never `-m`): `/models/unload` returns 404 otherwise, and that endpoint frees VRAM before marker-pdf.

## Conventions

- **Every GPU-using capability takes the GPU lock, inside the library, never in a tool wrapper.** The lock is a `flock`, so scripts the agent writes can take it too ([ADR-0007](docs/adr/0007-gpu-arbitration-via-os-level-lock.md)).
- **The split rule** decides where a new capability goes: a tool if the agent should discover and invoke it by name, a library if the agent may need to compose it. Most are both. The test is *can this block someone else's turn?* ([ADR-0006](docs/adr/0006-capability-split-tools-vs-libraries.md))
- **One agent.** dcode. Do not add a second loop ([ADR-0001](docs/adr/0001-adopt-dcode-as-agent-harness.md), [ADR-0002](docs/adr/0002-delete-in-house-agent-loop.md)).
- A new design decision gets an ADR. A reversal supersedes the old one rather than editing it.
- `docs/STATUS.md` is overwritten, never appended. `git log docs/STATUS.md` is its history.

## Verify, do not assume

This project has repeatedly punished confident prediction:

- A model's tool-calling ability is a property of its chat template. `grep tool_call` in `chat_template.jinja` before you design an agent around it. MedGemma has none.
- Quantization was predicted to break exact-match editing. It did not. Twenty minutes of testing settled it.
- If an agent hangs, check whether the model server received a request at all (`grep -c launch_slot_` in the llama-server log). No request means the fault is client-side, not slow inference.

deepagents and dcode move fast. Check the installed package rather than answering from memory.
