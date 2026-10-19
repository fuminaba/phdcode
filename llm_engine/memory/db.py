import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Sequence

import asyncpg
from dotenv import load_dotenv
from pgvector.asyncpg import register_vector

load_dotenv()

log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# A facet selects the columns `{facet}_embedding` and `{facet}_tsv` of papers.
# The text of each facet is in a different column (see schema.sql).
FACETS = ("concept", "methods", "results")
_FACET_TEXT = {"concept": "abstract", "methods": "methods_summary", "results": "results_summary"}

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await register_vector(conn)
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool() -> asyncpg.Pool:
    """Lazily creates the shared connection pool. Point DATABASE_URL at a
    different Postgres instance in .env to swap backends with no code change."""
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set.")
        _pool = await asyncpg.create_pool(DATABASE_URL, init=_init_connection)
    return _pool


async def init_schema() -> None:
    """Applies schema.sql. Idempotent (CREATE ... IF NOT EXISTS throughout)."""
    schema_sql = SCHEMA_PATH.read_text().format(embedding_dim=EMBEDDING_DIM)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(schema_sql)


def _check_facet(facet: str) -> str:
    # The facet goes into the SQL text as a column name, so only the fixed
    # names pass.
    if facet not in FACETS:
        raise ValueError(f"Unknown facet {facet!r}; expected one of {FACETS}.")
    return facet


async def save_paper(
        source: Optional[str],
        source_id: Optional[str],
        title: Optional[str],
        authors: Sequence[str],
        abstract: Optional[str],
        concept_embedding: Optional[list[float]],
        published_date: Optional[str] = None,
        venue: Optional[str] = None,
        doi: Optional[str] = None,
        url: Optional[str] = None,
        pdf_url: Optional[str] = None,
        keywords: Sequence[str] = (),
        origin: Optional[str] = None,
) -> int:
    """Stores a paper as abstract-only, or updates a paper that is already
    stored (same source and source_id). A value that is None (or empty
    authors) keeps the stored value, so a call with fewer fields never
    erases metadata, for example the pdf_url that the upgrade needs. A full
    text, if any, stays. A local PDF has source and source_id None and
    always gets a new row. origin "sweep" marks a paper that the sweep
    saved; a save by the user (origin None) makes any paper the user's, and
    a save by the sweep never takes a paper from the user. Returns the
    paper id."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        paper_id = await conn.fetchval(
            "INSERT INTO papers (source, source_id, title, authors, abstract, concept_embedding, "
            "                    published_date, venue, doi, url, pdf_url, origin) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) "
            "ON CONFLICT (source, source_id) DO UPDATE SET "
            "    title = COALESCE(EXCLUDED.title, papers.title), "
            "    authors = COALESCE(NULLIF(EXCLUDED.authors, '{}'), papers.authors), "
            "    abstract = COALESCE(EXCLUDED.abstract, papers.abstract), "
            "    concept_embedding = COALESCE(EXCLUDED.concept_embedding, papers.concept_embedding), "
            "    published_date = COALESCE(EXCLUDED.published_date, papers.published_date), "
            "    venue = COALESCE(EXCLUDED.venue, papers.venue), "
            "    doi = COALESCE(EXCLUDED.doi, papers.doi), "
            "    url = COALESCE(EXCLUDED.url, papers.url), "
            "    pdf_url = COALESCE(EXCLUDED.pdf_url, papers.pdf_url), "
            "    origin = CASE WHEN EXCLUDED.origin IS NULL THEN NULL ELSE papers.origin END, "
            "    updated_at = now() "
            "RETURNING id",
            source, source_id, title, list(authors), abstract, concept_embedding,
            published_date, venue, doi, url, pdf_url, origin,
        )
        await _link_keywords(conn, paper_id, keywords)
    return paper_id


async def get_paper(paper_id: int) -> Optional[dict[str, Any]]:
    """The stored record of one paper, with its facet texts, its keywords and
    its chunk count, or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT p.id, p.source, p.source_id, p.title, p.authors, p.published_date, p.venue, "
            "       p.doi, p.url, p.pdf_url, p.abstract, p.methods_summary, p.results_summary, "
            "       p.parse_mode, p.importance, "
            "       (SELECT count(*) FROM chunks c WHERE c.paper_id = p.id) AS chunks, "
            "       COALESCE(("
            "           SELECT array_agg(k.word ORDER BY k.word) FROM paper_keywords pk "
            "           JOIN keywords k ON k.id = pk.keyword_id WHERE pk.paper_id = p.id"
            "       ), ARRAY[]::TEXT[]) AS keywords "
            "FROM papers p WHERE p.id = $1",
            paper_id,
        )
    return dict(row) if row else None


async def find_paper(source: str, source_id: str) -> Optional[int]:
    """The id of the stored paper with this source and source_id, or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM papers WHERE source = $1 AND source_id = $2", source, source_id,
        )


async def library_titles() -> list[str]:
    """The titles of the user's papers (not the sweep's), oldest first: the
    library that triage reads (ADR-0018)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT title FROM papers WHERE origin IS NULL AND title IS NOT NULL ORDER BY id")
    return [row["title"] for row in rows]


async def find_paper_by_doi(doi: str) -> Optional[int]:
    """The id of a stored paper with this DOI (any source), or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT id FROM papers WHERE lower(doi) = lower($1) ORDER BY id LIMIT 1", doi)


async def find_paper_by_pdf_source(pdf_source: str) -> Optional[int]:
    """The id of the paper whose PDF queue holds (or held) this source, or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT id FROM papers WHERE pdf_source = $1 ORDER BY id LIMIT 1", pdf_source)


async def find_paper_in_text(text: str) -> Optional[int]:
    """The stored paper whose title is in the text (letters and digits only,
    any case), or None. The longest title wins, so that the first page of
    "How Attentive are Graph Attention Networks?" is not the PDF of "Graph
    Attention Networks"."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM (SELECT id, regexp_replace(lower(title), '[^a-z0-9]', '', 'g') AS t FROM papers) p "
            "WHERE t <> '' AND strpos(regexp_replace(lower($1), '[^a-z0-9]', '', 'g'), t) > 0 "
            "ORDER BY length(t) DESC, id LIMIT 1",
            text,
        )


async def full_text_papers(paper_ids: Optional[Sequence[int]] = None) -> list[dict[str, Any]]:
    """The full-text papers (all, or these ids) with their parse mode and files."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, parse_mode, pdf_path, text_path FROM papers "
            "WHERE parse_mode IS NOT NULL AND ($1::bigint[] IS NULL OR id = ANY($1)) ORDER BY id",
            None if paper_ids is None else list(paper_ids),
        )
    return [dict(row) for row in rows]


async def queue_pdf(paper_id: int, pdf_source: str) -> None:
    """Puts the PDF of a paper (a URL or a local path) into the queue of
    `phdcode ingest`, and clears an earlier failure."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE papers SET pdf_source = $2, pdf_error = NULL WHERE id = $1",
                           paper_id, pdf_source)


async def queued_pdfs() -> list[dict[str, Any]]:
    """The abstract-only papers in the PDF queue that did not fail, oldest first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, source_id, pdf_source FROM papers "
            "WHERE pdf_source IS NOT NULL AND parse_mode IS NULL AND pdf_error IS NULL ORDER BY id"
        )
    return [dict(row) for row in rows]


async def set_pdf_error(paper_id: int, error: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE papers SET pdf_error = $2 WHERE id = $1", paper_id, error)


async def update_paper_metadata(paper_id: int, title: Optional[str], authors: Sequence[str]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE papers SET title = $1, authors = $2, updated_at = now() WHERE id = $3",
            title, list(authors), paper_id,
        )


async def add_paper_keywords(paper_id: int, keywords: Sequence[str]) -> None:
    """Links keywords (case- and whitespace-normalized, shared by all papers)
    to this paper. No-op for an empty list."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _link_keywords(conn, paper_id, keywords)


async def _link_keywords(conn: asyncpg.Connection, paper_id: int, keywords: Sequence[str]) -> None:
    # Two statements for any number of keywords: a single statement does not
    # see the keyword rows that its own CTE inserts.
    words = sorted({w.strip().lower() for w in keywords} - {""})
    if not words:
        return
    await conn.execute(
        "INSERT INTO keywords (word) SELECT unnest($1::text[]) ON CONFLICT (word) DO NOTHING",
        words,
    )
    await conn.execute(
        "INSERT INTO paper_keywords (paper_id, keyword_id) "
        "SELECT $1, id FROM keywords WHERE word = ANY($2::text[]) ON CONFLICT DO NOTHING",
        paper_id, words,
    )


async def set_facet(paper_id: int, facet: str, text: str, embedding: list[float]) -> None:
    """Writes the text and the embedding of one facet. For 'concept' the text
    is the abstract."""
    facet = _check_facet(facet)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE papers SET {_FACET_TEXT[facet]} = $1, {facet}_embedding = $2, updated_at = now() "
            f"WHERE id = $3",
            text, embedding, paper_id,
        )


async def set_full_text(
        paper_id: int,
        parse_mode: str,
        pdf_path: str,
        text_path: str,
        chunks: Sequence[Any],
        embeddings: Sequence[list[float]],
        links: Sequence[Any] = (),
) -> None:
    """Replaces all chunks of a paper and their links, and records how its
    PDF was parsed, in one transaction. The first parse and the upgrade
    both use this, so a crash leaves the old chunks and the old parse_mode.
    Each chunk is a document_index.Chunk, in text order; each link a
    document_index.Link."""
    rows = [
        (paper_id, i, chunk.content, chunk.page_start, chunk.page_end, chunk.section or None, embedding)
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True))
    ]
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM chunk_links WHERE paper_id = $1", paper_id)
        await conn.execute("DELETE FROM chunks WHERE paper_id = $1", paper_id)
        await conn.executemany(
            "INSERT INTO chunks (paper_id, chunk_index, content, page_start, page_end, section, embedding) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            rows,
        )
        await conn.executemany(
            "INSERT INTO chunk_links (paper_id, from_index, to_index, kind, label) VALUES ($1, $2, $3, $4, $5)",
            [(paper_id, link.from_index, link.to_index, link.kind, link.label) for link in links],
        )
        await conn.execute(
            "UPDATE papers SET parse_mode = $1, pdf_path = $2, text_path = $3, updated_at = now() "
            "WHERE id = $4",
            parse_mode, pdf_path, text_path, paper_id,
        )


# Reciprocal rank fusion: score(row) = sum over each ranking method of
# 1 / (RRF_K + rank_in_that_method). RRF_K=60 is the standard constant from
# the original RRF paper (Cormack et al. 2009) -- chosen so no single
# top-ranked hit from one method can completely dominate the fused order,
# without needing to normalize cosine distance and ts_rank onto the same
# scale (they aren't comparable numbers otherwise). CANDIDATE_POOL is how
# many rows *each* method ranks before fusion -- wider than max_results so
# fusion has enough to work with; the final result is still capped at
# max_results after combining.
_RRF_K = 60
_CANDIDATE_POOL = 20
# The links of chunk c, as a JSON array (asyncpg returns it as text).
_LINKS_OF_CHUNK = (
    "(SELECT coalesce(json_agg(json_build_object('to', l.to_index, 'kind', l.kind, 'label', l.label) "
    "ORDER BY l.to_index), '[]') FROM chunk_links l "
    "WHERE l.paper_id = c.paper_id AND l.from_index = c.chunk_index)"
)


async def pending_papers() -> list[dict[str, Any]]:
    """The full-text papers with a pending model step: no methods or results
    summary, or a local PDF (no source) with no title. needs_metadata marks
    the second kind."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, text_path, (source IS NULL AND title IS NULL) AS needs_metadata FROM papers "
            "WHERE parse_mode IS NOT NULL "
            "AND (methods_summary IS NULL OR results_summary IS NULL OR (source IS NULL AND title IS NULL)) "
            "ORDER BY id"
        )
    return [dict(row) for row in rows]


def _rrf_cte(table: str, vector_col: str, text_col: str, where: str = "TRUE") -> str:
    """The shared vector_ranked/text_ranked/fused CTE prefix behind
    search_papers and search_chunks. A caller prepends this to its own final
    SELECT (projecting whatever columns it needs from `fused` joined back to
    `table`) and supplies these params in this fixed order:
    $1=query_embedding, $2=query text, $3=max_results (used only in the
    caller's own trailing LIMIT, not here), $4=candidate_pool, $5=rrf_k.
    `where` can use $6 and later.

    table/vector_col/text_col/where are always string literals or checked
    facet names at each call site, never user input."""
    return (
        f"WITH vector_ranked AS ("
        f"    SELECT id, ROW_NUMBER() OVER (ORDER BY {vector_col} <=> $1) AS rank "
        f"    FROM {table} "
        f"    WHERE {where} AND {vector_col} IS NOT NULL "
        f"    ORDER BY {vector_col} <=> $1 LIMIT $4"
        f"), text_ranked AS ("
        f"    SELECT id, ROW_NUMBER() OVER ("
        f"        ORDER BY ts_rank({text_col}, websearch_to_tsquery('english', $2)) DESC"
        f"    ) AS rank "
        f"    FROM {table} "
        f"    WHERE {where} AND {text_col} @@ websearch_to_tsquery('english', $2) "
        f"    ORDER BY ts_rank({text_col}, websearch_to_tsquery('english', $2)) DESC LIMIT $4"
        f"), fused AS ("
        f"    SELECT COALESCE(v.id, t.id) AS id, "
        f"           (COALESCE(1.0 / ($5 + v.rank), 0) + COALESCE(1.0 / ($5 + t.rank), 0))::float8 AS score "
        f"    FROM vector_ranked v FULL OUTER JOIN text_ranked t ON v.id = t.id"
        f") "
    )


async def search_papers(
        query: str,
        query_embedding: list[float],
        facet: str = "concept",
        max_results: int = 5,
) -> list[dict[str, Any]]:
    """Hybrid search over one facet of the papers: reciprocal rank fusion of
    the cosine distance to `{facet}_embedding` and full-text search over
    `{facet}_tsv`. A paper without that facet (an abstract-only paper, for
    methods or results) is not a candidate. Returns the text of the searched
    facet as `text`, with the metadata and the parse_mode of each paper."""
    facet = _check_facet(facet)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _rrf_cte("papers", f"{facet}_embedding", f"{facet}_tsv")
            + f"SELECT p.id, p.source, p.source_id, p.title, p.authors, p.published_date, p.venue, "
            f"       p.doi, p.url, p.parse_mode, p.{_FACET_TEXT[facet]} AS text, f.score, "
            f"       COALESCE(("
            f"           SELECT array_agg(k.word) FROM paper_keywords pk "
            f"           JOIN keywords k ON k.id = pk.keyword_id "
            f"           WHERE pk.paper_id = p.id"
            f"       ), ARRAY[]::TEXT[]) AS keywords "
            f"FROM fused f JOIN papers p ON p.id = f.id "
            f"ORDER BY f.score DESC LIMIT $3",
            query_embedding, query, max_results, _CANDIDATE_POOL, _RRF_K,
        )
    return [dict(row) for row in rows]


async def search_chunks(
        query: str,
        query_embedding: list[float],
        paper_ids: Optional[Sequence[int]] = None,
        max_results: int = 5,
) -> list[dict[str, Any]]:
    """Hybrid search over the chunks of the given papers (all full-text
    papers if paper_ids is None). Returns the content and the pages of each
    chunk, with the paper id and title, which make the citation.

    pgvector applies the paper_ids filter after the HNSW scan. Without an
    iterative scan, the first ef_search (40) nearest chunks of the whole
    table can all belong to other papers, and the vector half comes back
    empty. relaxed_order can return the candidates slightly out of order;
    ROW_NUMBER in vector_ranked sorts them again by exact distance."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        rows = await conn.fetch(
            _rrf_cte("chunks", "embedding", "search_vector", "($6::bigint[] IS NULL OR paper_id = ANY($6))")
            + "SELECT c.paper_id, p.title, c.chunk_index, c.page_start, c.page_end, c.section, c.content, "
            f"{_LINKS_OF_CHUNK} AS links, f.score "
            "FROM fused f JOIN chunks c ON c.id = f.id JOIN papers p ON p.id = c.paper_id "
            "ORDER BY f.score DESC LIMIT $3",
            query_embedding, query, max_results, _CANDIDATE_POOL, _RRF_K,
            None if paper_ids is None else list(paper_ids),
        )
    return [dict(row) for row in rows]


async def get_chunks(paper_id: int, chunk_indexes: Sequence[int]) -> list[dict[str, Any]]:
    """The chunks of one paper with these indexes, in text order, with the
    title of the paper and the links of each chunk."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.paper_id, p.title, c.chunk_index, c.page_start, c.page_end, c.section, c.content, "
            f"{_LINKS_OF_CHUNK} AS links "
            "FROM chunks c JOIN papers p ON p.id = c.paper_id "
            "WHERE c.paper_id = $1 AND c.chunk_index = ANY($2) ORDER BY c.chunk_index",
            paper_id, list(chunk_indexes),
        )
    return [dict(row) for row in rows]


async def add_saved_search(source: str, query: str, interest: str, threshold: float = 0.5) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO saved_searches (source, query, interest, threshold) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            source, query, interest, threshold,
        )


async def list_saved_searches() -> list[dict[str, Any]]:
    """The saved searches that are not cancelled, oldest first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, source, query, interest, threshold, created_at, last_swept_at "
            "FROM saved_searches WHERE cancelled_at IS NULL ORDER BY id"
        )
    return [dict(row) for row in rows]


async def cancel_saved_search(saved_search_id: int) -> bool:
    """Returns False if no active saved search has this id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        status = await conn.execute(
            "UPDATE saved_searches SET cancelled_at = now() WHERE id = $1 AND cancelled_at IS NULL",
            saved_search_id,
        )
    return status == "UPDATE 1"


async def triaged_source_ids(saved_search_id: int, source_ids: Sequence[str]) -> set[str]:
    """The source_ids that triage already judged for this saved search."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_id FROM triaged_hits WHERE saved_search_id = $1 AND source_id = ANY($2)",
            saved_search_id, list(source_ids),
        )
    return {row["source_id"] for row in rows}


async def record_triage(saved_search_id: int, source_id: str, probability: float) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO triaged_hits (saved_search_id, source_id, probability) VALUES ($1, $2, $3) "
            "ON CONFLICT (saved_search_id, source_id) DO UPDATE SET probability = EXCLUDED.probability, "
            "triaged_at = now()",
            saved_search_id, source_id, probability,
        )


async def mark_swept(saved_search_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE saved_searches SET last_swept_at = now() WHERE id = $1", saved_search_id)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
