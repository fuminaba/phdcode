# ADR-0017: GPU policy — embeddings on the CPU, one tenant on the GPU

**Status:** Accepted — 2026-10-11 (revised from the proposal of 2026-10-10).
The `balanced` mode (Surya, item 9) stays Proposed until a PDF needs it and the
VRAM test runs.
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

## Revision (2026-10-11)

The proposal made a drain on the timer the main GPU job: it swapped
llama-server for Surya and upgraded each paper from the CPU text parse to
`balanced`. One premise changed: **the text parse runs on the GPU in 22 s for
a 5-page paper, against 197 s on the CPU, with the same text** (measured
2026-10-11). The upgrade adds only OCR, which only a PDF with a bad text layer
needs (a scan, some old PubMed PDFs). So the drain is now `phdcode summarize`
after the sweep, and Surya waits for a real need (item 9).

## Decision

1. **Embeddings run on the CPU all the time** (Ollama `num_gpu: 0`). `embed()`
   never uses the GPU and takes no lock.
2. **The GPU has one tenant at a time:** the local LLM (llama-server) or
   training. A phdcode tenant holds `flock /tmp/gpu.lock` for its full life,
   and checks the free VRAM before it starts.
3. **llama-server starts on demand.** systemd listens on its port
   (`phdcode-llm-proxy.socket`); the first request starts the proxy, which
   starts llama-server (`phdcode-llm.service`, about 10 s with the model
   files in the page cache). The proxy exits after 30 minutes with no
   connection, and llama-server stops with it. So llama-server runs around
   the sweep and while a chat on a local model sends requests, and nothing
   detects a chat. llama-server does not start while the GPU is cleared or
   while less than 14.5 GB of VRAM is free.
4. **Short jobs: the text parse and triage.** Each uses the GPU only if that
   is faster than the CPU, waits included (`gpu.short_job()`):
   - cleared, or llama-server runs: the CPU at once (neither ends soon);
   - another short job holds the lock: wait for it, but at most as long as
     the GPU saves (the expected time on the CPU minus on the GPU); then the
     CPU;
   - less VRAM free than the job needs (the reranker 4 GB, the parse 6 GB),
     for example a training run without `phdcode gpu clear`: the CPU.

   If CUDA runs out of memory, the job continues or runs again on the CPU.
   The VRAM check runs outside the process (`gpu.free_vram_mib()`), so a job
   on the CPU holds no CUDA context. Measured: a 5-page parse 22 s on the GPU
   and 197 s on the CPU; triage 16 ms and 3.1 s for each hit.
5. **The settings** (`.env`): `AGENT_BACKEND` (the route of the model that
   drives the harness) and `SUMMARY_MODEL` (the route for summaries; the
   default is `AGENT_BACKEND`).
6. **`ingest_pdf`** never starts a tenant. It parses in text mode (item 4),
   stores the chunks, then makes the summaries through `SUMMARY_MODEL`, which
   can start llama-server (item 3). If a summary step fails for any reason
   (the GPU is cleared, no model is set, the reply is not valid), the
   summaries are pending, and `ingest_pdf` tells the agent why.
7. **The timer** runs the sweep, then `phdcode summarize`, which makes the
   pending summaries.
8. **Training.** Before a training run, the user runs `phdcode gpu clear`. It
   writes `/tmp/gpu.cleared`, stops llama-server and its socket (a request
   then fails at once), and waits for a short job to release the lock. While
   the file exists, no phdcode GPU user starts. After training, the user runs
   `phdcode gpu restore`: it deletes the file and starts the socket again.
   Training launchers do not change.
9. **Deferred: the `balanced` mode.** marker 2 with a capped Surya vLLM server
   adds OCR. Add it when a PDF with a bad text layer needs it, after the VRAM
   test. The `parse_mode` of a paper is then the queue of the upgrade.
10. **No Celery, no Redis, no queue table.**

## Consequences

- Search and ingest never wait long for the GPU: a short job waits at most
  the time that the GPU saves.
- Embeddings use the CPU that training also uses (data loaders). The cost comes
  only during an ingest or a sweep: about 6 s for a paper of 100 chunks.
- The first request to a stopped llama-server waits about 10 s for the load.
- While llama-server runs (up to 30 minutes after the last request), the parse
  and triage use the CPU.
- A training run without `phdcode gpu clear` shares the card with phdcode: the
  VRAM check stops a new llama-server or short job, but nothing stops the
  training run.
- If llama-server cannot start (too little VRAM), the socket can stop after
  repeated requests; `phdcode gpu restore` starts it again.
- A forgotten `phdcode gpu restore` stops llama-server and the GPU use of
  phdcode; the summaries stay pending.

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
- **The drain as the main GPU job** (the proposal of 2026-10-10): stop
  llama-server, start Surya, upgrade each text paper, start llama-server. The
  GPU text parse made the upgrade useful only for OCR (see "Revision").
- **llama-server all the time.** It holds 14.4 GB, so the parse and triage
  would use the CPU all day, and each training run would need a clear.
- **A short job always waits for the lock.** An ingest could wait for an idle
  llama-server for 30 minutes.
- **A short job never waits.** Two parses at the same time would put the
  second on the CPU, which can take minutes longer than a short wait.
- **Surya beside a local LLM.** They do not fit on the card together.
- **A driver limit for each process (MPS pinned device memory).** Not verified
  on a GeForce card, and MPS changes the scheduling of all processes, training
  included.
