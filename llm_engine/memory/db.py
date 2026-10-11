import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import asyncpg
from dotenv import load_dotenv
from pgvector.asyncpg import register_vector

load_dotenv()

log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await register_vector(conn)
    # default=str: without it, a value json.dumps can't natively handle
    # anywhere in a payload (e.g. a raw SQL row containing a Decimal --
    # confirmed real, not hypothetical: this exact thing once silently
    # dropped a whole tool_call row from turn_actions, since
    # write_turn_action's own best-effort try/except just swallows the
    # encoder's failure) raises instead of writing. Applies to every
    # jsonb write through this pool, not just turn_actions.
    await conn.set_type_codec(
        "jsonb", encoder=lambda obj: json.dumps(obj, default=str), decoder=json.loads, schema="pg_catalog",
    )


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


async def create_document(user_id: str, conversation_id: Optional[int], filename: str) -> int:
    """Registers a new document row. Returns its id — used to build the
    per-upload folder (which embeds the id), set afterward via
    set_document_paths() once that folder exists."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO documents (user_id, conversation_id, filename) VALUES ($1, $2, $3) RETURNING id",
            user_id, conversation_id, filename,
        )


async def set_document_paths(document_id: int, dir_path: str, text_path: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET dir_path = $1, text_path = $2 WHERE id = $3",
            dir_path, text_path, document_id,
        )


async def update_document_summary(document_id: int, summary: str, embedding: list[float]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET summary = $1, embedding = $2, updated_at = now() WHERE id = $3",
            summary, embedding, document_id,
        )


async def update_document_metadata(document_id: int, title: Optional[str], authors: list[str]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET title = $1, authors = $2, updated_at = now() WHERE id = $3",
            title, authors, document_id,
        )


async def add_document_keywords(document_id: int, keywords: list[str]) -> None:
    """Upserts each keyword into the shared, deduped `keywords` table
    (case-/whitespace-normalized) and links it to this document via
    document_keywords. No-op for an empty list."""
    if not keywords:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        for word in keywords:
            word = word.strip().lower()
            if not word:
                continue
            keyword_id = await conn.fetchval(
                "INSERT INTO keywords (word) VALUES ($1) "
                "ON CONFLICT (word) DO UPDATE SET word = EXCLUDED.word "
                "RETURNING id",
                word,
            )
            await conn.execute(
                "INSERT INTO document_keywords (document_id, keyword_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                document_id, keyword_id,
            )


async def save_literature_finding(
        user_id: str,
        conversation_id: Optional[int],
        src_db: str,
        db_id: str,
        title: str,
        authors: list[str],
        abstract: str,
        embedding: list[float],
        source_url: Optional[str],
        doi: Optional[str],
        categories: list[str],
) -> int:
    """Saves an arXiv/PubMed search hit as a lightweight documents row
    (document_kind='bookmark') -- title/authors/abstract-as-summary +
    embedding, so it's findable via search_documents' hybrid search right
    alongside fully-ingested uploads, but no dir_path/text_path/chunks
    (read_document/search_document_chunks won't work on one of these; that
    needs a real upload or a future "upgrade to full ingestion" path).

    external_ref ("{src_db}:{db_id}") is a stable dedup key: saving the
    same paper again updates the existing bookmark (ON CONFLICT ... DO
    UPDATE against the partial unique index on (user_id, external_ref))
    rather than creating a duplicate row. categories (arXiv subject tags /
    PubMed publication types) are stored as keywords via the existing
    add_document_keywords, same normalization as an upload's LLM-extracted
    ones. Returns the document_id (new or the existing bookmark's, if this
    was an update)."""
    external_ref = f"{src_db}:{db_id}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        document_id = await conn.fetchval(
            "INSERT INTO documents "
            "    (user_id, conversation_id, filename, document_kind, title, authors, "
            "     summary, embedding, source_url, external_ref, doi) "
            "VALUES ($1, $2, $3, 'bookmark', $4, $5, $6, $7, $8, $9, $10) "
            "ON CONFLICT (user_id, external_ref) WHERE external_ref IS NOT NULL DO UPDATE SET "
            "    title = EXCLUDED.title, authors = EXCLUDED.authors, summary = EXCLUDED.summary, "
            "    embedding = EXCLUDED.embedding, source_url = EXCLUDED.source_url, "
            "    doi = EXCLUDED.doi, updated_at = now() "
            "RETURNING id",
            user_id, conversation_id, external_ref, title, authors,
            abstract, embedding, source_url, external_ref, doi,
        )
    await add_document_keywords(document_id, categories)
    return document_id


async def update_document_methods_summary(document_id: int, summary: str, embedding: list[float]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET methods_summary = $1, methods_embedding = $2, updated_at = now() WHERE id = $3",
            summary, embedding, document_id,
        )


async def update_document_results_summary(document_id: int, summary: str, embedding: list[float]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET results_summary = $1, results_embedding = $2, updated_at = now() WHERE id = $3",
            summary, embedding, document_id,
        )


async def save_document_chunks(
        document_id: int,
        chunks: list[Any],
        embeddings: list[list[float]],
) -> None:
    """Bulk-inserts a document's chunks in one round trip. chunks and
    embeddings must be the same length and in chunk order (chunk_index is
    assigned from their position). Each chunk is a ChunkResult (or anything
    duck-typing .content/.start_line/.end_line) from document_index.chunk_text."""
    pool = await get_pool()
    rows = [
        (document_id, i, chunk.content, chunk.start_line, chunk.end_line, embedding)
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings))
    ]
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO document_chunks (document_id, chunk_index, content, start_line, end_line, embedding) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            rows,
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


def _rrf_cte(table: str, vector_col: str, text_col: str, filter_sql: str) -> str:
    """The shared vector_ranked/text_ranked/fused CTE prefix behind
    search_conversations, search_documents, and search_tool_result_cache --
    identical hybrid-RRF logic in all three, differing only in which table/
    columns/filter they rank over. A caller prepends this to its own final
    SELECT (projecting whatever columns it needs from `fused` joined back to
    `table`) and supplies the same six params in this fixed order:
    $1=query_embedding, $2=filter_sql's own scoping value (user_id,
    tool_name, ...), $3=query text, $4=max_results (used only in the
    caller's own trailing LIMIT, not here), $5=candidate_pool, $6=rrf_k.

    table/vector_col/text_col/filter_sql are always hardcoded string
    literals at each call site, never user input -- same safety profile as
    the plain string-built queries this replaces.

    Deliberately NOT used by search_document_chunks (needs an extra
    document-ownership JOIN inside the CTEs themselves, not just a WHERE
    filter) or search_turns (two independent fusions over different column
    pairs, combined via GREATEST rather than a single fused CTE) -- forcing
    either through this same shape would need extra optional parameters
    just to bend it back into their real structure, trading one
    easy-to-misread SQL string for a harder-to-verify parameterized one."""
    return (
        f"WITH vector_ranked AS ("
        f"    SELECT id, ROW_NUMBER() OVER (ORDER BY {vector_col} <=> $1) AS rank "
        f"    FROM {table} "
        f"    WHERE {filter_sql} AND {vector_col} IS NOT NULL "
        f"    ORDER BY {vector_col} <=> $1 LIMIT $5"
        f"), text_ranked AS ("
        f"    SELECT id, ROW_NUMBER() OVER ("
        f"        ORDER BY ts_rank({text_col}, websearch_to_tsquery('english', $3)) DESC"
        f"    ) AS rank "
        f"    FROM {table} "
        f"    WHERE {filter_sql} AND {text_col} @@ websearch_to_tsquery('english', $3) "
        f"    ORDER BY ts_rank({text_col}, websearch_to_tsquery('english', $3)) DESC LIMIT $5"
        f"), fused AS ("
        f"    SELECT COALESCE(v.id, t.id) AS id, "
        f"           (COALESCE(1.0 / ($6 + v.rank), 0) + COALESCE(1.0 / ($6 + t.rank), 0))::float8 AS score "
        f"    FROM vector_ranked v FULL OUTER JOIN text_ranked t ON v.id = t.id"
        f") "
    )


async def search_documents(
        user_id: str,
        query: str,
        query_embedding: list[float],
        max_results: int = 5,
) -> list[dict[str, Any]]:
    """Hybrid search over this user's summarized documents: reciprocal rank
    fusion of (a) cosine distance between query_embedding and each
    document's whole-document embedding, and (b) Postgres full-text search
    (websearch_to_tsquery) over title + summary + methods_summary +
    results_summary (documents.search_vector, a GENERATED STORED column --
    see schema.sql). Falls back gracefully to pure vector ranking when
    query has no lexical overlap with any document (the text_ranked CTE is
    just empty, contributing 0 to every row's fused score).

    Documents with no summary/embedding yet are excluded. Returns the full
    RAG-oriented record per match: title, authors, keywords, and all three
    summary flavors (general — which covers conceptual/theoretical framing
    too — methods, results) — not just the general one — so the agent can
    get a multi-angle overview from search_documents alone, before deciding
    whether it needs read_document for the full text. Also returns
    document_kind ('upload' or 'bookmark' -- see save_literature_finding)
    plus source_url/doi: a 'bookmark' has no text_path/chunks, so
    read_document/search_document_chunks won't work on one -- only the
    fields returned here are available for it."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _rrf_cte("documents", "embedding", "search_vector", "user_id = $2")
            + "SELECT d.id, d.filename, d.title, d.authors, d.text_path, d.created_at, "
            "       d.summary, d.methods_summary, d.results_summary, d.document_kind, "
            "       d.source_url, d.doi, f.score, "
            "       COALESCE(("
            "           SELECT array_agg(k.word) FROM document_keywords dk "
            "           JOIN keywords k ON k.id = dk.keyword_id "
            "           WHERE dk.document_id = d.id"
            "       ), ARRAY[]::TEXT[]) AS keywords "
            "FROM fused f "
            "JOIN documents d ON d.id = f.id "
            "ORDER BY f.score DESC "
            "LIMIT $4",
            query_embedding, user_id, query, max_results, _CANDIDATE_POOL, _RRF_K,
        )
        return [dict(row) for row in rows]


async def search_document_chunks(
        user_id: str,
        document_id: int,
        query: str,
        query_embedding: list[float],
        max_results: int = 5,
) -> list[dict[str, Any]]:
    """Hybrid search over one document's chunks: reciprocal rank fusion of
    cosine distance (query_embedding vs. each chunk's embedding) and
    Postgres full-text search (websearch_to_tsquery) over each chunk's
    content (document_chunks.search_vector, a GENERATED STORED column —
    see schema.sql) — the drill-deeper counterpart to search_documents, for
    a specific fact or passage the whole-document summary doesn't cover.
    Falls back gracefully to pure vector ranking when query has no lexical
    overlap with any chunk. The JOIN enforces that document_id actually
    belongs to user_id (same defense-in-depth pattern as read_document's
    path scoping): a document_id the caller doesn't own returns [], never
    another user's chunks, even if guessed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "WITH vector_ranked AS ("
            "    SELECT dc.id, ROW_NUMBER() OVER (ORDER BY dc.embedding <=> $1) AS rank "
            "    FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
            "    WHERE dc.document_id = $2 AND d.user_id = $3 "
            "    ORDER BY dc.embedding <=> $1 LIMIT $6"
            "), text_ranked AS ("
            "    SELECT dc.id, ROW_NUMBER() OVER ("
            "        ORDER BY ts_rank(dc.search_vector, websearch_to_tsquery('english', $4)) DESC"
            "    ) AS rank "
            "    FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
            "    WHERE dc.document_id = $2 AND d.user_id = $3 "
            "          AND dc.search_vector @@ websearch_to_tsquery('english', $4) "
            "    ORDER BY ts_rank(dc.search_vector, websearch_to_tsquery('english', $4)) DESC LIMIT $6"
            "), fused AS ("
            "    SELECT COALESCE(v.id, t.id) AS id, "
            "           (COALESCE(1.0 / ($7 + v.rank), 0) + COALESCE(1.0 / ($7 + t.rank), 0))::float8 AS score "
            "    FROM vector_ranked v FULL OUTER JOIN text_ranked t ON v.id = t.id"
            ") "
            "SELECT dc.chunk_index, dc.content, dc.start_line, dc.end_line, f.score "
            "FROM fused f "
            "JOIN document_chunks dc ON dc.id = f.id "
            "ORDER BY f.score DESC "
            "LIMIT $5",
            query_embedding, document_id, user_id, query, max_results, _CANDIDATE_POOL, _RRF_K,
        )
        return [dict(row) for row in rows]


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
