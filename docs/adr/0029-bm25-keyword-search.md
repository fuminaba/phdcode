# ADR-0029: BM25 ranks the keyword side of the hybrid search

**Status:** Accepted — 2026-10-22

## Context

`search_papers` and `search_chunks` fuse two ranked lists by reciprocal rank:
the cosine distance of pgvector and Postgres full-text search. The keyword
list used `websearch_to_tsquery` and `ts_rank`.

- `websearch_to_tsquery` joins the unquoted words with AND. An agent sends
  long natural-language queries, so few rows have every word. On the 12
  logged queries of `tool_calls` (ADR-0028), the keyword list was empty for
  5 of 9 `search_chunks` queries and 3 of 3 `search_papers` queries. The
  fused result was then the vector list only.
- `ts_rank` has no IDF: a word in most papers ("tumor", "graph") weighs the
  same as a rare word ("PAX5").

## Decision

1. **A row matches if it has any word of the query** (the words of
   `plainto_tsquery`, joined with OR). The GIN index of each `tsvector`
   column still selects the rows.
2. **BM25 ranks the matches**, in SQL over the stored `tsvector` columns
   (`_rrf_cte`, `llm_engine/memory/db.py`): IDF and term frequency from the
   lexemes and their positions, k1 = 1.2, b = 0.75. The corpus (N, the mean
   length) is the rows that pass the filter of the call.
3. The document length is the number of distinct lexemes, because a
   `tsvector` keeps no word count. RRF uses only ranks, so the fusion does not
   change.

## Consequences

- The 8 empty keyword lists now have 20 candidates each.
- A keyword query takes about 50–200 ms on 2,171 chunks (about 2 ms before),
  because the corpus numbers come from a scan of the table. Keep N and the
  mean length in a table if the chunks grow past about 100,000.
- The websearch syntax (`"phrase"`, `-word`) has no effect now: BM25 ranks a
  bag of words.
- Not measured yet: whether the fused top 10 is better. The test is labels on
  about 20 logged queries (nDCG@10, old against new).

## Rejected

- **ParadeDB `pg_search` or Timescale `pg_textsearch`:** a real BM25 index,
  but the image `pgvector/pgvector:pg16` has neither extension. A new image of
  the live database is a larger change than 20 lines of SQL. Re-open this ADR
  if the scan of the corpus becomes slow or the stemming of `english` is not
  good enough.
- **OR with `ts_rank_cd` and length normalization:** it fixes the empty lists,
  but with OR and no IDF a common word ranks the many rows that match only it.
