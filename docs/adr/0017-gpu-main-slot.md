# ADR-0017: GPU policy — embeddings on the CPU, one tenant on the GPU

**Status:** Proposed — 2026-10-10. It becomes Accepted after the VRAM test of
the capped Surya server.
**Supersedes:** [ADR-0007](0007-gpu-arbitration-via-os-level-lock.md), [ADR-0008](0008-retire-celery-and-redis.md)

## Context

One RTX 5070 Ti (16,303 MiB) serves training, a local LLM and Surya (the vision
model of marker 2). The desktop uses about 1.1 GB. ADR-0007 made a `flock`
lock, taken for each call, the GPU arbiter. Three of its premises are false:

- **Training does not take the lock.** `nuclei-seq/runs/chain.sh` has no lock,
  and the only `flock` in tea (`tea/drive.py`) locks a database (checked
  2026-10-08).
- **A model server keeps VRAM between requests.** llama-server holds a model
  after the request that loaded it ends, so a lock for each request does not
  protect the card.
- **Embeddings do not need the GPU.** `nomic-embed-text` on the CPU (Ollama
  `num_gpu: 0`) takes 57 ms for each text (the warm median of 20 texts; the
  first load takes 3.2 s; measured 2026-10-10). A lock for each embedding
  call made search wait for every other workload.

marker 2 runs Surya on a vLLM server. vLLM reserves 90% of the card by default
(`--gpu-memory-utilization 0.9`). marker can use a server that already runs,
through `SURYA_INFERENCE_URL`. A local LLM and Surya do not fit on the card
together.

ADR-0008 deleted Celery. Celery stays deleted. ADR-0008 accepted that ingest is
not durable across a crash; the `parse_mode` of a paper now makes the upgrade
durable.

## Decision

1. **Embeddings run on the CPU all the time** (Ollama `num_gpu: 0`). `embed()`
   never uses the GPU and takes no lock.
2. **The GPU has one tenant at a time:** the local LLM (llama-server, a systemd
   user unit), Surya (vLLM on this machine, with a VRAM cap), or training. A
   phdcode tenant takes `flock /tmp/gpu.lock` for its full life, and checks the
   free VRAM (`nvidia-smi`) before it starts.
3. **The settings** (`.env`):
   - `AGENT_BACKEND`: the route of the model that drives the harness.
   - `SUMMARY_MODEL`: the route for summaries. The default is `AGENT_BACKEND`.
   - the VRAM cap of Surya, and the idle time before a swap or a stop (default
     30 minutes).
4. **`ingest_pdf`** never starts a GPU tenant. If Surya runs, it parses in
   `balanced` mode at once. If not, it parses in text mode (item 8) and sets
   `parse_mode = text`. It stores the chunks first, then makes the summaries
   through `SUMMARY_MODEL`. If a summary step fails for any reason (the server
   is stopped, no model is set, the reply is not valid), the summaries are
   pending: a full-text paper with no methods summary. `ingest_pdf` tells the
   agent why.
5. **The drain** runs on the timer. It exits if the GPU is cleared (item 6) or
   if the lock is held.
   - With a hosted `AGENT_BACKEND`: it starts Surya, upgrades each paper with
     `parse_mode = text`, and makes the pending summaries. Surya stays up until
     it is idle for the idle time, so that new ingests get `balanced` at once.
     Then the drain stops it.
   - With a local `AGENT_BACKEND`: it acts only when llama-server is idle for
     the idle time. It stops llama-server, starts Surya, upgrades the papers,
     stops Surya, starts llama-server, and makes the pending summaries. A
     request during the swap fails with "connection refused", not with an
     out-of-memory error.
6. **Training.** Before a training run, the user runs `phdcode gpu clear`. It
   takes the lock (it waits for a drain), stops Surya and llama-server, writes
   the state file `/tmp/gpu.cleared`, and releases the lock. While the file
   exists, no phdcode tenant starts. After training, the user runs
   `phdcode gpu restore`, which deletes the file. Training launchers do not
   change.
7. **No Celery, no Redis, no queue table.** A crash during the drain leaves the
   papers in `parse_mode = text`, and the next drain does them again.
8. **Triage and the text-mode parse are short GPU jobs, not tenants.** Each
   uses the GPU only if `/tmp/gpu.cleared` does not exist and its VRAM is
   free (the reranker 4 GB, the parse 6 GB); else it runs on the CPU. If CUDA
   runs out of memory, the job continues or runs again on the CPU. The check
   reads the free VRAM outside the process (`gpu.free_vram_mib()`), so a job
   on the CPU holds no CUDA context. Measured: a sweep of 37 hits took 20 s
   on the GPU, against about 2 minutes of triage on the CPU; a 5-page parse
   took 22 s on the GPU and 197 s on the CPU, with the same text. With the
   lock (step 5), a short job takes the lock without a wait, and uses the CPU
   if the lock is held. While a local LLM holds the GPU (Bonsai 2 27B: 14.4
   GB), both jobs use the CPU.

## Consequences

- Search and ingest never wait for the GPU.
- Embeddings use the CPU that training also uses (data loaders). The cost comes
  only during an ingest or a sweep: about 6 s for a paper of 100 chunks.
- A forgotten `phdcode gpu restore` stops all upgrades. Each digest says so
  while `/tmp/gpu.cleared` exists.
- A training run without `phdcode gpu clear` shares the card with any phdcode
  tenant. The admission check stops a new phdcode tenant, but it cannot stop a
  training run.
- phdcode starts and stops llama-server, so llama-server runs as a systemd user
  unit, not in tmux.

- Until the lock exists, a training run that starts without `phdcode gpu
  clear` during a sweep can meet the reranker on the card for some seconds.

## Alternatives rejected

- **An embedding reserve on the GPU.** Chosen first, then rejected after the
  measurement: at 57 ms for each text on the CPU, a reserve adds VRAM, a reload
  after training, and a switch between the CPU and the GPU, for no gain the
  user notices.
- **One wrapper command for training** (`phdcode gpu run -- <command>`). It
  couples each training launcher to phdcode. Two commands work with any
  launcher.
- **Training launchers take the lock.** It needs a change in each launcher.
- **A lock for each call** (ADR-0007). It does not protect a model that stays
  loaded, and it made search wait.
- **Surya beside a local LLM.** They do not fit on the card together.
- **A driver limit for each process (MPS pinned device memory).** Not verified
  on a GeForce card, and MPS changes the scheduling of all processes, training
  included.
