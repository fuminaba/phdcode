# ADR-0018: Triage asks whether the student with this library wants the paper

**Status:** Accepted — 2026-10-12
**Supersedes:** item 7 (Triage) of [ADR-0015](0015-rescope-to-literature-rag-tool.md)

## Context

ADR-0015 made triage a pointwise decision on the pair (the interest statement
of a saved search, the paper), by a reranker, with a threshold for each
search. Its premise: the interest statement describes what the user wants
from that search. The first labelled week showed the premise false.

The sweep of 2026-10-12 gave 81 hits for 10 saved searches. The user ticked 26
as relevant, by their own interest, not by the wording of each search: a
retinal paper and a prostate ultrasound paper yes, a whole-slide-image MIL
paper that matches its search exactly no. Ranking quality against these
labels (AUC; 71 hits that could be fetched again, 24 relevant):

| Score | AUC |
| --- | --- |
| Qwen3-Reranker-4B, interest statements (with or without exclusions) | 0.59 to 0.69 |
| Qwen3-Reranker-0.6B, interest statements | 0.83 |
| Embedding similarity to the library (mean of the 3 nearest) | 0.84 |
| Qwen3-Reranker-0.6B, the library titles as the query | 0.87 |
| Bonsai 2 27B: "would the PhD student with this library want to read it?" | 0.87 |

The better a model followed the interest statements, the worse it matched the
user. The 0.6B reranker with the library scores near zero (the threshold that
misses 7 of 24 is 0.01), so its threshold is fragile; the LLM gives usable
probabilities (at 0.3 it kept 45 of 71 and missed 1).

## Decision

1. **Triage asks the summary model** (`TRIAGE_MODEL`, default
   `SUMMARY_MODEL`) one question for each hit: given the titles of the user's
   library, the probability that this PhD student wants to read the paper
   (its title and abstract). The prompt is `prompts.triage_prompt`; a change of
   its wording needs the same test against labels.
2. **The library is the user's papers only.** A new column `papers.origin` is
   `'sweep'` for a paper that the sweep saved, and NULL for a paper that the
   user added (import, `phdcode queue`, `save_paper`, `ingest_pdf`). A save by
   the user makes a paper the user's; a save by the sweep never takes one from
   the user. A hit that the user marks as relevant becomes the user's paper.
   So the library cannot grow from the sweep's own judgements.
3. **Thinking is off and the temperature is 0** for this call: about 1 s for
   each hit, because llama-server caches the prompt of the library.
4. **One threshold, 0.3,** for each saved search (the per-search value stays,
   for a later change). The interest statement of a saved search only orders
   its relevant hits in the digest.
5. **No fallback model.** If the model server cannot answer (the GPU is
   cleared for training), triage raises, the hits wait, and the next sweep
   looks back to the last complete sweep of the search. One model makes all
   judgements, so that the scores of two days compare.
6. **The reranker is removed** (`TRIAGE_RERANKER`).

## Consequences

- Triage needs llama-server, a GPU tenant of 14.4 GB (ADR-0017). The sweep unit
  runs the PDF queue first (its parses use the free GPU), then the sweep, then
  the summaries on the same llama-server.
- During training, no hit is judged; the hits wait. A wait longer than the
  window of the sweep is safe, up to the 50 hits for each search that a sweep
  reads (`MAX_HITS`).
- The prompt grows with the library: about 2,000 tokens for 104 titles.
  Hundreds of titles still fit the context of the model.
- The labels of each week enlarge the labelled set; with more weeks, the
  threshold and the prompt can be tested again.

## Alternatives rejected

- **The 4B reranker with the interest statements** (tried on 2026-10-12): it
  follows the wording, and the wording does not describe the user.
- **Embedding similarity to the library:** AUC 0.84, close, and it needs no
  GPU. ADR-0015 rejected similarity alone as seldom reliable for relevance;
  one week of labels cannot show that it is as good as the LLM.
- **The 0.6B reranker with the library as the query:** the same AUC as the LLM,
  but its scores sit near zero, so a threshold is fragile.
- **A description of the user instead of the library:** the user's question
  was "a student who has this library"; the titles need no upkeep.
