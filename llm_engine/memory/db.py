import json
import logging
import os
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

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


async def create_conversation(user_id: str, chat_id: str) -> int:
    """Registers a new conversation row. Returns its id — used both to build
    the transcript filename (which embeds the id) and to attach turns via
    save_turn(). file_path starts NULL; set it via set_conversation_file_path()
    once the transcript file exists (it needs this id to name itself)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO conversations (user_id, chat_id) VALUES ($1, $2) RETURNING id",
            user_id, chat_id,
        )


async def get_open_conversation(chat_id: str) -> Optional[dict[str, Any]]:
    """The most recent conversation for chat_id that hasn't been archived
    yet (summary IS NULL), if any -- pipeline.get_conversation's fallback
    when this chat has no in-memory entry (e.g. right after a process
    restart, which clears that cache but never touches the DB). Without
    this, a restart would silently orphan whatever conversation was open
    -- a fresh one gets created, the old one sits in the DB forever
    unarchived and invisible to search_memory, and any reply to it lands
    in the wrong conversation. Only ever looks at chat_id, never user_id:
    which conversation is "open" for a chat is independent of who's
    asking a given message in it.

    Also returns last_active_at (the most recent turn's created_at, or
    the conversation's own created_at if it has no turns yet) so the
    caller can still apply CONVERSATION_IDLE_TIMEOUT_SECONDS on this
    path -- confirmed a real gap otherwise, not hypothetical: this
    fallback used to skip the idle check entirely (it only ran on the
    in-memory cache-HIT path), so a process restart during active
    development (this fallback's own reason for existing) permanently
    bypassed idle-archival for that chat from then on -- a real
    conversation sat open across 2.7 days and several restarts, and a
    later message resumed it wholesale, complete with several-day-stale
    "today's date" turns still sitting in its live history."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT c.id, c.file_path, "
            "       COALESCE((SELECT MAX(created_at) FROM conversation_turns WHERE conversation_id = c.id), c.created_at) AS last_active_at "
            "FROM conversations c "
            "WHERE c.chat_id = $1 AND c.summary IS NULL "
            "ORDER BY c.id DESC LIMIT 1",
            chat_id,
        )
        return dict(row) if row else None


async def set_conversation_file_path(conversation_id: int, file_path: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE conversations SET file_path = $1 WHERE id = $2",
            file_path, conversation_id,
        )


async def save_turn(
        conversation_id: int,
        user_id: str,
        user_prompt: str,
        result: dict[str, Any],
        model_name: Optional[str] = None,
        turn_id: Optional[UUID] = None,
        prompt_embedding: Optional[list[float]] = None,
        output_embedding: Optional[list[float]] = None,
) -> None:
    """JSONB backup of one turn (prompt, output, evaluation) — mirrors what
    ConversationTranscript.append_turn() writes to the markdown file.
    turn_id links this row to its own detailed, live history in
    turn_actions (WHERE turn_id = $1 ORDER BY created_at) -- the detailed
    trace itself lives only there now, not duplicated into result.

    prompt_embedding/output_embedding (memory/persist.py computes these
    before calling in) back search_turns' fine-grained recall -- kept
    optional so a caller that can't or doesn't want to embed (or an
    embedding call that failed) still persists the turn itself rather
    than losing it entirely; that row just won't surface in search_turns
    until backfilled."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO conversation_turns "
            "(conversation_id, user_id, user_prompt, result, model_name, turn_id, prompt_embedding, output_embedding) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            conversation_id, user_id, user_prompt, result, model_name, turn_id,
            prompt_embedding, output_embedding,
        )


async def get_turns_missing_embeddings(batch_size: int = 100) -> list[dict[str, Any]]:
    """One page of conversation_turns rows still missing an embedding on
    either side -- scripts/backfill_turn_embeddings.py's own query.
    Ordered oldest-first so a backfill that's interrupted partway through
    and rerun just picks up roughly where it left off, not because
    anything tracks progress explicitly."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, user_prompt, result->>'output' AS output "
            "FROM conversation_turns "
            "WHERE prompt_embedding IS NULL OR output_embedding IS NULL "
            "ORDER BY id LIMIT $1",
            batch_size,
        )
        return [dict(row) for row in rows]


async def update_turn_embeddings(
        id: int, prompt_embedding: Optional[list[float]], output_embedding: Optional[list[float]],
) -> None:
    """Backfills one existing conversation_turns row's embeddings in
    place -- id here is the row's own primary key (not turn_id), matching
    what get_turns_missing_embeddings returns."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE conversation_turns SET prompt_embedding = $1, output_embedding = $2 WHERE id = $3",
            prompt_embedding, output_embedding, id,
        )


async def write_turn_action(
        turn_id: UUID,
        conversation_id: Optional[int],
        user_id: str,
        action_type: str,
        payload: dict[str, Any],
) -> None:
    """Appends one row to the live, granular action log (see schema.sql's
    comment on turn_actions for the full action_type vocabulary). Always
    best-effort -- swallows its own failures rather than raising, since a
    live-log write going wrong should never break the actual turn/pipeline
    it's describing. Callers in a sync context (tgi.py, running inside a
    worker thread) still need their own run_on_main_loop bridge; this
    function itself is just the async DB write."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO turn_actions (turn_id, conversation_id, user_id, action_type, payload) "
                "VALUES ($1, $2, $3, $4, $5)",
                turn_id, conversation_id, user_id, action_type, payload,
            )
    except Exception as e:
        log.warning(f"Couldn't write turn_action ({action_type}, turn_id={turn_id}): {e}")


async def get_recent_turn_actions(since_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    """Rows from turn_actions with id > since_id, oldest first, capped at
    limit -- live_viewer.py's polling query: pass the highest id seen so
    far as since_id to get only what's new since the last poll."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, turn_id, conversation_id, user_id, action_type, payload, created_at "
            "FROM turn_actions WHERE id > $1 ORDER BY id LIMIT $2",
            since_id, limit,
        )
        return [dict(row) for row in rows]


async def get_turns(conversation_id: int) -> list[dict[str, Any]]:
    """All turns for a conversation, oldest first — the raw material for
    summarize_and_embed_conversation()."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_prompt, result, created_at FROM conversation_turns "
            "WHERE conversation_id = $1 ORDER BY id",
            conversation_id,
        )
        return [dict(row) for row in rows]


async def get_older_turns(
        conversation_id: int, skip_recent: int, limit: int = 10,
) -> list[dict[str, Any]]:
    """Turns from this conversation older than the skip_recent most recent
    ones (the caller's own live conversation_history window) --
    recall_conversation_history's backing query, for pulling back turns
    that fell out of the live window rather than keeping them all in
    context by default. Returns at most limit turns, oldest-first within
    the page (so reading top to bottom is chronological)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT turn_id, user_prompt, result->>'output' AS output, created_at "
            "FROM conversation_turns WHERE conversation_id = $1 "
            "ORDER BY id DESC OFFSET $2 LIMIT $3",
            conversation_id, skip_recent, limit,
        )
        return [dict(row) for row in reversed(rows)]


async def get_turn_actions_for_turn(turn_id: UUID, user_id: str) -> list[dict[str, Any]]:
    """The full action-by-action trace for one specific turn (see
    write_turn_action) -- recall_turn_detail's backing query, for drilling
    into exactly what a tool returned or what the model reasoned at each
    step, once a turn-level recall (get_older_turns) isn't enough detail
    on its own. Filtered by user_id, not just turn_id -- the model
    supplies turn_id itself (picked from a prior recall_conversation_history
    result), so this is the ownership check that stops it from being
    pointed at a turn it doesn't actually own."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action_type, payload, created_at FROM turn_actions "
            "WHERE turn_id = $1 AND user_id = $2 ORDER BY created_at",
            turn_id, user_id,
        )
        return [dict(row) for row in rows]


async def search_conversations(
        user_id: str,
        query: str,
        query_embedding: list[float],
        max_results: int = 5,
) -> list[dict[str, Any]]:
    """Hybrid search over this user's summarized conversations: reciprocal
    rank fusion of cosine distance (query_embedding vs. each
    conversation's summary embedding) and Postgres full-text search
    (websearch_to_tsquery) over the summary (conversations.search_vector,
    a GENERATED STORED column -- see schema.sql). Same technique as
    search_documents. Conversations with no summary/embedding yet are
    excluded -- this only ever covers already-archived conversations; see
    search_turns for one that also covers the currently open one."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _rrf_cte("conversations", "embedding", "search_vector", "user_id = $2")
            + "SELECT c.id, c.summary, c.file_path, c.created_at, f.score "
              "FROM fused f "
              "JOIN conversations c ON c.id = f.id "
              "ORDER BY f.score DESC "
              "LIMIT $4",
            query_embedding, user_id, query, max_results, _CANDIDATE_POOL, _RRF_K,
        )
        return [dict(row) for row in rows]


async def search_turns(
        user_id: str,
        query: str,
        query_embedding: list[float],
        max_results: int = 5,
) -> list[dict[str, Any]]:
    """Fine-grained recall across individual turns (both what the user
    said and what the model answered), across every conversation --
    archived or still open, unlike search_conversations, since these
    embeddings are written at persist time, not archive time.

    Prompt and output are treated as two SEPARATE entities, each with its
    own ordinary 2-way RRF fusion (vector + text, identical technique to
    search_documents) -- not one 4-way fusion summing all of prompt-
    vector, prompt-text, output-vector, and output-text together. Summing
    was tried first and is wrong: it systematically punishes a turn that
    matches strongly on just ONE side in favor of turns that match only
    mediocre-ly on BOTH -- confirmed directly on real data (a turn whose
    prompt was an excellent match, rank 2 of 39, but whose output was an
    unrelated error message, lost to turns ranked 7th-17th on each side
    individually, purely because they had something, however weak, on
    both sides to add together). Taking the MAX of the two sides' scores
    instead means a turn only needs to be genuinely well-matched on
    EITHER side to surface, which is the actual intent: the user's words
    and the model's answer are two different things a query might be
    looking for, not fragments of one combined signal.

    matched_side in the return ('prompt' or 'output') is which side
    actually won, in case that's useful context for the caller. Turns
    with no embeddings yet (save_turn was called without them, or
    pre-dates the backfill -- see scripts/backfill_turn_embeddings.py)
    simply can't surface; they're not excluded by a WHERE clause, they
    just never win a vector/text ranking on either side."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "WITH prompt_vec AS ("
            "    SELECT id, ROW_NUMBER() OVER (ORDER BY prompt_embedding <=> $1) AS rank "
            "    FROM conversation_turns "
            "    WHERE user_id = $2 AND prompt_embedding IS NOT NULL "
            "    ORDER BY prompt_embedding <=> $1 LIMIT $5"
            "), prompt_txt AS ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "        ORDER BY ts_rank(prompt_search_vector, websearch_to_tsquery('english', $3)) DESC"
            "    ) AS rank "
            "    FROM conversation_turns "
            "    WHERE user_id = $2 AND prompt_search_vector @@ websearch_to_tsquery('english', $3) "
            "    ORDER BY ts_rank(prompt_search_vector, websearch_to_tsquery('english', $3)) DESC LIMIT $5"
            "), prompt_fused AS ("
            "    SELECT COALESCE(pv.id, pt.id) AS id, "
            "           (COALESCE(1.0 / ($6 + pv.rank), 0) + COALESCE(1.0 / ($6 + pt.rank), 0))::float8 AS score "
            "    FROM prompt_vec pv FULL OUTER JOIN prompt_txt pt ON pv.id = pt.id"
            "), output_vec AS ("
            "    SELECT id, ROW_NUMBER() OVER (ORDER BY output_embedding <=> $1) AS rank "
            "    FROM conversation_turns "
            "    WHERE user_id = $2 AND output_embedding IS NOT NULL "
            "    ORDER BY output_embedding <=> $1 LIMIT $5"
            "), output_txt AS ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "        ORDER BY ts_rank(output_search_vector, websearch_to_tsquery('english', $3)) DESC"
            "    ) AS rank "
            "    FROM conversation_turns "
            "    WHERE user_id = $2 AND output_search_vector @@ websearch_to_tsquery('english', $3) "
            "    ORDER BY ts_rank(output_search_vector, websearch_to_tsquery('english', $3)) DESC LIMIT $5"
            "), output_fused AS ("
            "    SELECT COALESCE(ov.id, ot.id) AS id, "
            "           (COALESCE(1.0 / ($6 + ov.rank), 0) + COALESCE(1.0 / ($6 + ot.rank), 0))::float8 AS score "
            "    FROM output_vec ov FULL OUTER JOIN output_txt ot ON ov.id = ot.id"
            "), combined AS ("
            "    SELECT COALESCE(pf.id, of.id) AS id, "
            "           GREATEST(COALESCE(pf.score, 0), COALESCE(of.score, 0)) AS score, "
            "           CASE WHEN COALESCE(pf.score, 0) >= COALESCE(of.score, 0) "
            "                THEN 'prompt' ELSE 'output' END AS matched_side "
            "    FROM prompt_fused pf FULL OUTER JOIN output_fused of ON pf.id = of.id"
            ") "
            "SELECT t.turn_id, t.conversation_id, t.user_prompt, t.result->>'output' AS output, "
            "       t.created_at, c.score, c.matched_side "
            "FROM combined c "
            "JOIN conversation_turns t ON t.id = c.id "
            "ORDER BY c.score DESC "
            "LIMIT $4",
            query_embedding, user_id, query, max_results, _CANDIDATE_POOL, _RRF_K,
        )
        return [dict(row) for row in rows]


async def update_conversation_summary(conversation_id: int, summary: str, embedding: list[float]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE conversations SET summary = $1, embedding = $2, updated_at = now() WHERE id = $3",
            summary, embedding, conversation_id,
        )


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


async def run_readonly_query(user_id: str, sql: str, limit: int = 50) -> list[dict[str, Any]]:
    """Runs sql (already string-validated by the caller -- see
    tooling.database_tools.query_database) as the alira_agent_readonly
    role, scoped to user_id via a transaction-local session variable that
    the agent_readonly.my_* views (see schema.sql) key off of.

    Layered safety, in order of how much they're actually trusted:
      1. The role's own grants -- SELECT on the agent_readonly views only,
         nothing else, not even the underlying tables -- are what actually
         make a write or a cross-user read impossible. Everything else
         here is defense-in-depth on top of that, not the real boundary.
      2. The transaction is explicitly rolled back in a finally, always,
         regardless of success or failure -- this path should never
         persist anything, so there's no reason to ever commit it.
      3. A 5s statement_timeout bounds any runaway query (e.g. a pathological
         join or a recursive CTE) even though it can't write.
      4. sql is wrapped in a subquery on its own lines (`SELECT * FROM (\\n
         {sql}\\n) ... LIMIT {limit}`) so the row cap always applies
         regardless of what the query itself requested, and a trailing
         `--` comment in sql can only ever consume up to that inserted
         newline, not the wrapper's own closing parenthesis after it.
    """
    pool = await get_pool()
    wrapped_sql = f"SELECT * FROM (\n{sql}\n) AS _query_database_query LIMIT {int(limit)}"
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            await conn.execute("SET LOCAL ROLE alira_agent_readonly")
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", user_id)
            rows = await conn.fetch(wrapped_sql)
            return [dict(row) for row in rows]
        finally:
            await tr.rollback()


async def save_tool_result_cache(
        tool_name: str, user_id: str, query: str, embedding: list[float], result: Any,
) -> None:
    """Persists one cacheable tool call's result, keyed by tool_name + an
    embedding of its query -- see schema.sql's comment on
    tool_result_cache. Best-effort: swallows its own failures, since a
    cache write going wrong should never break the call that's actually
    answering the user right now."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tool_result_cache (tool_name, user_id, query, embedding, result) "
                "VALUES ($1, $2, $3, $4, $5)",
                tool_name, user_id, query, embedding, result,
            )
    except Exception as e:
        log.warning(f"Couldn't write tool_result_cache row (tool={tool_name!r}): {e}")


async def search_tool_result_cache(
        tool_name: str, query: str, query_embedding: list[float], max_results: int = 5,
) -> list[dict[str, Any]]:
    """Hybrid search over past results for one specific cacheable tool
    (any user -- see schema.sql's comment on tool_result_cache):
    reciprocal rank fusion of cosine distance (query_embedding vs. each
    cached call's own query embedding) and Postgres full-text search over
    that same query text (tool_result_cache.search_vector, a GENERATED
    STORED column). Same technique as search_documents.

    Returns both `score` (the fused rank, for ordering) and `relevance`
    (plain cosine similarity, 1 - distance, recomputed per row regardless
    of which method(s) actually surfaced it) -- callers that need an
    absolute "is this actually relevant" cutoff (tooling/cache.py's
    _MIN_RELEVANCE) should filter on relevance, not score: RRF scores
    live on a completely different, pool-size-dependent scale (max ~0.03
    here) and were never meant to be read as a 0-1 similarity."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _rrf_cte("tool_result_cache", "embedding", "search_vector", "tool_name = $2")
            + "SELECT c.query, c.result, c.created_at, f.score, (1 - (c.embedding <=> $1))::float8 AS relevance "
              "FROM fused f "
              "JOIN tool_result_cache c ON c.id = f.id "
              "ORDER BY f.score DESC "
              "LIMIT $4",
            query_embedding, tool_name, query, max_results, _CANDIDATE_POOL, _RRF_K,
        )
        return [dict(row) for row in rows]


async def create_hook(
        user_id: str, chat_id: str, payload: dict[str, Any],
        next_trigger_at, recurrence_seconds: Optional[int] = None, kind: str = "reminder",
) -> int:
    """Creates one hooks row (see schema.sql's comment) and returns its
    id. Called by tooling/scheduling_tools.py's schedule_reminder and
    tooling/gmail_tools.py's schedule_gmail_check, which then re-render
    the user's plan markdown from get_active_hooks."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO hooks "
            "(user_id, chat_id, kind, payload, next_trigger_at, recurrence_seconds) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            user_id, chat_id, kind, payload, next_trigger_at, recurrence_seconds,
        )
        return row["id"]


async def get_active_hooks(user_id: str, kind: Optional[str] = None) -> list[dict[str, Any]]:
    """Every active hooks row for user_id, soonest-first, optionally
    narrowed to one kind -- backs list_scheduled_reminders/
    list_gmail_hooks and plan markdown rendering (memory/plans.py, which
    always passes kind="reminder": a Gmail poll hook has no business in
    the reminders-and-habits plan text)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if kind is not None:
            rows = await conn.fetch(
                "SELECT id, kind, payload, next_trigger_at, recurrence_seconds, created_at "
                "FROM hooks WHERE user_id = $1 AND kind = $2 AND active "
                "ORDER BY next_trigger_at",
                user_id, kind,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, kind, payload, next_trigger_at, recurrence_seconds, created_at "
                "FROM hooks WHERE user_id = $1 AND active "
                "ORDER BY next_trigger_at",
                user_id,
            )
        return [dict(row) for row in rows]


async def get_due_hooks(now) -> list[dict[str, Any]]:
    """Every active, due-or-overdue hooks row, across all users and every
    kind -- the hook ticker's (webhook_server.py) own query, run once per
    tick; it dispatches each row by its own kind."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, user_id, chat_id, kind, payload, next_trigger_at, recurrence_seconds "
            "FROM hooks WHERE active AND next_trigger_at <= $1 "
            "ORDER BY next_trigger_at",
            now,
        )
        return [dict(row) for row in rows]


async def deactivate_hook(id: int, user_id: Optional[str] = None, kind: Optional[str] = None) -> bool:
    """Cancels one hooks row (sets active = false). If user_id is given,
    only deactivates a row actually owned by that user -- cancel_reminder/
    cancel_gmail_hook's own ownership check, since the model supplies id
    directly. kind, if given, additionally requires the row actually be
    that kind -- cancel_reminder passes kind="reminder" so it can't
    accidentally deactivate a gmail_poll hook (or vice versa) just because
    the model reused an id it saw in a different tool's output this same
    conversation. Returns whether a row was actually deactivated just now
    -- the WHERE clause requires `active` already true, not just a
    matching id/user_id/kind, so cancelling an already-cancelled id
    correctly reports false (UPDATE's row count reflects rows MATCHED,
    not rows actually changed -- re-deactivating an already-inactive row
    would otherwise still read as a match)."""
    pool = await get_pool()
    conditions = ["id = $1", "active"]
    params: list[Any] = [id]
    if user_id is not None:
        params.append(user_id)
        conditions.append(f"user_id = ${len(params)}")
    if kind is not None:
        params.append(kind)
        conditions.append(f"kind = ${len(params)}")
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"UPDATE hooks SET active = false WHERE {' AND '.join(conditions)}", *params,
        )
        return result != "UPDATE 0"


async def mark_hook_fired(
        id: int, next_trigger_at, *, deactivate: bool = False,
) -> None:
    """Updates one hooks row after the ticker fires it -- either advances
    next_trigger_at (a recurring hook) or deactivates it (a one-shot, or
    a recurring one past some future end condition). last_fired_at is set
    either way, for visibility."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if deactivate:
            await conn.execute(
                "UPDATE hooks SET active = false, last_fired_at = now() WHERE id = $1",
                id,
            )
        else:
            await conn.execute(
                "UPDATE hooks SET next_trigger_at = $1, last_fired_at = now() WHERE id = $2",
                next_trigger_at, id,
            )


async def update_hook_payload(id: int, payload: dict[str, Any]) -> None:
    """Overwrites one hooks row's payload in place -- unlike a reminder's
    payload (set once at creation, never mutated), a gmail_poll hook must
    persist its last_seen_internal_date dedup watermark after every tick
    it finds new mail, or it would re-notify about the same messages
    forever."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hooks SET payload = $1 WHERE id = $2", payload, id,
        )


async def get_oauth_credentials(user_id: str, service: str) -> Optional[dict[str, Any]]:
    """This user's stored OAuth credentials for one external service
    ('google' -- Gmail and Calendar share one token, granted in a single
    consent flow), or None if they've never run the matching setup script
    (see schema.sql's comment on oauth_credentials)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT access_token, refresh_token, expires_at, scopes "
            "FROM oauth_credentials WHERE user_id = $1 AND service = $2",
            user_id, service,
        )
        return dict(row) if row else None


async def upsert_oauth_credentials(
        user_id: str, service: str, access_token: Optional[str],
        refresh_token: str, expires_at, scopes: list[str],
) -> None:
    """Writes this user's OAuth credentials for one service, replacing
    any existing row -- called once by a service's setup script (e.g.
    scripts/setup_google_oauth.py) after the initial consent flow."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_credentials "
            "(user_id, service, access_token, refresh_token, expires_at, scopes) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (user_id, service) DO UPDATE SET "
            "    access_token = EXCLUDED.access_token, refresh_token = EXCLUDED.refresh_token, "
            "    expires_at = EXCLUDED.expires_at, scopes = EXCLUDED.scopes, updated_at = now()",
            user_id, service, access_token, refresh_token, expires_at, scopes,
        )


async def update_oauth_access_token(user_id: str, service: str, access_token: str, expires_at) -> None:
    """Persists a refreshed access_token (and its new expiry) after
    gmail_client transparently refreshes an expired one -- leaves
    refresh_token/scopes untouched."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE oauth_credentials SET access_token = $1, expires_at = $2, updated_at = now() "
            "WHERE user_id = $3 AND service = $4",
            access_token, expires_at, user_id, service,
        )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
