# ADR-0032: The page Triage, the briefs of a model, and the pertinence

**Status:** Accepted — 2026-10-24

## Context

The daily sweep saves each relevant hit as a paper, and the page Papers
showed these papers among the user's own: 98 sweep papers beside 104 of the
user's after ten days. The user wants the new papers in a separate place
first, a short text that says which to read first, a text of the user's
interests from the library, and a summary of findings for each day of the
digest, with the papers ordered by how well they match the interests.

## Decision

1. **A paper is in triage while `papers.origin` is `'sweep'`.** The column
   exists (ADR-0018): the sweep sets it, and a save by the user clears it.
   No new column and no new value of `importance`, because importance is the
   user's judgement after reading (ADR-0019). The page Triage (`/triage`, the
   first sub-tab of Papers) lists these papers; the page Papers lists the
   rest. Both use `papers.html`.
2. **Keep** (`POST /papers/{id}/keep`, on the page Triage and on the page of
   the paper) clears `origin`: the paper moves to Papers and joins the
   library, so triage reads its title from then on. **Delete** moves it to
   the bin, as on the page Papers; the bin still blocks a second save by the
   sweep (ADR-0026). Keep and Delete do not set a label: the labels test
   triage and do not feed it (ADR-0027).
3. **A brief is the prose that a model writes at the top of a page**, one row
   of the table `briefs` for each key: `interests` (page Interests: one
   paragraph to the user, from the titles of the library and the interest
   statements, with 8 to 15 keywords of interest in bold), `triage` (page
   Triage: what the papers cover, and the 3 to 5 to read first, with why),
   and `digest:<date>` (each card of the page Digests: what happened in the
   field in the papers of that day, and the 2 to 4 most pertinent). A brief
   cites a paper as `[P<id>]`, which links to its page.
4. **The sweep writes the three briefs after its digest**, interests first,
   through BRIEF_MODEL (default `claude-haiku-5-5`, a hosted route through
   `claude -p`: the briefs do not wait for the GPU, and a short text from
   20 abstracts needs no local model). A brief that fails (the GPU is cleared, the model
   server is down) logs one line and keeps the old brief; the digest is
   already written. A button on each brief writes it again with a model of
   the menu.
5. **The pertinence of a paper is its hybrid score against the interests
   brief** (`db.pertinence`): the reciprocal rank fusion of the concept
   embedding and BM25 over the concept text, as `search_papers` (ADR-0029),
   ranked among one set of papers and scaled so that 1 is the first rank in
   both lists. The set is the papers of the page Triage, or the papers of one
   day of the digest. The interests brief stores its own embedding, so that
   a page does not call the embedding model. The pertinence orders the page
   Triage, shows beside the triage probability on both pages, and orders the
   papers that a brief reads (at most 20, abstracts cut at 1,500 characters).

## Consequences

- The library grows only by the user's choice: a kept paper changes the
  prompt of triage, the interests brief and the paper of the day.
- The pertinence is rank-based: it moves when the set changes (a paper kept
  or deleted), and BM25 takes its IDF from the set only. It is an order, not
  a calibrated probability.
- Three more model calls for each sweep, on the Claude account that is
  logged in to Claude Code on the server; with a local BRIEF_MODEL, they
  wait for the GPU as triage does.
- The overview and the MCP search still count and search the papers in
  triage.

## Not done

- **A second pertinence score from a MedGemma System One head trained on
  PubMedQA.** The model does not exist yet. When it does, its score goes
  beside the hybrid score, as the shadow score of ADR-0028 went beside
  triage, and a test on the user's Keep and Delete decisions chooses how to
  combine them.
- Keep and Delete of many papers at once.

## Alternatives rejected

- **A value `in-triage` in `importance`.** It mixes the state of a paper
  with the user's judgement of it, and `origin` already holds the state.
- **A stored pertinence column.** The score is rank-based and the interests
  brief changes each day, so a stored score goes stale; the page computes it
  in one query (about 50 to 200 ms).
- **Cosine similarity only.** The search of the repo is hybrid (ADR-0029),
  and the user asked for a hybrid score.
