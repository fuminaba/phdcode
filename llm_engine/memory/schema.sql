CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS conversations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id TEXT NOT NULL,           -- person identifier (Telegram user id today; interface-agnostic once real auth exists)
    chat_id TEXT NOT NULL,           -- interface thread identifier, e.g. Telegram chat id
    file_path TEXT,                  -- markdown transcript on disk (source of truth); set once the transcript is created, after this row exists
    summary TEXT,
    embedding vector({embedding_dim}),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration for installs created before user_id / nullable file_path existed.
-- No-ops on a fresh table (columns already correct, nothing to backfill).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS user_id TEXT;
UPDATE conversations SET user_id = chat_id WHERE user_id IS NULL;
ALTER TABLE conversations ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE conversations ALTER COLUMN file_path DROP NOT NULL;

CREATE INDEX IF NOT EXISTS conversations_user_id_idx ON conversations (user_id);
CREATE INDEX IF NOT EXISTS conversations_chat_id_idx ON conversations (chat_id);

CREATE INDEX IF NOT EXISTS conversations_embedding_idx
    ON conversations USING hnsw (embedding vector_cosine_ops);

-- Lexical half of hybrid search (search_memory combines this with vector
-- similarity via reciprocal rank fusion, same technique search_documents
-- already uses -- see memory/db.py's search_conversations).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(summary, ''))) STORED;
CREATE INDEX IF NOT EXISTS conversations_search_vector_idx
    ON conversations USING GIN (search_vector);

-- JSONB backup of every turn (mirrors the markdown transcript: prompt,
-- output, tool-calling trace, evaluation) so it's queryable from SQL even
-- without reading the file.
CREATE TABLE IF NOT EXISTS conversation_turns (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,           -- denormalized copy of conversations.user_id, for per-user turn queries without a join
    user_prompt TEXT NOT NULL,
    result JSONB NOT NULL,
    model_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration for installs created before user_id existed on this table.
ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS user_id TEXT;
UPDATE conversation_turns t SET user_id = c.user_id
    FROM conversations c WHERE t.conversation_id = c.id AND t.user_id IS NULL;
ALTER TABLE conversation_turns ALTER COLUMN user_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS conversation_turns_conversation_id_idx
    ON conversation_turns (conversation_id);
CREATE INDEX IF NOT EXISTS conversation_turns_user_id_idx
    ON conversation_turns (user_id);

-- Groups conversation_turns.result with the live, granular action log
-- below (turn_actions) -- minted once per turn (a plain uuid4, not tied to
-- this row's own id, since this row still isn't written until the turn
-- completes; see turn_actions' own comment for why).
ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS turn_id UUID;
CREATE INDEX IF NOT EXISTS conversation_turns_turn_id_idx
    ON conversation_turns (turn_id);

-- Per-turn embeddings (prompt and output kept separate, not one combined
-- embedding) plus matching full-text columns -- fine-grained recall
-- across BOTH what the user said and what the model answered, the
-- search_documents/search_document_chunks two-tier pattern applied to
-- conversations/turns instead of documents/chunks (search_memory stays
-- the coarse "which past conversation" layer; search_turns, tooling/
-- memory_tools.py, is the fine-grained one). Populated at persist time
-- (memory/persist.py), not archive time -- unlike search_memory (which
-- only ever covers conversations with summary/embedding set, i.e.
-- already-archived ones), this means even the CURRENTLY OPEN
-- conversation's turns are searchable immediately. output_embedding/
-- output_search_vector are read from result->>'output' at write time in
-- Python (persist_turn), not as a GENERATED column here, since output can
-- be non-string (as_json=True responses) and embedding requires an
-- actual API call a GENERATED column can't make.
ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS prompt_embedding vector({embedding_dim});
ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS output_embedding vector({embedding_dim});
ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS prompt_search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(user_prompt, ''))) STORED;
ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS output_search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(result->>'output', ''))) STORED;

CREATE INDEX IF NOT EXISTS conversation_turns_prompt_embedding_idx
    ON conversation_turns USING hnsw (prompt_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS conversation_turns_output_embedding_idx
    ON conversation_turns USING hnsw (output_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS conversation_turns_prompt_search_vector_idx
    ON conversation_turns USING GIN (prompt_search_vector);
CREATE INDEX IF NOT EXISTS conversation_turns_output_search_vector_idx
    ON conversation_turns USING GIN (output_search_vector);

-- Live, granular, append-only log of what the agent is doing *during* a
-- turn or a multi-stage pipeline (document ingestion today; any future
-- multi-stage tool later) -- as opposed to conversation_turns, which is
-- the complete-turn summary written once, at the end. turn_id is minted
-- at the start of a turn (before conversation_turns' own row exists) and
-- carried through every action in it, including any pipeline nested
-- inside it, so `WHERE turn_id = $1 ORDER BY created_at` reconstructs the
-- whole story in order. action_type is one of: turn_start, llm_call,
-- tool_call, pipeline_start, pipeline_step, pipeline_end, turn_end -- the
-- two *_end types carry a status ('success' or 'error') in their payload,
-- folding error reporting into the same event rather than a separate type.
CREATE TABLE IF NOT EXISTS turn_actions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    turn_id UUID NOT NULL,
    conversation_id BIGINT REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{{}}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS turn_actions_turn_id_idx ON turn_actions (turn_id);
CREATE INDEX IF NOT EXISTS turn_actions_conversation_id_idx ON turn_actions (conversation_id);
CREATE INDEX IF NOT EXISTS turn_actions_user_id_idx ON turn_actions (user_id);

-- Proactive triggers: one row per habit/reminder/poll the user (or the
-- agent, on the user's behalf) has set up, independent of any incoming
-- message -- the foundation for the agent initiating contact, or taking
-- some other action, instead of only responding. kind dispatches to a
-- handler in llm_engine/hooks/ (see that package's registry.py) -- adding
-- a new kind is a new handler module plus widening the CHECK constraint
-- below, not a schema change. payload is kind-specific: for 'reminder' it
-- holds an INTENT ("remind me to take my vitamins"), not verbatim text to
-- send -- what actually gets sent is composed by a real model call at
-- fire time (llm_engine.hooks.reminder / pipeline.handle_reminder_hook),
-- scoped to just the plan markdown + this intent + tools, never the full
-- conversation history (same discipline as the tool-loop's digest call).
-- 'gmail_poll' payload instead holds query/on_match/
-- last_seen_internal_date fields -- see llm_engine/hooks/gmail.py.
-- recurrence_seconds NULL means fire once then deactivate; otherwise
-- next_trigger_at advances by that many seconds after each firing. This
-- table used to be named scheduled_messages, back when firing one could
-- only ever mean "compose and send a message" -- renamed once that
-- stopped being true.
DO $$
BEGIN
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'scheduled_messages')
       AND NOT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'hooks') THEN
        ALTER TABLE scheduled_messages RENAME TO hooks;
        ALTER INDEX scheduled_messages_due_idx RENAME TO hooks_due_idx;
        ALTER INDEX scheduled_messages_user_id_idx RENAME TO hooks_user_id_idx;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS hooks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'reminder',
    payload JSONB NOT NULL DEFAULT '{{}}',
    next_trigger_at TIMESTAMPTZ NOT NULL,
    recurrence_seconds BIGINT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_fired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS hooks_due_idx
    ON hooks (next_trigger_at) WHERE active;
CREATE INDEX IF NOT EXISTS hooks_user_id_idx
    ON hooks (user_id) WHERE active;
CREATE INDEX IF NOT EXISTS hooks_user_kind_idx
    ON hooks (user_id, kind) WHERE active;

-- Rebuilt unconditionally (not the one-time-only guard documents'
-- document_kind check uses) since kind is specifically meant to grow as
-- new hook types are added -- a one-time guard would never pick up a
-- widened list on a later schema.sql edit. Cheap: init_schema() already
-- reruns this whole file on every startup regardless.
ALTER TABLE hooks DROP CONSTRAINT IF EXISTS hooks_kind_check;
ALTER TABLE hooks ADD CONSTRAINT hooks_kind_check CHECK (kind IN ('reminder', 'gmail_poll'));

-- Per-user OAuth credentials for external service connectors. Gmail and
-- Calendar share ONE row (service = 'google'): Google grants scopes
-- requested together in a single consent flow onto a single token, so
-- these are one credential, not two. Another provider later would be a
-- new `service` value, same table.
-- Plaintext, matching this project's existing single-tenant, locally
-- trusted secret handling (e.g. TAVILY_TOKEN in .env) -- not a regression
-- from it. Populated by scripts/setup_google_oauth.py, a one-time local
-- flow (this server is headless/Tailscale-only, so the OAuth consent
-- redirect can't happen on the server itself); refreshed in place by
-- llm_engine.tooling._tooling_deps.google_auth whenever access_token is
-- expired or about to be.
CREATE TABLE IF NOT EXISTS oauth_credentials (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id TEXT NOT NULL,
    service TEXT NOT NULL,
    access_token TEXT,
    refresh_token TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    scopes TEXT[] NOT NULL DEFAULT '{{}}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS oauth_credentials_user_service_idx
    ON oauth_credentials (user_id, service);

-- Generic cache of one cacheable tool's results, keyed by tool_name +
-- an embedding of the query that produced them -- lets
-- recall_tool_results (tooling/cache_tools.py) find a close-enough past
-- call instead of re-running an expensive external lookup (web_search
-- today; search_arxiv/search_pubmed are natural future additions -- see
-- tooling/cache.py). One shared table across every cacheable tool rather
-- than one table per tool: adding a new cacheable tool later needs no
-- schema change, just another tool_name value. Global, not scoped to a
-- single user: results from an external lookup (a web page, a paper)
-- have no ownership to enforce, and reuse across users is exactly the
-- point -- user_id here is provenance (who triggered it), not a lookup
-- filter. Freshness is left to the model's own judgement rather than a
-- hard TTL: every recall_tool_results match carries its own age, and how
-- stale is too stale varies by tool (web_search goes stale in hours; a
-- literature search result basically never does), which only the model
-- calling it actually knows.
CREATE TABLE IF NOT EXISTS tool_result_cache (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tool_name TEXT NOT NULL,
    user_id TEXT NOT NULL,
    query TEXT NOT NULL,
    embedding vector({embedding_dim}) NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tool_result_cache_tool_name_idx
    ON tool_result_cache (tool_name);
CREATE INDEX IF NOT EXISTS tool_result_cache_embedding_idx
    ON tool_result_cache USING hnsw (embedding vector_cosine_ops);

-- Lexical half of hybrid search (recall_tool_results combines this with
-- vector similarity via reciprocal rank fusion, same technique
-- search_documents already uses -- see memory/db.py's
-- search_tool_result_cache).
ALTER TABLE tool_result_cache ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', query)) STORED;
CREATE INDEX IF NOT EXISTS tool_result_cache_search_vector_idx
    ON tool_result_cache USING GIN (search_vector);

-- One row per uploaded document. dir_path/text_path start NULL: the
-- per-upload folder embeds this row's own id, so it can only be created
-- (and this row updated) after the row exists -- same two-step pattern as
-- conversations.file_path. Two summary angles beyond the general one:
-- methods and results. (A third, "conceptual", was tried and folded back
-- into the general summary -- see the migration below.)
CREATE TABLE IF NOT EXISTS documents (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id BIGINT REFERENCES conversations(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    dir_path TEXT,                   -- per-upload folder: logs/uploads/<user_id>/<conversation_id>/<id>/
    text_path TEXT,                  -- extracted markdown file within dir_path
    title TEXT,
    authors TEXT[] NOT NULL DEFAULT '{{}}',
    summary TEXT,
    embedding vector({embedding_dim}),
    methods_summary TEXT,
    methods_embedding vector({embedding_dim}),
    results_summary TEXT,
    results_embedding vector({embedding_dim}),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration for installs created before the RAG-oriented metadata/summary
-- flavor columns existed. No-ops on a fresh table.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS authors TEXT[] NOT NULL DEFAULT '{{}}';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS methods_summary TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS methods_embedding vector({embedding_dim});
ALTER TABLE documents ADD COLUMN IF NOT EXISTS results_summary TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS results_embedding vector({embedding_dim});

-- Migration: the "conceptual" summary flavor was merged back into the
-- general summary (document_summary_prompt now covers conceptual framing
-- too) -- drops the columns (and their HNSW index, automatically) on any
-- install that has them. No-op if they were never created.
ALTER TABLE documents DROP COLUMN IF EXISTS conceptual_summary;
ALTER TABLE documents DROP COLUMN IF EXISTS conceptual_embedding;

-- Migration: distinguishes a real uploaded-and-fully-ingested document
-- ('upload' -- has dir_path/text_path/chunks, full RAG treatment) from a
-- lightweight literature-search bookmark ('bookmark' -- save_finding saves
-- title/authors/abstract-as-summary only, no local file, no chunks --
-- read_document/search_document_chunks won't work on one of these).
-- Default 'upload' so every pre-existing row (all of them uploads, since
-- save_finding didn't exist before this) classifies correctly with no
-- backfill needed. source_url/external_ref/doi are bookmark provenance,
-- NULL for an upload. external_ref ("arxiv:2401.12345v2" / "pubmed:
-- 33649564") is a stable dedup key -- saving the same paper twice updates
-- the existing bookmark instead of creating a duplicate (see the partial
-- unique index below).
ALTER TABLE documents ADD COLUMN IF NOT EXISTS document_kind TEXT NOT NULL DEFAULT 'upload';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS external_ref TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS doi TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'documents_document_kind_check'
    ) THEN
        ALTER TABLE documents ADD CONSTRAINT documents_document_kind_check
            CHECK (document_kind IN ('upload', 'bookmark'));
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS documents_user_external_ref_idx
    ON documents (user_id, external_ref) WHERE external_ref IS NOT NULL;

-- Lexical half of hybrid search (search_documents combines this with
-- vector similarity via reciprocal rank fusion -- see memory/db.py).
-- GENERATED ... STORED: Postgres recomputes and persists this automatically
-- on every INSERT/UPDATE to the source columns, including the incremental
-- UPDATEs ingest_and_index_document makes as each summary flavor lands, so
-- there's no manual trigger/backfill step to maintain.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title, '') || ' ' ||
            coalesce(summary, '') || ' ' ||
            coalesce(methods_summary, '') || ' ' ||
            coalesce(results_summary, '')
        )
    ) STORED;

CREATE INDEX IF NOT EXISTS documents_user_id_idx ON documents (user_id);
CREATE INDEX IF NOT EXISTS documents_conversation_id_idx ON documents (conversation_id);

CREATE INDEX IF NOT EXISTS documents_embedding_idx
    ON documents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS documents_methods_embedding_idx
    ON documents USING hnsw (methods_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS documents_results_embedding_idx
    ON documents USING hnsw (results_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS documents_search_vector_idx
    ON documents USING GIN (search_vector);

-- Keywords, normalized and deduped across all documents (mirrors the
-- reference project's Keyword model; skips its UserKeyword per-user-search
-- tracking, a different feature this project hasn't asked for).
CREATE TABLE IF NOT EXISTS keywords (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    word TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS document_keywords (
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    keyword_id BIGINT NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, keyword_id)
);

CREATE INDEX IF NOT EXISTS document_keywords_keyword_id_idx
    ON document_keywords (keyword_id);

-- Chunked text + per-chunk embedding for fine-grained retrieval within a
-- document, once search_documents (whole-document level) has found it.
-- start_line/end_line (1-indexed, inclusive) locate the chunk within the
-- extracted markdown file, so a future reader can jump straight to
-- read_document(text_path, start_line=..., end_line=...) for full context.
CREATE TABLE IF NOT EXISTS document_chunks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    start_line INT NOT NULL,
    end_line INT NOT NULL,
    embedding vector({embedding_dim}) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration for installs created before chunks tracked their source line
-- range. Existing rows (if any) get a harmless 0/0 placeholder -- their
-- content is unaffected, only the "jump to this range" convenience is
-- unavailable for chunks created before this column existed.
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS start_line INT NOT NULL DEFAULT 0;
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS end_line INT NOT NULL DEFAULT 0;

-- Lexical half of hybrid search (search_document_chunks combines this with
-- vector similarity via reciprocal rank fusion -- see memory/db.py).
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS document_chunks_document_id_idx
    ON document_chunks (document_id);
CREATE INDEX IF NOT EXISTS document_chunks_embedding_idx
    ON document_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS document_chunks_search_vector_idx
    ON document_chunks USING GIN (search_vector);

-- Read-only role + user-scoped views backing the query_database tool
-- (llm_engine.tooling.database_tools). NOLOGIN: only ever assumed via
-- SET LOCAL ROLE within a transaction that's always rolled back regardless
-- of outcome (see memory.db.run_readonly_query) -- never connected to
-- directly, and the role's grants (SELECT on these views only, nothing
-- else, not even the underlying tables) are what actually make writes and
-- cross-user reads impossible, not just the caller's own query validation.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'alira_agent_readonly') THEN
        CREATE ROLE alira_agent_readonly NOLOGIN;
    END IF;
END
$$;

GRANT alira_agent_readonly TO CURRENT_USER;

CREATE SCHEMA IF NOT EXISTS agent_readonly AUTHORIZATION CURRENT_USER;
GRANT USAGE ON SCHEMA agent_readonly TO alira_agent_readonly;

-- Each view filters on current_setting('app.current_user_id', true) --
-- set via SET LOCAL / set_config(..., true) for the duration of one
-- transaction by run_readonly_query. The `true` ("missing_ok") second
-- argument means an unset session variable yields NULL, and
-- `user_id = NULL` is never true -- fails closed (zero rows) rather than
-- erroring, if that setup step is ever skipped.
CREATE OR REPLACE VIEW agent_readonly.my_conversations AS
    SELECT id, user_id, chat_id, summary, created_at, updated_at
    FROM conversations
    WHERE user_id = current_setting('app.current_user_id', true);

CREATE OR REPLACE VIEW agent_readonly.my_conversation_turns AS
    SELECT id, conversation_id, user_id, user_prompt, result, model_name, created_at
    FROM conversation_turns
    WHERE user_id = current_setting('app.current_user_id', true);

CREATE OR REPLACE VIEW agent_readonly.my_documents AS
    SELECT id, user_id, conversation_id, filename, title, authors, summary,
           methods_summary, results_summary, created_at, updated_at
    FROM documents
    WHERE user_id = current_setting('app.current_user_id', true);

CREATE OR REPLACE VIEW agent_readonly.my_document_chunks AS
    SELECT dc.id, dc.document_id, dc.chunk_index, dc.content, dc.start_line, dc.end_line, dc.created_at
    FROM document_chunks dc
    JOIN documents d ON d.id = dc.document_id
    WHERE d.user_id = current_setting('app.current_user_id', true);

CREATE OR REPLACE VIEW agent_readonly.my_document_keywords AS
    SELECT dk.document_id, k.word
    FROM document_keywords dk
    JOIN keywords k ON k.id = dk.keyword_id
    JOIN documents d ON d.id = dk.document_id
    WHERE d.user_id = current_setting('app.current_user_id', true);

GRANT SELECT ON
    agent_readonly.my_conversations,
    agent_readonly.my_conversation_turns,
    agent_readonly.my_documents,
    agent_readonly.my_document_chunks,
    agent_readonly.my_document_keywords
    TO alira_agent_readonly;
