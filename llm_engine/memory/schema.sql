CREATE EXTENSION IF NOT EXISTS vector;

-- One row for each paper (ADR-0015). Each facet has a text column, an
-- embedding and a keyword index, so that search can target one facet:
--   concept = title + abstract (the author's own summary, never generated)
--   methods, results = summaries of the full text
-- parse_mode NULL = abstract-only paper. 'text' or 'balanced' = full-text
-- paper; a 'text' paper waits for the upgrade (ADR-0017).
CREATE TABLE IF NOT EXISTS papers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source TEXT CHECK (source IN ('arxiv', 'pubmed', 'openalex')),  -- NULL for a local PDF
    source_id TEXT,
    title TEXT,
    authors TEXT[] NOT NULL DEFAULT '{{}}',
    published_date TEXT,
    venue TEXT,
    doi TEXT,
    url TEXT,
    pdf_url TEXT,
    abstract TEXT,
    concept_embedding vector({embedding_dim}),
    methods_summary TEXT,
    methods_embedding vector({embedding_dim}),
    results_summary TEXT,
    results_embedding vector({embedding_dim}),
    parse_mode TEXT CHECK (parse_mode IN ('text', 'balanced')),
    pdf_path TEXT,
    text_path TEXT,
    concept_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(abstract, ''))
    ) STORED,
    methods_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(methods_summary, ''))
    ) STORED,
    results_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(results_summary, ''))
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULLs are distinct, so local PDFs (source NULL) never collide.
    UNIQUE (source, source_id)
);

-- The PDF queue (phdcode import, phdcode ingest): pdf_source is the URL or
-- the local path of the PDF of an abstract-only paper; `phdcode ingest`
-- parses each paper that has one. pdf_error is why the last attempt failed;
-- the paper then leaves the queue until its source is set again.
ALTER TABLE papers ADD COLUMN IF NOT EXISTS pdf_source TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS pdf_error TEXT;

CREATE INDEX IF NOT EXISTS papers_concept_embedding_idx
    ON papers USING hnsw (concept_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS papers_methods_embedding_idx
    ON papers USING hnsw (methods_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS papers_results_embedding_idx
    ON papers USING hnsw (results_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS papers_concept_tsv_idx ON papers USING GIN (concept_tsv);
CREATE INDEX IF NOT EXISTS papers_methods_tsv_idx ON papers USING GIN (methods_tsv);
CREATE INDEX IF NOT EXISTS papers_results_tsv_idx ON papers USING GIN (results_tsv);
CREATE INDEX IF NOT EXISTS papers_parse_mode_idx ON papers (parse_mode);

-- Source categories (arXiv subjects, PubMed publication types) and the
-- keywords that the metadata prompt extracts from a local PDF.
CREATE TABLE IF NOT EXISTS keywords (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    word TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS paper_keywords (
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    keyword_id BIGINT NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    PRIMARY KEY (paper_id, keyword_id)
);

CREATE INDEX IF NOT EXISTS paper_keywords_keyword_id_idx ON paper_keywords (keyword_id);

-- The full text of a full-text paper. page_start/page_end come from marker's
-- page separators and make the citation (paper, page, exact quote). An
-- upgrade replaces all chunks of a paper, so chunk ids stay internal.
CREATE TABLE IF NOT EXISTS chunks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    page_start INT NOT NULL,
    page_end INT NOT NULL,
    embedding vector({embedding_dim}) NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    UNIQUE (paper_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_search_vector_idx ON chunks USING GIN (search_vector);

-- A standing query to one source, with the interest statement that triage
-- judges each hit against. cancelled_at NOT NULL = cancelled; the row stays
-- so that old digests still resolve.
CREATE TABLE IF NOT EXISTS saved_searches (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('arxiv', 'pubmed', 'openalex')),
    query TEXT NOT NULL,
    interest TEXT NOT NULL,
    threshold REAL NOT NULL DEFAULT 0.5 CHECK (threshold BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_swept_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ
);

-- Each hit that triage judged, so that the next sweep skips it. The sweep
-- searches a window of some days (sweep.py), and a hit that waits for triage
-- has no row, so the next sweep judges it.
CREATE TABLE IF NOT EXISTS triaged_hits (
    saved_search_id BIGINT NOT NULL REFERENCES saved_searches (id),
    source_id TEXT NOT NULL,
    probability REAL NOT NULL CHECK (probability BETWEEN 0 AND 1),
    triaged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (saved_search_id, source_id)
);
