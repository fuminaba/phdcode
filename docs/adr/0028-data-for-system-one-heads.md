# ADR-0028: Data for System One heads: a shadow score of triage and a log of tool calls

**Status:** Accepted — 2026-10-19

## Context

bonsai2-sysone trains small heads on the frozen vectors of Bonsai 2 27B: the
head reads the vector at each option and at a decide token, and gives a
calibrated probability for each option. On Fumi's 71 week-1 hits, its head
(p2-rank16) gave AUC 0.878 from the titles alone, level with the triage judge
(0.869, which read the abstracts). It cannot choose tools yet: zero-shot, it
got 17 % of 1,937 Claude Code prompts right, and the label (the first tool)
is weak. Both heads need data from phdcode's own use.

## Decision

1. **A shadow score of triage.** `phdcode headscore` runs after the sweep:
   it sends the library (titles, without the hits being scored) and each
   judged hit without a score (title, abstract) to bonsai2-sysone
   (`score_triage.py`, in its own environment), and stores P(yes) in
   `triaged_hits.head_probability`. The judge still decides; the page Labels
   shows the head's score beside the probability, with its AUC on the labels.
2. **The GPU for the head.** The extractor of bonsai2-sysone loads its own
   Bonsai 2 (about 11 GB), which does not fit beside llama-server. So
   headscore stops llama-server with `gpu.clear()` for about a minute and
   starts its socket again with `gpu.restore()`. If a training run has cleared
   the GPU, the hits wait for the next run.
3. **A log of MCP tool calls.** The `tool()` wrapper of the MCP server stores
   each call in the table `tool_calls`: the tool, its arguments, ok or the
   error, and the time in ms. A failed log only warns; the tool call goes on.
   The agent's prompt is not visible to the server: it comes from the agent's
   transcripts (bonsai2-sysone harvests them each day), joined by time.

## Rejected

- **One llama-server for both:** its `--embeddings` mode serves only
  embeddings, so the same server cannot also generate. The runtime that
  encodes, decides and decodes in one context is the later fix
  (bonsai2-sysone doc "System One: encode, decide, decode").
- **A head score that decides now:** 81 labels cannot show that the head is
  better; the shadow score collects the evidence first.

## Consequences

- Each sweep stops llama-server for about a minute more.
- phdcode reads `SYSONE_DIR` and `SYSONE_PYTHON` (defaults: the paths on
  fuminaba-server); without bonsai2-sysone, headscore fails and the sweep
  goes on.
- On the 81 week-1 labels, the head gave AUC 0.836 against 0.766 for the
  stored triage probabilities (the probabilities of the first sweep).
