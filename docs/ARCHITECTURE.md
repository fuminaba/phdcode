# ALiRA2 Architecture

A local-first, tool-calling LLM agent (served by Ollama) exposed through a Telegram bot, with a
transparent, human-readable and SQL-queryable memory system, document (PDF) ingestion via
`marker-pdf`, and external connectors (web search, arXiv/PubMed, Gmail, Google Calendar).

Two things distinguish it from a plain chatbot wrapper:

- **Every GPU-touching call is serialized through a single Celery queue** (`concurrency=1`), so
  chat generation and document extraction can never race for the same VRAM — see "Celery/Redis"
  below.
- **The agent can act without being spoken to.** The `hooks` table plus `webhook_server.py`'s
  ticker fire scheduled work (reminders, Gmail polling) on their own, independent of any incoming
  message — see "Hooks" below and [HOOKS_PLAN.md](HOOKS_PLAN.md) for that subsystem's full design
  record.

See [SCRIPTING.md](SCRIPTING.md) for a function-by-function reference of every module,
[COMMANDS.md](COMMANDS.md) for the user-facing Telegram surface, and [ROADMAP.md](ROADMAP.md) for
what's built, what's next, and the reasoning/measurements behind each decision.

## Components

```
Telegram client
      |
      v
webhook_server.py  (python-telegram-bot, long-polling) -- a thin channel adapter
      |    extracts chat_id/user_id from Telegram's objects, builds a TelegramFeedback
      |    [llm_engine/frontend/telegram.py], calls into:
      v
llm_engine/pipeline.py  -- channel-agnostic; a future Django-based interface
      |                     would call these same functions, not duplicate them
      |--- handle_incoming_message(user_id, chat_id, text, attachments=None, feedback=None)
      |         |
      |         |--- ingest_and_index_document()   [llm_engine/memory/document_index.py], if attachments given
      |         |         |--- documents.ingest_document()   --> marker-pdf: extracted markdown +
      |         |         |       images + tables, saved under a per-upload folder:
      |         |         |       logs/uploads/{user_id}/{conversation_id}/{document_id}/
      |         |         |--- feedback.progress("Extracted text", 0.35)
      |         |         |--- chunk_text() (sentence-/line-aware, tracks start_line/end_line) +
      |         |         |       embeddings.embed() per chunk --> Postgres: document_chunks
      |         |         |--- feedback.progress("Indexed for search", 0.45)
      |         |         |--- map-reduce, sequentially: digest each larger chunk (feedback.progress
      |         |         |       per chunk, 45-80%), then general summary (covers conceptual/
      |         |         |       theoretical framing too), title/authors/keywords (structured JSON),
      |         |         |       and 2 more summary flavors (methods/results) over the digests --
      |         |         |       each generate_text() + embed() --> Postgres: documents.* /
      |         |         |       keywords / document_keywords (feedback.progress 85-100%)
      |         |         '--- note folded into the effective prompt text, so the reference
      |         |               survives into later turns via history
      |         |
      |         '--- feedback.start("Thinking") / GenerativeService.generate_with_tools()   [llm_engine/tgi.py]
      |                   |
      |                   |--- OpenAI-compatible client --> Ollama (qwen3:14b)
      |                   |         via DEFAULT_SERVER_IP / LLM_ROUTING (.env)
      |                   |
      |                   |--- _run_tool_loop: step budget starts at max_steps, extendable up to
      |                   |       hard_max_steps via continue_research; feedback.progress() once per
      |                   |       step (e.g. "Step 2: read_paper", editing the same Telegram message)
      |                   |
      |                   '--- tool dispatch --> llm_engine/tooling  (TOOLS / TOOL_FUNCTIONS registry)
      |                              |--- web_search()          --> Tavily API
      |                              |--- search_memory() / read_conversation()
      |                              |         --> llm_engine/context.current_user_id  (who's asking)
      |                              |         --> Postgres: conversations (pgvector cosine search, scoped to that user)
      |                              |--- search_documents() / search_document_chunks() / read_document()
      |                              |          --> Postgres: documents / document_chunks (pgvector cosine
      |                              |                search; document_chunks JOINs documents to enforce
      |                              |                ownership even for a guessed document_id)
      |                              |          --> logs/uploads/{user_id}/...  (same ownership-scoped read pattern)
      |                              |--- query_database()   -- real SQL, scoped by a read-only Postgres
      |                              |          role (SET LOCAL ROLE, always rolled back) that can only
      |                              |          SELECT from user-filtered views, nothing else
      |                              |--- search_arxiv() / search_pubmed() --> live APIs, short overview only
      |                              |--- read_paper(src_db, db_id)        --> re-queries for one paper's
      |                              |          full detail (abstract, etc.), only when actually asked for
      |                              |--- save_finding()  --> Postgres: documents (document_kind='bookmark',
      |                              |          no dir_path/text_path -- title+abstract-as-summary only)
      |                              '--- continue_research(reason) -- intercepted by _run_tool_loop itself,
      |                                         extends the step budget instead of being dispatched normally
      |
      |--- reply_text(result["output"])   <-- ONLY the final answer ever reaches the user
      |
      '--- llm_engine/memory.persist_turn()   (awaited after the reply, non-blocking)
                |--- ConversationTranscript   --> logs/conversations/<user_id>/*.md
                '--- save_turn()              --> Postgres: conversation_turns (JSONB)

On /start or /summarize (pipeline.start_new_conversation / summarize_open_conversation):
      '--- summarize_and_embed_conversation()   [llm_engine/memory/summarize.py]
                |--- GenerativeService.generate_text()  --> structured summary (prompts.summarize_prompt)
                |--- embeddings.embed()                 --> Ollama (nomic-embed-text, 768-dim)
                '--- Postgres: conversations.summary / .embedding  (pgvector, HNSW cosine index)
```

## Design principles

**Local-first, swappable backends.** The LLM is served by Ollama over Tailscale
(`100.110.224.47`), accessed through the standard OpenAI Python SDK — same client code would work
against real OpenAI or any OpenAI-compatible endpoint by changing `.env` (`LLM_ROUTING`,
`DEFAULT_SERVER_IP`). The Postgres backend is equally swappable: `docker-compose.yml` +
`POSTGRES_*` only *provision* the container; the app itself only ever reads `DATABASE_URL`. Point
that at a different host and nothing else changes. Document extraction (`marker-pdf`) runs fully
local too, directly on this machine's GPU — no external service, no server process to keep warm
(see "Why `marker-pdf` is pinned to 1.10.2" below for why that specific choice matters here).

**Channel-agnostic core, thin adapters.** `llm_engine/pipeline.py` holds everything about
processing a message that isn't specific to *how* it arrived: conversation lookup/lifecycle,
document ingestion, running the agent, persisting the turn. It takes plain `user_id`/`chat_id`
strings, never Telegram's `Update`/`Context` objects. `webhook_server.py` is a thin adapter over
it — extract Telegram's ids, call the pipeline, send back `result["output"]`. A future Django-based
chat interface (planned) would be another adapter calling the exact same functions, not a
reimplementation.

**Progress/activity feedback is channel-agnostic too, via `llm_engine/frontend/`.** A document
upload takes minutes; Telegram's `send_chat_action` ("typing", "upload_document", ...) only lasts
~5 seconds per call, so a single one-shot call — the original implementation — silently goes stale
almost immediately on anything longer than that. `frontend/base.py`'s `ChannelFeedback` is a
three-method interface (`start(label)` / `progress(label, fraction)` / `finish(label=None)`) that
`memory/document_index.py` and `pipeline.py` call at each stage boundary, knowing nothing about
*how* it's surfaced — every method is a no-op on the base class itself, so call sites always have a
`feedback` object (`feedback = feedback or ChannelFeedback()`) and never need `if feedback:` checks.
`frontend/telegram.py`'s `TelegramFeedback` is the only real implementation today: `start()` sends
one message and spins up a background task that re-sends the chat action every ~4 seconds (cancelled
in `finish()`) so the indicator never goes stale regardless of how long the operation runs;
`progress()` edits that same message with a percentage (`ingest_and_index_document` reports 8
stages: extraction 35%, RAG indexing 45%, one increment per map-reduce chunk digest up to 80%, then
the 4 reduce/metadata calls to 100%); `finish()` either edits the message to a final state or
deletes it, caller's choice. A future Django front end would implement `ChannelFeedback` however
suits it (a websocket push, a polled status row, ...) — `document_index.py`/`pipeline.py` would need
zero changes. Deliberately *not* re-exported from `frontend/__init__.py` — only `ChannelFeedback` is
(`from .base import ChannelFeedback`, not `.telegram`) — since a package `__init__.py` always runs on
any submodule import; re-exporting `TelegramFeedback` there would force `python-telegram-bot` to
import as a side effect of anything importing `llm_engine.frontend` at all, including the
channel-agnostic modules that only need the abstract base. Verified end-to-end against the real test
PDF with a mocked bot: 14-step progress trail from `⏳ Processing test.pdf` through 8 map-chunk
digest updates to `✅ Finished processing test.pdf`, then a separate `⏳ Thinking` message
(deleted, not edited, once the agent's real reply is ready) for the `generate_with_tools` phase, 58
chat-action refreshes over the ~230s run confirming the keep-alive loop survived the whole duration.

**Why `marker-pdf` is pinned to `1.10.2`, not the latest `2.0.0`.** `2.0.0`'s layout/OCR model
(`surya-ocr>=0.18`) is a real architecture change, not a rough edge: layout detection became a
prompted, JSON-schema-guided *generation* task on a small VLM, served via a vLLM engine that
`surya` spawns in its own Docker container (confirmed by reading `surya.layout.LayoutPredictor`
and `marker_single`'s own source — the documented CLI goes through the identical code path). That
architecture cold-starts in ~5-6 minutes on every process start unless the vLLM server is kept
running permanently, and keeping it running permanently means it holds a large, fixed slice of
GPU memory in reserve whether or not anything is happening at that moment — at vLLM's default
`gpu_memory_utilization=0.85` that measured ~13GB (81% of this machine's 16GB card); tuned down to
`0.2` it was still ~2.9GB, permanently. That's not just a budget question: with that reservation
warm, a local `gpt-oss:20b` (Ollama) measurably spilled ~23% of itself to CPU and ran at less than
half its normal throughput (162.85 → ~68-73 tok/s, confirmed via `size_vram` vs `size` in Ollama's
`/api/ps`). `1.10.2` / `surya-ocr==0.17.1` predates this shift entirely — plain in-process torch
models, no server, no Docker, ~8-9s per document (including a one-time Triton JIT compile) and
zero VRAM held once the call returns. Given this project's actual usage pattern — a bot process
that also shares its machine with locally-run models sometimes — the older, architecturally
simpler version was strictly better here, not just cheaper to operate.

**Documents get the same two-tier treatment as conversations — and then some, for RAG.**
`documents` (whole-document summary + embedding) sits alongside `document_chunks` (per-chunk text
+ embedding) exactly the way `conversations` sits alongside `conversation_turns` — a coarse
`search_documents` match at the document level, then `search_document_chunks(document_id, query)`
for fine-grained retrieval within one already-found document (a nearest-neighbor search over just
that document's chunks; the `JOIN documents ON ... AND d.user_id = $user` enforces ownership even
for a `document_id` the caller didn't get from their own `search_documents` results — same
defense-in-depth spirit as `read_document`'s path scoping, verified directly: querying as a
different user against a real `document_id` returns `[]`, not another user's chunks). Because the
actual goal is RAG over research articles, `documents` goes further
than conversations do: `title`/`authors` (extracted via a structured `as_json=True` call, the same
mechanism `_judge_output` already uses) and normalized `keywords` (deduped across all documents via
a `keywords` + `document_keywords` join table), plus two summary "flavors" beyond the general
one — `methods_summary`, `results_summary` — each with its *own* embedding column and HNSW index.
(A third flavor, `conceptual_summary`, was tried and then folded back into the general summary —
`document_summary_prompt`'s template now has its own "Core concepts / theoretical framing" section,
so the general summary covers that angle without a separate LLM call, embedding column, or index.)
This mirrors a prior project's `ResearchArticle`/`ArticleChunk` Django
models (`methods`/`methods_embedding`, `results`/`results_embedding`, `abstract`/
`abstract_embedding`, plus a normalized `Keyword` model) fairly closely — the main thing
deliberately left out is that project's `UserKeyword` (per-user keyword-search-usage tracking) and
citation-graph fields (DOIs, `references` self-FK), neither of which this project has needed. Its
`authors` field was already a plain array, not normalized, so `documents.authors` matches that
directly rather than introducing an `Author` table nothing has asked for.

**`search_documents`/`search_document_chunks` are hybrid search — vector + keyword, fused by
reciprocal rank, not just cosine distance.** A design discussion up front settled the shape: HyDE
(generating a hypothetical answer via LLM and embedding *that* instead of the raw query) was
considered and explicitly rejected — it would add a real LLM call, a few seconds, to every single
search, versus today's fast embedding-only call, for a benefit that wasn't worth that cost here.
What shipped instead: the existing direct query embedding, combined with genuine Postgres full-text
search over the actual text (`documents.search_vector`/`document_chunks.search_vector`, `GENERATED
ALWAYS AS (to_tsvector('english', ...)) STORED` columns with GIN indexes — auto-recomputed on every
`UPDATE`, including the incremental ones `ingest_and_index_document` makes as each summary flavor
lands, so there's no manual backfill/trigger to maintain). The two rankings are combined via
**reciprocal rank fusion** (`score = Σ 1/(60 + rank_in_each_method)`, run separately for the vector
and text CTEs, `FULL OUTER JOIN`ed on row id) rather than a weighted score blend — RRF sidesteps
needing to normalize cosine distance and `ts_rank` onto a comparable scale (they aren't naturally
comparable numbers), at the cost of the fused score no longer meaning "1.0 = identical" the way raw
cosine similarity did; it's now just a relative ranking signal, documented as such in both tools'
docstrings. Falls back gracefully to pure vector ranking when a query has no lexical overlap at all
— the text-ranked CTE is simply empty, contributing `0` to every row's fused score rather than
excluding rows or erroring. `websearch_to_tsquery` (not the stricter `to_tsquery`) tolerates
arbitrary/malformed query text without raising, including empty strings and stray special
characters — verified directly, not assumed, along with the actual fusion math (a query matching
both signals at rank 1 scores ~2× a query matching only one — `2 × 1/61` vs `1/61`, exactly as the
formula predicts) and that pure-vector fallback still finds the right document when there's no
keyword overlap. `search_document_chunks`'s existing ownership JOIN (`document_id` must belong to
the requesting `user_id`) is applied in *both* the vector and text CTEs, so the security guarantee
holds regardless of which signal — or both — a chunk would otherwise match on.

**General-purpose database access (`query_database`) is real SQL, but structurally can't write or
cross a user boundary — enforcement is a Postgres role's grants, not (just) string validation.**
Every other search tool takes a natural-language `query`, embeds it, and runs one specific,
hand-written SQL statement. `query_database` is different in kind: the model writes the actual SQL
(against a small set of pre-scoped views), for exploratory questions the curated tools don't answer
directly. That's real power, so it's built with the same "the check that matters is the one that
can't be argued with" instinct as `read_document`'s path scoping, taken further: a dedicated
Postgres role, `alira_agent_readonly` (`NOLOGIN` — only ever assumed via `SET LOCAL ROLE` inside a
transaction, never connected to directly), is granted `SELECT` on five views in a separate
`agent_readonly` schema (`my_conversations`, `my_conversation_turns`, `my_documents`,
`my_document_chunks`, `my_document_keywords`) — and *nothing else*, not even the underlying tables.
Each view filters on `current_setting('app.current_user_id', true)`, set via `set_config(...,
true)` (transaction-local, third arg `true`) for the duration of one query. `memory.db.
run_readonly_query` always rolls the transaction back in a `finally`, regardless of outcome — there
is never a reason to commit a read-only path, so this closes off "what if a write somehow got
past the role's grants anyway" for free. Client-side string validation (single `SELECT` only, no
`;`, a keyword blocklist) runs *before* any of that as a cheap first filter, but it's explicitly
not the real boundary — verified directly, not assumed: a `SELECT * FROM documents` (the raw
table, bypassing the views entirely) fails with Postgres's own `permission denied for table
documents`, and querying as a different/fake `user_id` against the real views returns `[]`, not
another user's rows. A trailing SQL comment in the model's query can't comment out the wrapper's
own closing parenthesis either — `run_readonly_query` inserts the model's SQL on its own line
inside `SELECT * FROM (\n{sql}\n) AS ... LIMIT 50`, so a `--` can only consume up to that newline.
Confirmed pooled connections revert cleanly afterward too (`SET LOCAL` is transaction-scoped by
definition, but verified rather than assumed): five `run_readonly_query` calls in a row, then a
plain query on the same pool still runs as the normal `alira` role with full privileges.

**The tool-call step budget is extendable by the model, not just a fixed ceiling.** `max_steps`
(default 5) is where `_run_tool_loop` starts, but it's not where it has to end: calling
`continue_research(reason)` mid-loop grows the budget by `_CONTINUE_RESEARCH_STEP_INCREMENT` (5),
capped at `hard_max_steps` (`.env`'s `AGENT_HARD_MAX_STEPS`, default 20) — an admin-set ceiling the
model can ask to approach but never override, however many times it asks. `continue_research` is
deliberately *not* a real dispatched tool: it's intercepted by name in `_run_tool_loop` itself,
before normal `TOOL_FUNCTIONS` lookup — `tooling/control_tools.py`'s own function body exists only
so `@tool` has something to introspect for the schema the model sees; it never actually runs. Added
specifically for multi-step investigations like iterative literature search, where a fixed, small
step cap cuts the work short right when it's getting somewhere — but it's deliberately generic, not
gated to literature tools, since any sufficiently complex task could reasonably need the same.
Verified with a forced mock sequence: a budget of 2 (would normally exhaust after 2 steps) that
calls `continue_research` first extends to 7 and completes successfully; a budget already at its
hard ceiling correctly denies a further request and the loop still raises `ToolLoopError` as
before (dumped to the transcript/DB via the existing error path, not silently swallowed).

**Real-time visibility into a long tool loop reuses `ChannelFeedback` — it doesn't touch the
markdown transcript.** The transcript/DB record has always been written once, after the whole turn
completes (`persist_turn`, called from `pipeline.handle_incoming_message` only after
`generate_with_tools` returns) — there was no live view into a multi-step loop while it ran.
Rather than rearchitect transcript writing to be incremental, `_run_tool_loop` now calls
`feedback.progress(f"Step {n}: {tool_names}", steps_taken / steps_budget)` once per step, reusing
the exact mechanism already proven for document-ingestion progress — `pipeline.py`'s
`feedback.start("Thinking")` before the loop and the per-step `progress()` calls during it edit the
*same* Telegram message, so `TelegramFeedback`'s existing lifecycle needed no changes at all. The
fraction is recomputed each step against the *current* budget, so it can move backward right after
a `continue_research` grant (e.g. 4/5 → 4/10) — an honest reflection of more work having just been
added, not a bug. Verified end-to-end against the real APIs, not mocked: a live three-step run
(`search_arxiv` → `read_paper` → `save_finding`) produced exactly the expected `20% → 40% → 60%`
progress trail and a correctly-saved bookmark, in 10.5 seconds total.

**Literature search (`search_arxiv`/`search_pubmed`/`read_paper`) is deliberately two-tier — an
overview first, full detail only on request — after a real run measured why that matters.**
Adapted from a prior project's `core/search/services/db_handlers/` (copied with zero modification —
both are self-contained, `requests` + stdlib only), these share one schema and a canonical `field`
vocabulary (`canonical_to_arxiv_field`/`canonical_to_pubmed_field`, designed upstream for exactly
this kind of shared integration). The first design measured a single default 10-result `search_arxiv`
call serializing to **~3,900 tokens — ~95% of this deployment's 4096-token context budget, by
itself**, before the prompt, system prompt, or tool schemas. A real agent turn using it was
immediately truncated to an empty response as a direct result — not a theoretical risk, an actual
observed failure. Fixed by splitting into `search_arxiv`/`search_pubmed` (title/authors/date/venue
only — ~790 tokens for 10 results) and `read_paper(src_db, db_id)` (the rest — abstract_text,
hyperlink, doi, categories, pdf_url, is_open_access — ~400 tokens, paid only for papers actually
inspected, not every hit). `read_paper` is stateless by design, not a cache: it re-queries the live
API for just that one paper (via each module's existing `raw_query` escape hatch — `id:{db_id}` for
arXiv, `{db_id}[uid]` for PubMed — no modification needed to either module), trading one extra
~1s API call per lookup for zero memory-management surface (no cache eviction, no staleness, no
new subsystem). Re-ran the exact real scenario that failed before this split: `search_arxiv` →
`read_paper` → `save_finding` completed cleanly in 10.5s with a correct final answer, no truncation.

**Literature findings save as lightweight `documents` bookmarks, unified with uploads — not a
separate table.** `save_finding` (`llm_engine/tooling/literature_tools.py`) writes title/authors/
abstract-as-`summary`/embedding into the same `documents` table real uploads use, so a bookmarked
arXiv/PubMed hit becomes findable through the exact same `search_documents` hybrid search as an
uploaded PDF — one unified search surface, not two. What distinguishes the two is an explicit
`document_kind` column (`'upload'` or `'bookmark'`, `CHECK` constraint, default `'upload'` so every
pre-existing row classified correctly with zero backfill) — considered a bare boolean first, went
with an enum-shaped text column instead so a third state (e.g. "upgraded to full ingestion" if that
ever gets built) doesn't need another migration. `dir_path`/`text_path` stay `NULL` for a bookmark
— no local file, no chunks, so `read_document`/`search_document_chunks` won't work on one of these;
only the document-level fields are available, which `search_documents` now also surfaces
(`document_kind`, `source_url`, `doi`) so the agent knows not to try. `external_ref`
(`"{src_db}:{db_id}"`) is a partial-unique-indexed dedup key (`ON CONFLICT (user_id, external_ref)
WHERE external_ref IS NOT NULL DO UPDATE`) — saving the same paper twice updates the existing
bookmark rather than creating a duplicate, verified directly by saving the same real paper twice
and confirming both the same `document_id` came back and exactly one row exists.

**Document indexing is sequential, not concurrent, by choice.** `ingest_and_index_document` makes
several independent LLM calls (a variable number of per-chunk digests, plus general/methods/results
summaries + metadata extraction — see map-reduce summarization below) plus embeddings for each.
These *could* be dispatched together via `asyncio.gather`, and an earlier version did — but whether
the Ollama server actually executes concurrent requests to the same model in parallel, or just
queues and serializes them, wasn't something this project had verified at the time. It's since been
confirmed directly: `systemctl status ollama` shows the actual `llama-server` launch command
includes `-np 1` (one parallel processing slot) — Ollama on this deployment genuinely serves one
request at a time per model, full stop, not merely "unverified whether it parallelizes." So each
call running one at a time isn't just a cautious default anymore, it's the only way requests
actually execute here regardless of how they're dispatched from this side. If real parallel
throughput is ever needed, that's a job for a proper task queue (Redis/Celery — the reference
project already uses this for its own GPU-bound Celery tasks) fanning out to multiple Ollama
instances or a server configured with more parallel slots, not `asyncio` concurrency against this
one.

**Chunking is sentence- and line-aware, never mid-sentence.** `chunk_text` (in
`llm_engine/memory/document_index.py`) first splits the extracted text into lines, then each line
into sentences (`re.split(r'(?<=[.!?])\s+', ...)`), giving a flat ordered list of
`(line_number, sentence)` units. Units are greedily accumulated into a chunk up to `chunk_size`
characters — a chunk boundary can only ever fall between two sentences (or two lines), never inside
one. Overlap is implemented by stepping back far enough through the *previous* chunk's trailing
units (by their approximate character length, not a raw character slice) that the next chunk starts
with recent context instead of cold. Each returned `ChunkResult` carries `content` plus the
`start_line`/`end_line` (1-indexed, inclusive) it came from, persisted onto
`document_chunks.start_line`/`end_line` — enough for a future reader to jump straight to
`read_document(text_path, start_line=..., end_line=...)` for full surrounding context. This
replaced an earlier fixed-size character-slicing version that ignored sentence/line boundaries
entirely and had no way to say where in the document a chunk came from.

**Summary/methods/results generation is map-reduce, not one call over the raw document — a large
`num_ctx` was tried first and rejected.** Ollama's actual default context window on this deployment
is 4096 tokens regardless of what a model architecturally supports (confirmed via `/api/ps`'s
`context_length` field and the `-c 4096` baked into `systemctl status ollama`'s launch command) —
and it fails silently, not with an error: a longer prompt just gets truncated
(`llama-server`'s `--context-shift` keeps the *most recent* tokens and drops the rest). This was the
confirmed root cause of two bugs seen earlier: document summaries drifting toward describing just a
document's tail (whatever was left "in view" after truncation), and repeated metadata-extraction
JSON-parsing failures. The first fix attempted was a per-call `num_ctx` override
(`GenerativeService.generate_text`'s `num_ctx` param, routed through Ollama's *native* `/api/chat`
since `num_ctx` — like `keep_alive` — isn't honored through the OpenAI-compatible endpoint the rest
of `tgi.py` uses; confirmed via
[ollama/ollama#6544](https://github.com/ollama/ollama/issues/6544)/[#7063](https://github.com/ollama/ollama/issues/7063)).
That correctly stopped the truncation (confirmed: `n_tokens = 14248, truncated = 0` in Ollama's own
logs for the real test PDF) but uncovered something worse: a **catastrophic decode-speed cliff** at
real document lengths on this hardware — `journalctl -u ollama` showed live generation at **0.85
tokens/second** with a ~14k-token prompt at `num_ctx=32768`, versus ~77 tok/s at the default
context, a ~90x slowdown, even though Ollama's own memory-fit logic confirmed everything fit in
VRAM with room to spare. The likely cause: GQA attention cost scales with how deep the KV cache is,
so every decode step now has to attend back across ~14k prior tokens instead of ~2k; whether that
alone accounts for a 90x hit, or an immature long-context kernel path on this Blackwell-generation
GPU compounds it, wasn't fully isolated. Either way, a big `num_ctx` for a whole document turned out
to be a dead end on this hardware.

The actual fix: **map-reduce**. `ingest_and_index_document` splits the extracted text into
larger chunks via the same `chunk_text` used for RAG (`MAP_CHUNK_SIZE`/`.env`'s
`DOCUMENT_MAP_CHUNK_SIZE`, default 8000 chars — distinct from the smaller RAG-retrieval
`CHUNK_SIZE`), generates a short digest of each chunk individually (`prompts.chunk_digest_prompt`)
at the cheap default context — fast, and never at risk of truncation since each chunk is small — and
then runs the *existing* `document_summary_prompt`/`methods_summary_prompt`/`results_summary_prompt`
unchanged against the concatenation of those digests, which stays comfortably within the default
context even for a long document. Verified end-to-end against the real test PDF (8 map-chunks): the
general summary now correctly follows its template again, methods/results summaries are detailed
and accurate, and — as a side effect, since metadata is now scoped to just the first map-chunk
rather than the full (previously truncated) text — the metadata-extraction JSON failures also
stopped recurring. Total pipeline time for that document: ~181s (was previously failing/producing
wrong output at this length, not a clean baseline to compare against). `GenerativeService`'s
`num_ctx`/native-API capability was kept (it's correct, tested infrastructure, not wrong on its own —
just not the right tool for whole-long-document prompts on this hardware).

**`marker-pdf` and a resident Ollama model can OOM competing for the same GPU — batch-size tuning
alone doesn't fix it.** On hardware where Ollama and this bot process share one GPU (true here:
`DEFAULT_SERVER_IP`/`OLLAMA_API_URL` resolve to this same machine's own Tailscale address), a
16GB-class card can't hold both a ~13-14GB resident LLM *and* marker's own model weights (layout +
detection + recognition + table-rec + OCR-error) at once. Verified directly: with `gpt-oss:20b`
idle-resident in VRAM, starting document extraction failed with a genuine `torch.OutOfMemoryError`
*while loading marker's own weights* — before any batch ever ran. Lowering
`MARKER_*_BATCH_SIZE` (see Environment variables below) doesn't help this failure mode: batch size
only bounds *activation* memory during a forward pass, not the fixed cost of loading model weights
in the first place. The actual fix is `GenerativeService.unload_all_models()`
(`llm_engine/tgi.py`), called by `ingest_and_index_document` right before extraction starts: it
queries Ollama's native `/api/ps` for whatever's currently loaded and unloads each one
(`/api/generate` with `keep_alive=0`), polling briefly to confirm the eviction actually happened
before returning. This has to go through Ollama's *native* API rather than the OpenAI-compatible
endpoint the rest of `tgi.py` uses — `keep_alive` passed via the OpenAI SDK's `extra_body` is
silently ignored by Ollama (confirmed:
[ollama/ollama#11458](https://github.com/ollama/ollama/issues/11458)), so it's the only path that
actually works. Ollama reloads naturally afterward — either on this same pipeline's own next
`generate_text()` call (summary/metadata/methods/results), or the agent's next turn — verified
end-to-end: extraction completes cleanly with the model unloaded, and a subsequent chat turn
reloads with `size_vram == size` (no spillover). The trade-off is a one-time reload cost right
after an upload, accepted deliberately since it's scoped to just that moment rather than (as a
global `OLLAMA_KEEP_ALIVE=0` would do) adding a reload to every single message.

**Batch-size tuning: benchmarked, no free lunch.** Since `unload_all_models()` already fixes the
OOM, `MARKER_*_BATCH_SIZE` was benchmarked on its own merits (real PDF, repeated runs, SHA-256
hashing extraction output to check for silent quality drift) rather than assumed. Findings: (1)
scaling all four batch sizes together from 100%→25% of default barely moved time (~59-70s) or peak
VRAM (~11.5-13.5GB, non-monotonic — noise) — the ~12GB+ footprint is dominated by fixed model-weight
loading across five separate models, not batch-dependent activation memory; (2) only an aggressive
10% cut showed a real, repeatable effect (3 runs each: 63.44s/12.74GB at 100% vs. 36.19s/7.29GB at
10%, tight variance) — but diffing the extracted markdown between them showed **real quality loss**,
not just noise: dropped citation superscripts and a character-level OCR substitution (en-dash →
hyphen), consistent with batch composition shifting borderline low-confidence decisions across a
classification boundary (see below); (3) isolating each knob individually (cut to 25%, others at
default) showed `detection`/`table_rec` can be cut freely with zero output change, but also zero
meaningful time/VRAM benefit (they were never the bottleneck — text recognition alone is ~55s of a
~63-70s run), while `recognition` is both the actual bottleneck *and* the sole source of the
observed quality risk, and `layout` was a clear net loss (changed output, and even ran slower/used
more VRAM in this single run). Conclusion: no isolated cut is both safe and worthwhile, so all four
were left unset (surya's defaults). The batch-size-vs-quality mechanism is most likely GPU kernel
non-determinism: different batch shapes select different reduction orders in the underlying CUDA
matmuls, producing tiny floating-point differences that most decisions absorb harmlessly but that
can flip an already-borderline classification (a diff of ~6 lines out of 272, all borderline calls,
not wholesale corruption) — a reasonable, evidence-consistent explanation, not something traced
through surya's exact kernels line by line. Also tried and rejected as an alternative to a batch cut:
an LLM cleanup pass over the (corrupted, 10%-batch) extracted markdown — it cannot recover
information that's genuinely gone (a dropped citation number has no textual trace to recover from),
and the attempt itself failed outright on this hardware for an unrelated reason: qwen3:14b's default
4096-token context silently truncated the ~8,119-word input, so the model produced a 540-word
synthesis instead of the requested full rewrite — the same context-window bug that motivated the
map-reduce summarization work above.

**One folder per upload, named after its own row id.** `documents.dir_path` follows the same
two-step pattern as `conversations.file_path`: insert the row first (to get its id), then create
`logs/uploads/{user_id}/{conversation_id}/{document_id}/` and write the raw file, extracted
markdown, and any extracted `images/`/`tables/` into it. Per-upload (not per-conversation)
directories mean two uploads with the same filename in one conversation never collide, and
everything marker-pdf produced for one document — including images/tables, kept for provenance
even though nothing reads them yet (`qwen3:14b` is text-only, so images aren't tool-accessible;
tables are stored as CSVs but no tool for them exists yet either) — stays together as a unit.

**Output-only surface.** Telegram only ever sees `result["output"]`. The reasoning trace, tool
calls/results, evaluation verdicts, and token counts never reach the chat — they're logged
(`utils.setup_logging`) and persisted (`llm_engine.memory`) instead. This keeps the chat clean
while keeping the full pipeline auditable elsewhere.

**Transparency via markdown + JSONB backup.** Every conversation is written twice, concurrently:
a human-readable `.md` transcript (the source of truth — git-diffable, greppable, openable in any
editor) and a JSONB row per turn in Postgres (queryable from SQL without touching the
filesystem). Both writes run via `asyncio.gather`, and the markdown write itself runs off-thread
(`asyncio.to_thread`), so persistence never adds to the user's perceived latency — it's meant to
be awaited *after* the reply has already been sent.

**Tool registry via decorator.** Any function decorated with `@tool`
(`llm_engine/tooling/registry.py`) is automatically added to `TOOLS` (an OpenAI function-calling
schema, derived from the function's type hints + docstring via pydantic) and `TOOL_FUNCTIONS`
(name → callable, for dispatch). Adding a new tool is: write the function, decorate it, import it
in `tooling/__init__.py`. No schema is ever hand-written.

**Sync core, async shell.** `GenerativeService` is deliberately synchronous (simpler control flow
for the tool-calling loop, and it wraps a sync `OpenAI` client). It's only ever called from async
code via `asyncio.to_thread`, so a slow generation or tool call never blocks the bot's event loop.

**Identity flows around the model, not through it.** `search_memory`/`read_conversation` need to
know which user is asking, but a tool parameter is something the model controls — it can't be
trusted to correctly (or honestly) supply "who I am." Instead, `llm_engine/context.py` exposes a
`ContextVar` (`current_user_id`) that the caller sets right before invoking
`generate_with_tools`; the tools read it directly, and it's never part of either tool's schema, so
the model has no way to see or override it. The same module also bridges the sync/async boundary
for those tools: they run inside a worker thread (via the `asyncio.to_thread` above), but the
asyncpg pool is bound to the main event loop, so `run_on_main_loop` dispatches their DB queries
back onto it (`asyncio.run_coroutine_threadsafe`) instead of spinning up a second pool on a second
loop, which asyncpg doesn't allow.

**Dual import style.** Modules under `llm_engine/` use a `try: from .sibling import X / except
ImportError: from sibling import X` pattern, so every file works both as part of the `llm_engine`
package (`from llm_engine.tgi import GenerativeService`, used by `webhook_server.py` at the repo
root) *and* as a standalone script run with `cwd=llm_engine/` (matches the original `tests.py`
convention: `from tgi import GenerativeService`).

## Data flow: one incoming message, end to end

1. The channel adapter (`webhook_server.handle_message`/`handle_document`) extracts `user_id`/
   `chat_id` from its own native objects and calls `pipeline.handle_incoming_message(user_id,
   chat_id, text, attachments=...)`.
2. `pipeline.get_conversation(chat_id, user_id)` returns that chat's `(ConversationTranscript,
   conversation_id)` — lazily creating them on first use, or auto-archiving and replacing them if
   the conversation has been idle longer than `CONVERSATION_IDLE_TIMEOUT_SECONDS`. See
   "Conversation lifecycle" below.
3. If `attachments` were given, each is run through `memory.document_index.ingest_and_index_document`:
   registers a `documents` row, extracts it (marker-pdf → markdown + images + tables, saved under a
   per-upload folder — see "One folder per upload" above), chunks + embeds the text into
   `document_chunks`, and summarizes + embeds the whole document into `documents.summary`/
   `.embedding`. A note about it (filename + extracted-text path) is folded directly into the
   effective prompt text — not just a one-off system-prompt aside, so the reference to the upload
   survives into later turns via `conversation_history` (below), the same way everything else does.
4. `get_turns(conversation_id)` fetches the conversation's turns so far, and `pipeline._turns_to_history`
   projects as many of the most recent ones as fit in `MAX_HISTORY_CHARS` (a character budget, not
   a turn count — a handful of big turns can cost as much as many small ones, so a count alone
   never actually bounded this) into a `conversation_history` list — this is the model's **live,
   short-term memory** of the still-open conversation, distinct from `search_memory`'s long-term
   lookup over already-archived ones. If any turns were left out, `prompts.recall_history_note` is
   set as `system_prompt` — it tells the model exactly how many turns it can currently see, and to
   call `recall_conversation_history(skip_recent=<that many>)` for anything older, pulled straight
   from `conversation_turns` rather than re-reading a file. (`search_memory` can't help here: only
   *archived* — summarized+embedded — conversations are searchable, and this one still isn't.)
5. `GenerativeService.generate_with_tools(prompt, conversation_history=...)` runs off-thread:
   - Builds the message list as `[system, *conversation_history, new user prompt]`. The system
     prompt accumulates `TOOL_REASONING_PROMPT`, a JSON-schema instruction (if `as_json=True`),
     `MEMORY_SEARCH_PRIORITY_PROMPT` (if `search_memory` is among the offered tools — prefer recent
     matches over older, merely conceptually-related ones), and `DOCUMENT_SEARCH_GUIDANCE_PROMPT`
     (if `search_documents` is offered — prefer the most relevant document, and cite which one by
     filename).
   - `_run_tool_loop` drives model↔tool round trips (up to `max_steps`), dispatching each
     `tool_call` through `TOOL_FUNCTIONS` and recording a `trace` entry per step.
   - If `evaluate=True`, `_judge_output` scores the final answer against `JUDGE_SCHEMA` and can
     trigger up to `max_eval_retries` more loop passes on a failing verdict. (Off by default in
     the bot — doubles round trips per message.)
6. Only `result["output"]` is sent back to the user, by the channel adapter.
7. `persist_turn()` is awaited (after the reply): writes the markdown transcript entry and the
   JSONB backup row concurrently.

## Conversation lifecycle

All of this lives in `llm_engine/pipeline.py`, not `webhook_server.py` — any channel adapter gets
it for free.

- **First message from a chat** → a new `ConversationTranscript` and a new `conversations` row are
  created and kept in the in-process `pipeline._conversations` dict, along with a `last_active`
  timestamp.
- **Every message** refreshes that chat's `last_active` timestamp (`time.monotonic()`, immune to
  wall-clock changes).
- **Idle timeout** → if a chat's next message arrives more than `CONVERSATION_IDLE_TIMEOUT_SECONDS`
  (default 3600) after its last one, `get_conversation` archives the old conversation in the
  background (same as `/start`, via the shared `_archive_in_background` helper) and transparently
  opens a new one before processing the new message.
- **`/start`** (`pipeline.start_new_conversation`) → the *previous* conversation (if any) is popped
  from `_conversations` and archived in the background; the next message opens a brand new one.
- **`/summarize`** (`pipeline.summarize_open_conversation`) → summarizes + embeds the *current*,
  still-open conversation on demand, replies with the summary text, and does **not** close the
  conversation — you can keep chatting in it.
- **Process restart** → `_conversations` is in-memory only, so every open chat effectively gets a
  new conversation on next message. Nothing is lost (the old transcript file and DB rows are
  untouched, just orphaned from `_conversations`), but nothing gets auto-archived either — you'd
  need to call `summarize_and_embed_conversation` manually for any conversation that was open at
  the moment of a restart.

## Infrastructure

- **`docker-compose.yml`** — single `postgres` service, image `pgvector/pgvector:pg16`, data in
  the named volume `alira_postgres_data`, healthchecked on `pg_isready`. `docker compose up -d`
  to start it.
- **Schema** (`llm_engine/memory/schema.sql`, applied idempotently by `init_schema()` on bot
  startup):
  - `conversations` — `id`, `user_id` (person identifier — Telegram user id today, standing in
    until real auth exists), `chat_id` (interface thread id), `file_path` (points at the markdown
    transcript; `NULL` until the transcript is created, since its filename embeds this row's own
    id), `summary`, `embedding vector(768)`, `created_at`, `updated_at`. Indexes: btree on
    `user_id`, btree on `chat_id`, HNSW (cosine) on `embedding`.
  - `conversation_turns` — `id`, `conversation_id` (FK → `conversations`, `ON DELETE CASCADE`),
    `user_id` (denormalized copy, for per-user turn queries without a join), `user_prompt`,
    `result JSONB`, `model_name`, `created_at`. Indexes: btree on `conversation_id`, btree on
    `user_id`.
  - Both tables carry an idempotent migration block (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
    + backfill) so `user_id` gets added safely to pre-existing installs.
  - `documents` — `id`, `user_id`, `conversation_id` (FK → `conversations`, `ON DELETE CASCADE`,
    nullable — a document isn't required to belong to a conversation), `filename`, `dir_path` /
    `text_path` (both `NULL` until the per-upload folder is created, same two-step pattern as
    `conversations.file_path` — also `NULL` for a `'bookmark'`, permanently, since there's no local
    file), `title`, `authors TEXT[]`, `summary` + `embedding vector(768)` (general — also covers
    conceptual/theoretical framing; for a bookmark, the paper's abstract), `methods_summary` +
    `methods_embedding`, `results_summary` + `results_embedding`, `search_vector tsvector`
    (`GENERATED ALWAYS AS (to_tsvector('english', title || summary || methods_summary || ...))
    STORED` — the lexical half of hybrid search), `document_kind TEXT` (`'upload'` or `'bookmark'`,
    `CHECK` constraint, default `'upload'`), `source_url`/`external_ref`/`doi TEXT` (bookmark
    provenance — `NULL` for an upload; `external_ref` is `"{src_db}:{db_id}"`, a dedup key),
    `created_at`, `updated_at`. Indexes: btree on `user_id`, btree on `conversation_id`, HNSW
    (cosine) on each of the three embedding columns, GIN on `search_vector`, unique on
    `(user_id, external_ref) WHERE external_ref IS NOT NULL`.
  - `document_chunks` — `id`, `document_id` (FK → `documents`, `ON DELETE CASCADE`), `chunk_index`,
    `content`, `start_line`, `end_line` (both 1-indexed/inclusive, from `chunk_text`'s
    sentence-/line-aware splitting), `embedding vector(768)`, `search_vector tsvector`
    (`GENERATED ALWAYS AS (to_tsvector('english', content)) STORED`), `created_at`. Indexes: btree
    on `document_id`, HNSW (cosine) on `embedding`, GIN on `search_vector`. Queried by
    `search_document_chunks`.
  - `keywords` — `id`, `word` (`UNIQUE`, lowercased/stripped before insert) — deduped across every
    document. `document_keywords` — `(document_id, keyword_id)` composite PK, join table; index on
    `keyword_id`. Both `ON DELETE CASCADE` from their respective parents.
  - `agent_readonly` schema — five views (`my_conversations`, `my_conversation_turns`,
    `my_documents`, `my_document_chunks`, `my_document_keywords`), each filtered on
    `current_setting('app.current_user_id', true)`, plus the `alira_agent_readonly` `NOLOGIN` role
    granted `SELECT` on them and nothing else — backs the `query_database` tool. See "General-purpose
    database access" above for the full design and how it's enforced.

## Environment variables (`.env`)

| Variable | Used by | Purpose |
|---|---|---|
| `LOG_DIR` | `utils.py`, `memory/transcript.py`, `documents.py` | Base dir for logs (`LOG_DIR/*.log`), transcripts (`LOG_DIR/conversations/`), and uploaded documents (`LOG_DIR/uploads/`). |
| `POSTGRES_USER/PASSWORD/DB/PORT` | `docker-compose.yml` only | Provisions the Postgres container. Not read by the app. |
| `DATABASE_URL` | `llm_engine/memory/db.py` | The actual Postgres connection string the app uses. Change this to swap backends. |
| `EMBEDDING_DIM` | `llm_engine/memory/db.py` | Vector column width in `schema.sql`. Must match the embedding model's output size (768 for `nomic-embed-text`). |
| `EMBEDDING_MODEL` | `llm_engine/embeddings.py` | Ollama model used for `embed()`. |
| `CONVERSATION_IDLE_TIMEOUT_SECONDS` | `llm_engine/pipeline.py` | How long a chat can go quiet before its conversation is auto-archived on the next message. Default `3600`. |
| `MAX_HISTORY_CHARS` | `llm_engine/pipeline.py` | Character budget (not a turn count) for how much of the open conversation's recent history is fed back as live `conversation_history`. Older turns are looked up on demand via `recall_conversation_history` instead. Default `3000`. |
| `AGENT_MAX_STEPS` / `AGENT_HARD_MAX_STEPS` | `llm_engine/pipeline.py` | Starting/ceiling step budget for the tool-calling loop. The model can extend the starting budget up to the ceiling by calling `continue_research` mid-loop, but never past it. Defaults `5` / `20`. |
| `DOCUMENT_CHUNK_SIZE` / `DOCUMENT_CHUNK_OVERLAP` | `llm_engine/memory/document_index.py` | Character-based (not token-aware) target chunk size / overlap for `chunk_text`, which only ever breaks at sentence/line boundaries — actual chunk lengths vary around this target. Defaults `1500` / `200`. |
| `MARKER_DETECTION_BATCH_SIZE` / `MARKER_LAYOUT_BATCH_SIZE` / `MARKER_RECOGNITION_BATCH_SIZE` / `MARKER_TABLE_REC_BATCH_SIZE` | `llm_engine/documents.py` | Per-model batch sizes passed to `marker-pdf`'s `ConfigParser` (surya's own CUDA defaults are 36/32/256/32). Unset (the current setting) = surya's default. Benchmarked lowering these — see "Batch-size tuning: benchmarked, no free lunch" below — and left unset; kept as a knob since the code path is real and harmless to use, just not something this project currently sets. |
| `DOCUMENT_MAP_CHUNK_SIZE` | `llm_engine/memory/document_index.py` | Map-reduce chunk size (chars) for summary/methods/results generation — distinct from `DOCUMENT_CHUNK_SIZE` (RAG-retrieval granularity). Default `8000`. See map-reduce summarization above. |
| `TELEGRAM_BOT_TOKEN` | `webhook_server.py` | Bot auth token. |
| `TELEGRAM_CHAT_ID` | *(unused by current code)* | Reserved. |
| `MODEL_NAME` | `webhook_server.py` (passed explicitly only for `/start`/`/summarize`); `pipeline.py`'s fallback default otherwise | Default chat model — see `pipeline.handle_incoming_message`'s resolution order (explicit argument > per-chat `/switch_model` override > this). |
| `OLLAMA_API_URL` | `llm_engine/tgi.py` (`GenerativeService.unload_all_models`) | Ollama's native API base (no `/v1` suffix) — used only for `/api/ps` and `/api/generate` with `keep_alive=0`, since `keep_alive` isn't honored through the OpenAI-compatible endpoint the rest of `tgi.py` uses. |
| `DEFAULT_SERVER_IP` | `llm_engine/tgi.py` | Fallback Ollama endpoint for any model not listed in `LLM_ROUTING`. |
| `LLM_ROUTING` | `llm_engine/tgi.py` | JSON-ish dict (single-quoted, parsed after string substitution) mapping model name → `{host_endpoint, api_key}`, for routing different models to different servers. |
| `TAVILY_TOKEN` | `llm_engine/tooling/websearch.py` | API key for the `web_search` tool. |
| `PUBMED_EMAIL` / `PUBMED_API_KEY` | `llm_engine/tooling/literature_tools.py` | Both optional. `PUBMED_EMAIL` is a courtesy identifier NCBI asks for on E-utilities traffic; `PUBMED_API_KEY` raises the rate limit if set. |

## Planned: Celery/Redis for GPU-bound work

**Status: design only — nothing in this section is implemented yet.** Written down here ahead of
the code so the shape is settled before anything changes; see ROADMAP.md for implementation order.

**Why.** Every GPU-touching call today runs in this one process, offloaded to worker threads as
needed. Nothing stops two from overlapping — a chat message arriving mid-upload, or two uploads
at once, can genuinely race for the same VRAM (see "`marker-pdf` and a resident Ollama model can
OOM" above — that fix only covers one specific case, not concurrent access in general). The goal:
every GPU-touching call funnels through a single queue with concurrency fixed at 1, so exactly one
such call can ever be running, by construction — including a future where more than one person
uses this bot against the same GPU, and a future where GPU work runs on a different machine
entirely than the one receiving Telegram updates.

**Chat and document ingestion get different task boundaries, deliberately.** It's tempting to
wrap the same unit both times ("the whole turn," or "the whole document"), but they don't have the
same shape:

- **Document ingestion — the whole pipeline is one task.** `ingest_and_index_document` already
  takes explicit `user_id`/`conversation_id` arguments (no `ContextVar` dependency at all), so a
  task body is close to a direct lift: `asyncio.run(ingest_and_index_document(...))`, unmodified
  internally. Fine-grained wrapping (one task per LLM/embedding call, of which a single upload
  makes a dozen-plus) was considered and rejected — each step depends on the last, so it would just
  mean a dozen-plus sequential round trips instead of one, for no benefit.
- **Chat — only the raw model call is a task.** `_run_tool_loop` (`tgi.py`) calls
  `client.chat.completions.create(...)` directly, up to `steps_budget` times per turn, with tool
  execution interleaved between calls. Wrapping the *whole* tool loop (tools included) was the
  first design, but it meant `current_user_id`/`run_on_main_loop`/the tool functions/conversation
  state (`pipeline._conversations`/`_model_overrides`) all having to move into a separate process —
  a much bigger rewrite, for no actual gain, once it's clear that tools never need to run anywhere
  but the main process. Instead: only the one line that calls the model becomes a task (call it
  `call_model`, on the `chat` queue); `_run_tool_loop` itself, `pipeline.py`, every tool function,
  `context.py`'s bridge, and Telegram access all stay **exactly as they are today, unmodified** —
  they never leave the main process. The cost, honestly: a multi-step turn makes one `call_model`
  round trip per loop iteration instead of one per turn (needs a Celery result backend + a blocking
  wait per iteration, inside the worker thread that's already isolated there today — doesn't block
  the event loop, same as a direct Ollama call does today).

**`run_on_main_loop`/`current_user_id` are *not* being retired — this was an earlier, wrong
conclusion, corrected once the task boundary above settled.** An initial pass at this design
assumed the whole tool loop would move into the Celery worker, which would have genuinely broken
the bridge (no shared event loop across a process boundary) and required retiring it. Once the
task boundary narrowed to just the raw model call, the bridge never needed to move at all — every
site that uses it (5 tools doing DB lookups, plus the tool loop's own per-step progress update)
keeps running in the main process, for the same reason it works today: same process, same event
loop. Document ingestion's Celery task doesn't use the bridge either, but for a different reason —
it was never built on it; its task body talks to the DB via a plain local `asyncio.run(...)` inside
its own process, which is the right pattern for code that genuinely runs somewhere else.

**Queue topology: one worker, two named queues, explicit-command-only priority.** A single Celery
worker (`prefork` pool, `--concurrency=1` — `prefork`, not `solo`, specifically because `solo` has
no separate process to kill, and abort needs one; `worker_prefetch_multiplier=1` so the worker
never grabs jobs ahead of time) normally consumes only `chat`. A user-issued command (e.g.
`/ingest-docs`) — **never automatic, never time-based** — hands off to `literature`: but not
instantly. Flipping `cancel_consumer("chat")` the moment the command arrives could strand an
already-in-progress multi-step chat turn mid-loop (its next `call_model` dispatch would sit
unconsumed until the whole batch finishes, possibly minutes later) — a turn that's genuinely
mid-flight should finish before the switch takes effect, not get cut off by it. So the command
handler waits for an in-process "chat turns currently in flight" counter to reach zero (incremented
when a turn's tool loop starts, decremented when it ends — tracked in the main process, since
that's the only process deciding when to flip) before actually calling `cancel_consumer("chat")` +
`add_consumer("literature")`. Brand-new messages arriving during that short wait still process
normally — the goal is "don't interrupt something already running," not an instant, perfectly
race-free cutover. The batch's own last task flips it back (`cancel_consumer("literature")` +
`add_consumer("chat")`) and sends the end-of-cycle digest. **This whole drain-before-flip mechanism
only matters on a single machine/single worker.** If `chat` and `literature` ever run on two
separate machines (see "Multi-machine" below), there's no shared worker to flip — both queues just
run concurrently all the time, and this complexity disappears entirely.

**Force-abort kills the task, not the tool's own error handling.** `revoke(task_id,
terminate=True)` sends a real `SIGTERM` to the worker's one child process — reliable because of
the `prefork` choice above. Cleanup is **not** the dying task's own responsibility (a hard kill
isn't something to build correctness on): the abort command looks up what the job was doing via
its `job_history` row, kills the task, then runs the cleanup itself — deletes any partial
`documents`/`document_chunks` rows and files the job had created, and resets the associated
`literature_candidates` row back to `'relevant'`, so a retry looks like a first attempt, never a
retry-of-a-retry.

**Progress updates stay split along the same line as everything else.** Chat's per-step progress
(`tgi.py`'s `feedback.progress()` call) needs no change — it never left the main process. Document
ingestion's progress calls, once `ingest_and_index_document` runs inside the worker, have no direct
way to reach Telegram anymore (the worker never holds the bot token, deliberately — see below), so
that task publishes progress events to a Redis channel instead; the main process, having already
spawned a background waiter for that job's result, relays those to the actual Telegram edits.

**The worker never touches Telegram, ever — only the main process does.** The Celery task returns
a plain result (via a Redis result backend); the main process awaits it in a fire-and-forget
background `asyncio.create_task` (the same pattern `pipeline._archive_in_background` already uses)
and sends the reply itself, using the `Bot`/`Update`/`Context` access it already has. This keeps
the bot token and all Telegram-sending logic in exactly one place, rather than duplicating
credentials and a second message-sending implementation into the worker.

**Multi-machine scaling falls out of the queue-name abstraction for free — no code changes
needed.** A Celery worker is just a process that connects to Redis over the network; it doesn't
need to share a machine with Redis, the main bot process, or anything else. A second machine (with
its own GPU) can run `celery worker -Q literature` pointed at the same Redis instance
(`CELERY_BROKER_URL=redis://<main-machine>:6379/0`) and start pulling ingestion/literature jobs
immediately — purely a deployment decision (which flags a worker process starts with, where Redis
is reachable from), not something baked into task code. One nuance worth having precisely right:
"exactly one GPU task at a time" really means **one GPU task at a time per physical GPU** — one
machine, one constraint, today; two machines with two GPUs would mean two independent
`concurrency=1` domains, each protecting its own card, which is correct (there's no shared VRAM
between them to race over).

**New state this introduces:**
- **`job_history`** (Postgres) — one row per enqueued job: `job_id`, `job_type`, `celery_task_id`,
  `status` (`queued`/`running`/`done`/`aborted`), timestamps, plus whatever a cleanup needs (e.g.
  `document_id`). Doubles as both the abort-target lookup and an ETA estimator's duration log
  (rolling average per `job_type`) — one table, not two, since both need the same record.
- **`literature_candidates`** (Postgres) — the arXiv/PubMed triage ledger (not detailed here; see
  the literature-pipeline design notes once that section of ROADMAP.md lands).
- **`pipeline._conversations`/`_model_overrides` move out of in-process dicts.** These currently
  live in whichever process's memory created them — fine when one process does everything, broken
  once `webhook_server.py` and the Celery worker are different processes (a dict in one is
  invisible to the other). This is the one piece of genuine state-migration work the chat-side
  change requires, moving to Redis (or a small Postgres table).

## Known gaps / not yet built

- **No daily/scheduled reset** — only idle-timeout and manual `/start`. Not requested; noting it as
  a variant in case idle-only proves insufficient in practice.
- **Real user authentication** — `user_id` is currently just the Telegram user id. The schema and
  every write path already key on it, so swapping in real auth later is a matter of changing where
  that value comes from, not a schema change.
- **No automated tests or CI.**
- **Single-process bot** — no per-chat concurrency limits, rate limiting, or multi-instance
  coordination (the `_conversations` dict is in-process memory, not shared). See "Planned:
  Celery/Redis" above — moving GPU-bound work off this one process is the identified fix, not yet
  built.
- **No sequential-upload throughput story.** Every document upload now makes 5 sequential LLM
  calls + embeddings on top of extraction — correctness-first, but slow uploads stay slow (no
  parallelism, deliberately — see "Document indexing is sequential" above). See "Planned:
  Celery/Redis" above for the identified path.
- **Extracted images/tables aren't tool-accessible.** They're saved (`images/`, `tables/*.csv`
  under each document's folder) for provenance, but no tool reads them — `qwen3:14b` can't consume
  images at all, and no `read_document_table`-style tool exists yet for the CSVs.
- **No chunked/map-reduce summarization for very large documents.** `document_summary_prompt`
  passes the full extracted text to `generate_text` in one call; a document long enough to exceed
  the model's context window (`qwen3:14b`: 40960 tokens) would need a different strategy. Not hit
  in practice yet.
