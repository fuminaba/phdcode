# ADR-0037: The summaries of a large batch run on Bonsai 2 on a transient GPU VM

**Status:** Accepted — 2026-11-17
**Supersedes:** item 6 of [ADR-0036](0036-fetch-open-access-pdfs.md) (the summaries use Claude Haiku)

## Context

ADR-0036 made the summaries with Claude Haiku: about 2.3 min for one paper, so
a backlog of 400 papers is about 16 h of `claude -p` calls. The first real
fetch (2026-11-17) queued 231 PDFs of the PhD library alone: 23 of the
library and 208 of triage. Fumi wants all of them parsed and summarized,
because Ask and the MCP tools search the passages of triage papers too, and
wants Bonsai 2 27B, the local model, not Haiku.

On the server, Bonsai 2 has one slot and shares the one GPU with training
(ADR-0017): a backlog of hundreds of papers would hold the GPU for days.
VMQ (`~/Desktop/dev/VMQ`) already runs a job as a **service**: the VM stays
after its setup, and the sweeper stops it after `idle_hours` with no work.
The PrismML fork of llama.cpp, which alone loads the ternary tensor types of
Bonsai 2, publishes a CUDA 12.8 build with kernels for sm_86 (A6000) and sm_89
(L40); the model is public on Hugging Face (`prism-ml/Ternary-Bonsai-2-27B-gguf`,
`Ternary-Bonsai-2-27B-PQ2_0.gguf`, 7.2 GB, the file of the server).

## Decision

1. **A VMQ profile `bonsai`** (`profiles/bonsai.sh`): it downloads the fork
   build and the model in parallel, and starts llama-server on
   `127.0.0.1:8081` of the VM, model name `bonsai2-27b-vm`, with 8 parallel
   slots of 32k tokens on 48 GB (2 on a smaller GPU), with the sampling values
   of the preset of the server. No compile step. The flavors: L40, then
   A6000. Each step prints its seconds, so the first job measures the setup.
2. **`phdcode summarize --vm`** counts the pending papers. From 20
   (`VM_MIN_PAPERS`) on, it reuses the serving `phdcode-bonsai` job of VMQ,
   else submits one, waits until it serves, opens an SSH tunnel with the key
   of VMQ to the port of the route `bonsai2-27b-vm` in `LLM_ROUTING` (18081 in
   the PhD clone, 18082 in the second clone), and summarizes 8 papers at a
   time. Below 20, it summarizes on the server with `SUMMARY_MODEL`
   (`bonsai2-27b`), one paper at a time; it waits while training holds the GPU.
3. **The VM stops by itself** after 30 min with no request (the VMQ idle
   stop), so the run of the other clone at 06:30 can reuse it. Both clones
   share the folder of the job (`~/.local/share/phdcode/bonsai/`) and its lock.
4. **At most 6 h of VM** (`max_hours` of the profile): the personal account
   had $8.08 of credit, and VMQ needs credit of at least the price × (`max_hours`
   + 1): $7 on an L40. Papers that are not summarized stay pending for the
   next week; a job that fails or does not serve leaves them pending, and the
   command exits with 1.
5. The weekly units and the button "Parse the queue now" run
   `summarize --vm`.

## Consequences

- The GPU of the server makes only the summaries of a small batch.
- `SUMMARY_MODEL` is `bonsai2-27b` again, so the writing coach and the puzzles
  use Bonsai 2 on the server again when the page gives no model.
- An L40 costs $1.00/h. The speed of Bonsai 2 on an L40 with 8 slots is not
  measured yet: the first job measures the setup and the minutes for each
  paper.
- Not checked without a VM: whether the image has the CUDA 12 runtime and
  cuBLAS; `bonsai.sh` installs the NVIDIA wheels when `ldconfig` has none.
- Rejected: Claude Haiku for the backlog (Fumi's choice of the local model).
- Rejected for now: 2× RTX A4000 ($0.30/h). The model fits on one A4000, so it
  needs two llama-servers and a client that spreads the papers over them.
  Add it when the L40 costs too much.
