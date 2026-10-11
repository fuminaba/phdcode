# Roadmap

What's built, what's next, and what's still just an idea. This is meant to be edited directly as
priorities change — it's a working doc, not a spec. See [ARCHITECTURE.md](ARCHITECTURE.md) for how
the finished pieces fit together and [SCRIPTING.md](SCRIPTING.md) for the function-level reference.

## Done

- [x] Structured logging (`utils.setup_logging`) — timestamped files under `LOG_DIR`.
- [x] Tool-calling agent core (`GenerativeService.generate_with_tools`) — model↔tool loop, JSON
      schema output, explicit reasoning trace, post-loop LLM-judge evaluation with bounded retry.
- [x] Tool registry (`@tool` decorator, `tooling/`) — schema auto-generated from type hints +
      docstring; `web_search` (Tavily) as the first tool.
- [x] Conversation memory database — Postgres + pgvector via `docker-compose.yml`, schema for
      `conversations` (summary + embedding) and `conversation_turns` (JSONB backup), swappable via
      `DATABASE_URL`.
- [x] Markdown conversation transcripts — human-readable, source-of-truth log per conversation,
      written concurrently with the JSONB backup, off the async hot path.
- [x] Telegram bot (`webhook_server.py`) — output-only surface, everything else logged/persisted;
      `/start` opens a fresh conversation and archives the old one in the background.
- [x] Summarize + embed (`summarize_and_embed_conversation`) — structured `Topics / Key facts /
      Decisions / Open questions` summary, embedded via `nomic-embed-text`; triggered by `/start`
      (auto, on close) and `/summarize` (manual, on the open conversation).
- [x] `docs/` — architecture + scripting reference.
- [x] `user_id` tracked alongside `chat_id`/`conversation_id` — on both `conversations` and
      `conversation_turns` (denormalized), and in every transcript's filename/header. Stands in
      for real auth today (populated from the Telegram user id); the schema won't need to change
      once real auth exists, just the source of the value.
- [x] `search_memory` / `read_conversation` tools — the reason the summary/embedding pipeline
      exists. `search_memory` embeds the query and does a cosine-distance nearest-neighbor search
      over `conversations.embedding`, scoped to the current user (via `context.current_user_id` —
      never a model-controlled parameter), returning summaries + file paths.
      `read_conversation` reads the full transcript for a returned `file_path` when the summary
      isn't enough, with a path-traversal check as defense in depth against cross-user reads. This
      was the original motivating idea ("make another tool to make the agent go look for specific
      files based on the nature of the convo").
- [x] **Conversation idle timeout.** A conversation left idle for `CONVERSATION_IDLE_TIMEOUT_SECONDS`
      (default 1h) gets auto-archived (summarize + embed, same as `/start`) on the next message
      from that chat, rather than growing forever. Safe now that `search_memory` exists — a related
      follow-up later just finds it again.
- [x] **Live conversation memory.** `handle_message` now fetches the open conversation's recent
      turns (`get_turns`, capped at `MAX_HISTORY_TURNS`) and passes them as `conversation_history`
      to `generate_with_tools` on every message — the model actually remembers earlier turns in the
      *current* chat, not just what it can retrieve via `search_memory` (which only covers
      already-archived conversations). Found while discussing the timeout: this was silently never
      wired up before, so every message was being processed with zero prior context.
- [x] **Memory search hierarchy.** `prompts.MEMORY_SEARCH_PRIORITY_PROMPT`, appended to the system
      prompt whenever `search_memory` is offered: instructs the model to prefer the most recent
      matches and only reach further back for conceptually-related-but-older ones if recent
      matches don't answer the question.
- [x] **Spillover recall within the open conversation.** `MAX_HISTORY_TURNS` was leaving a real
      gap: turns older than the live-history window weren't in context, and `search_memory`
      couldn't find them either since only *archived* (summarized) conversations are embedded —
      the current conversation isn't, until it's closed. Fixed without adding a per-message
      summarize/embed cost: when a conversation has more turns than fit in the live window,
      `handle_message` tells the model its own transcript's file_path
      (`prompts.current_transcript_note`), and it can page back into it directly with
      `read_conversation` — which already worked on any file under the user's directory, archived
      or not, we just hadn't pointed the model at it for its own still-open conversation.
- [x] **Message-ordering bug fix.** `generate_text`/`generate_with_tools` were appending the system
      prompt *after* `conversation_history` instead of before it — dormant until history started
      actually being passed (above), fixed as part of that change.
- [x] **Reusable ingestion pipeline (`llm_engine/pipeline.py`).** Pulled the channel-agnostic half
      of `webhook_server.handle_message` (conversation lookup/lifecycle, live history, agent
      invocation, persistence) out into `handle_incoming_message`/`start_new_conversation`/
      `summarize_open_conversation`, taking plain `user_id`/`chat_id` strings instead of Telegram
      objects. `webhook_server.py` is now a thin adapter over it; a future Django-based interface
      calls the same functions instead of duplicating the logic.
- [x] **Document upload + `read_document` tool (`llm_engine/documents.py`).** PDFs are extracted to
      markdown via `marker-pdf`; the pipeline folds an upload note into the effective prompt text
      (so the reference survives into later turns via `conversation_history`, same mechanism as
      everything else); `read_document` mirrors `read_conversation`'s path-scoped-to-owner security
      model. Wired into Telegram via a `filters.Document.ALL` handler. Images (non-PDF uploads,
      i.e. photos/screenshots) are explicitly out of scope for now — `marker-pdf`'s strength is
      PDF/document → markdown, and the local model (`qwen3:14b`) is text-only anyway.
- [x] **Document chunking, embedding, summarizing, and per-upload folders
      (`llm_engine/memory/document_index.py`, `documents` + `document_chunks` tables,
      `search_documents` tool).** The same two-tier pattern as conversations: `documents` holds a
      whole-document summary + embedding (structured `Topic / Key points / Notable data or
      figures` template, mirroring `summarize_prompt`); `document_chunks` holds fixed-size
      overlapping character chunks (`DOCUMENT_CHUNK_SIZE`/`_OVERLAP` in `.env`, defaults
      `1500`/`200`) each with its own embedding, captured now for future fine-grained retrieval
      (see Backlog — no chunk-level search tool exists yet). `search_documents` (ranks by the
      whole-document embedding) and `read_document` are a matched pair, same shape as
      `search_memory`/`read_conversation`; `prompts.DOCUMENT_SEARCH_GUIDANCE_PROMPT` is appended
      to the system prompt whenever `search_documents` is offered.

      Uploads also moved to **one folder per upload**, named after the new `documents` row's own
      id (`logs/uploads/{user_id}/{conversation_id}/{document_id}/`, `{document_id}/{filename}`
      under a `"standalone"` segment if there's no conversation) — the same two-step
      create-row-then-build-path-then-update pattern already used for conversation transcripts.
      This also made room for **extracted images and tables**: `marker-pdf`'s `images` dict is
      saved to `{folder}/images/`, and markdown tables are regex-extracted and parsed into CSVs
      under `{folder}/tables/` (table-parsing code adapted directly from the reference project's
      `marker_pdf2md.py`) — stored for provenance, not yet exposed to the agent via any tool (see
      Backlog).

      Schema modeled on a prior project's `ResearchArticle`/`ArticleChunk` Django models
      (`~/Desktop/dev/ALiRA/core/document_processor/models.py`) — at this point, kept just the
      "document + chunks" shape; the fuller metadata/summary-flavor fields came next (below).
- [x] **Granular RAG metadata: title/authors/normalized keywords + 3 summary flavors
      (`documents` schema extended, `document_metadata_prompt`/`methods_summary_prompt`/
      `results_summary_prompt`/`conceptual_summary_prompt`, `search_documents` enriched).**
      Explicit ask: RAG over research articles wants more than one summary per document.
      `documents` gained `title`, `authors TEXT[]` (matches the reference project's own
      `ArrayField`, not normalized there either), and three more summary+embedding pairs —
      `methods_summary`, `results_summary`, `conceptual_summary` — alongside the original general
      one, each independently embedded and HNSW-indexed. Keywords *are* normalized/deduped, via a
      `keywords` + `document_keywords` join table (mirroring the reference project's `Keyword`
      model; its `UserKeyword` per-user-search-tracking table was deliberately left out — a
      different feature this project hasn't asked for). Title/authors/keywords come from one
      structured `as_json=True` extraction call (`DOCUMENT_METADATA_SCHEMA`, the same mechanism
      `_judge_output` already used) — the same reuse-what-exists instinct as everywhere else in
      this codebase. `search_documents` now returns all four summary flavors plus
      title/authors/keywords per match, not just the general summary, so the agent can get a
      multi-angle view of a document from one tool call.

      This means 5 more LLM calls + up to 4 more embedding calls per upload (metadata + 3 flavor
      summaries, on top of the general summary and chunk embeddings) — real added latency, accepted
      deliberately since it's exactly what was asked for. **Runs sequentially, not concurrently** —
      an earlier version dispatched all 5 LLM calls via `asyncio.gather`, but since whether the
      remote Ollama server actually parallelizes concurrent requests to the same model (vs. queuing
      them) isn't something this project controls or has verified, unproven concurrency wasn't
      worth the added complexity. Made explicit at the same time: if real parallel GPU throughput
      is ever needed, that's a job for a proper task queue (Redis/Celery, as the reference project
      already uses for its own GPU-bound tasks), not more `asyncio.gather`.
- [x] **`marker-pdf` pinned to `1.10.2`, down from `2.0.0`.** The investigation: `2.0.0`'s
      layout/OCR model (`surya-ocr` 0.22.1+) is a genuine VLM served via a spawned vLLM Docker
      container — not a workaround, a real architecture change (prompted, JSON-schema-guided
      generation, confirmed by reading `surya.layout.LayoutPredictor` and `marker_single`'s own
      source — same code path either way). That cold-starts at ~5-6 minutes *every process start*
      unless the container is kept running permanently (`SURYA_INFERENCE_KEEP_ALIVE=true`), which
      we verified holds ~13GB (81% of this machine's 16GB card) at vLLM's default
      `gpu_memory_utilization=0.85` — tunable down to `0.2` (~2.9GB) without breaking output, but
      still a permanent reservation. That measurably hurt local Ollama throughput: `gpt-oss:20b`
      dropped from a clean 162.85 tok/s (verified via `size_vram`/`size` in `/api/ps`, fully in
      VRAM) to ~68-73 tok/s (≈23% of the model spilled to CPU) whenever marker's server was warm.
      `1.10.2` / `surya-ocr` 0.17.1 predates this shift entirely (confirmed: zero references to
      vllm/docker across its whole source) — direct in-process torch models, ~8-9s per document
      including a one-time Triton JIT compile, zero VRAM held once the call returns. Strictly
      better on every axis that mattered here. Needed a system C compiler (`build-essential`) for
      Triton's JIT; not previously installed.
- [x] **Two latent bugs found and fixed along the way:**
      - `.env`'s `LLM_ROUTING` had a trailing comma (valid Python, invalid JSON) after adding
        `gpt-oss:20b`, so pydantic's strict JSON parse silently failed and `model_routing` fell
        back to `None` instead of `{}` — `get_client()` would `AttributeError` on any call not
        already mocked past it.
      - `GenerativeService.model_routing`'s failure-path default was `None`; changed to `{}` so a
        routing-parse failure degrades to "no routes configured" (falls through to
        `DEFAULT_SERVER_IP`) instead of crashing every `get_client()` call.
- [x] `openai` pinned to `1.109.1` (down from a stale `3.3.1` pin that didn't match what was
      actually installed) — pulled down by `marker-pdf==1.10.2`'s dependency resolution; verified
      with real (non-mocked) `chat.completions`/`embeddings` calls before accepting the pin.
- [x] **`/switch_model` command.** Per-chat model override, stored in-process
      (`pipeline._model_overrides`, keyed by `chat_id`) — in-process only, doesn't survive a bot
      restart. Resolution order in `handle_incoming_message`: explicit `model_name` argument >
      this chat's override > `DEFAULT_MODEL_NAME`. Validates the requested name against
      `GenerativeService.model_routing` (i.e. what's actually in `LLM_ROUTING`) rather than
      accepting any string. Telegram command names can't contain hyphens, so it's `/switch_model`,
      not the originally-suggested `/switch-model`.
- [x] **`read_document` paginated by line range, not flat truncation.** Was a hard cutoff at 8000
      chars with no way to see the rest; now takes `start_line`/`end_line` (1-indexed, inclusive)
      and always states `[lines A-B of N total]` plus the exact `start_line` to continue with if
      there's more — the model can page through a whole paper without tracking state itself. A
      defensive char cap still applies underneath the line cap (`_MAX_LINES_PER_CALL=400`) in case
      a single line is unexpectedly huge.
- [x] **Sentence-/line-aware chunking, and the conceptual summary folded into the general one.**
      Two explicit asks, done together. `chunk_text` (`llm_engine/memory/document_index.py`) no
      longer slices fixed-size character windows blind to structure — it splits text into lines,
      each line into sentences, and greedily accumulates those `(line_number, sentence)` units into
      a chunk up to `chunk_size` characters, so a chunk boundary can only ever land between two
      sentences (or two lines), never mid-sentence. Overlap steps back through the previous chunk's
      trailing units (by approximate character length) rather than re-slicing raw characters. Each
      chunk is now a `ChunkResult(content, start_line, end_line)` (1-indexed, inclusive), persisted
      onto new `document_chunks.start_line`/`end_line` columns — a future chunk-level search tool
      (see Backlog) can point straight back at `read_document(text_path, start_line=..., end_line=...)`
      for full context around a match. Separately, the `conceptual_summary` flavor (its own LLM
      call, embedding column, and HNSW index) was removed and folded into the general summary
      instead: `document_summary_prompt`'s template gained a `Core concepts / theoretical framing`
      section, so `documents.summary` now covers that angle without a fourth summary call. Net
      effect on `ingest_and_index_document`: one fewer LLM call + one fewer embedding call per
      upload (4 LLM calls + 3 embeddings, down from 5 + 4), and `document_chunks` rows now carry
      their source line range. `documents`/`document_chunks` schema migrated on the real dev DB
      (`ALTER TABLE ... DROP COLUMN IF EXISTS conceptual_summary/conceptual_embedding`,
      `ADD COLUMN IF NOT EXISTS start_line/end_line`) and verified against a real PDF — chunking
      produced correct non-overlapping-mid-sentence, properly-line-ranged chunks end to end.
- [x] **`marker-pdf` extraction OOMs against a resident Ollama model — found via a real concurrent
      test, fixed by unloading Ollama first, not by tuning batch sizes.** Started as a request to
      lower marker's surya batch sizes (`MARKER_DETECTION_BATCH_SIZE`/`_LAYOUT_/_RECOGNITION_/
      _TABLE_REC_BATCH_SIZE`, new `.env` vars wired via `ConfigParser` into `documents._get_converter`)
      to shrink the peak VRAM spike from a document upload landing while Ollama is also generating —
      this machine runs both on the same physical GPU (`DEFAULT_SERVER_IP`/`OLLAMA_API_URL` are this
      box's own Tailscale address). A real concurrent test (one Ollama chat turn, then a genuine
      `ingest_and_index_document` call against `~/Downloads/DRAFT_v8_submission_v0.pdf`) surfaced a
      harder wall than expected: with `gpt-oss:20b` idle-resident (~12-14GB of a ~15.5GB-usable
      16GB card), marker's own model loading OOM'd outright (`torch.OutOfMemoryError`) *before* any
      batching happened — batch size only bounds per-forward-pass activation memory, not the fixed
      cost of loading marker's weights, so no batch-size value could have prevented this. Fixed with
      `GenerativeService.unload_all_models()` (`llm_engine/tgi.py`), called right before extraction
      starts: queries Ollama's native `/api/ps`, unloads whatever's loaded via `/api/generate` +
      `keep_alive=0`, polls briefly to confirm the eviction landed. Has to use Ollama's *native* API,
      not the OpenAI-compatible one the rest of `tgi.py` uses — `keep_alive` passed via the OpenAI
      SDK's `extra_body` is silently ignored by Ollama, a confirmed open bug
      ([ollama/ollama#11458](https://github.com/ollama/ollama/issues/11458)). Re-ran the same
      concurrent test with the fix in place: extraction completed cleanly using the full card with
      Ollama unloaded throughout, and a subsequent chat turn reloaded `qwen3:14b` with zero
      spillover (`size_vram == size`) — confirmed end to end, not just unit-tested. Deliberately
      scoped to just-before-extraction rather than a global `OLLAMA_KEEP_ALIVE=0` (server-wide env
      var): the latter would add a full model reload to *every* message, not just ones near an
      upload. The batch-size env vars were kept as a real, working code path — see the next entry
      for why they ended up unset.
- [x] **Batch-size tuning benchmarked (no free lunch), then a bigger, unrelated bug found and fixed:
      Ollama's 4096-token context was silently truncating whole-document LLM calls — fixed with
      map-reduce summarization, not a bigger context window.** Two threads that started separately
      and converged:
      1. *Batch-size benchmark.* Real PDF, repeated runs, SHA-256-hashing extracted output to catch
         silent quality drift. Scaling all four `MARKER_*_BATCH_SIZE` knobs together from 100%→25%
         barely moved time (~59-70s) or VRAM (~11.5-13.5GB, noisy/non-monotonic) — the footprint is
         dominated by fixed model-weight loading, not batch-dependent activation memory. Only an
         aggressive 10% cut showed a real, repeatable win (63.44s/12.74GB → 36.19s/7.29GB, 3 runs
         each, tight variance) — but it also introduced **real** quality loss: dropped citation
         superscripts and an en-dash→hyphen substitution, not just noise. Isolating each knob (cut
         to 25% alone, others at default) showed why: `detection`/`table_rec` can be cut with zero
         quality impact but also zero real benefit (never the bottleneck — text recognition alone
         is ~55s of a ~63-70s run); `recognition` is both the actual bottleneck *and* the only
         source of the quality risk; `layout` was a clear net loss (changed output, ran slower, used
         *more* VRAM in this run). No isolated cut is both safe and worthwhile — all four batch
         sizes left unset.
      2. *The bigger discovery.* Chasing "keep the 10% speedup, patch quality with an LLM cleanup
         pass" led to timing that cleanup call — which failed outright: qwen3:14b returned a
         540-word synthesis of an 8,119-word input instead of the requested full rewrite. Root
         cause, confirmed via `systemctl status ollama`: the actual `llama-server` launch has
         `-c 4096` hardcoded, regardless of what a model architecturally supports (qwen3:14b: up to
         40,960) — and `--context-shift` means overflow is silently dropped, not errored. This is
         almost certainly what caused two earlier-observed bugs: document summaries drifting toward
         just describing a document's tail, and repeated metadata-extraction JSON-parse failures.
         Also incidentally confirmed via that same log: `-np 1` — Ollama really does serve one
         request at a time per model here, validating the earlier "kept sequential, unverified
         whether concurrency helps" decision as simply correct, not just cautious.

         First fix attempted: a per-call `num_ctx` override
         (`GenerativeService.generate_text`/`_native_chat`, routed through Ollama's native
         `/api/chat` since `num_ctx` — like `keep_alive` — isn't honored through the
         OpenAI-compatible endpoint;
         [ollama/ollama#6544](https://github.com/ollama/ollama/issues/6544)/[#7063](https://github.com/ollama/ollama/issues/7063)).
         Also found along the way: Ollama's own memory-fit logic silently *downgrades* a requested
         `num_ctx` if it won't fit (asked for 32768, got 16384) — and a global `OLLAMA_CONTEXT_LENGTH`
         systemd fix was ruled out early since it measured a +4.4GB VRAM cost (9.65GB→14.05GB for
         qwen3:14b) on *every* request, chat included, on a card that's already tight. The per-call
         version correctly stopped the truncation (`n_tokens = 14248, truncated = 0`) but uncovered
         something worse: `journalctl -u ollama` showed live decode at **0.85 tokens/second** with a
         ~14k-token prompt at `num_ctx=32768`, vs. ~77 tok/s at the default context — a ~90x
         slowdown, despite everything fitting in VRAM with room to spare. Likely GQA attention cost
         scaling with a much deeper KV cache, possibly compounded by immature long-context kernels
         on this Blackwell-generation GPU (not fully isolated). A big `num_ctx` for a whole document
         turned out to be a dead end on this hardware.

         Actual fix: **map-reduce**. `ingest_and_index_document` now chunks the extracted text a
         second way (`MAP_CHUNK_SIZE`/`.env`'s `DOCUMENT_MAP_CHUNK_SIZE`, default `8000` — distinct
         from the smaller RAG-retrieval chunk size), digests each chunk individually
         (`prompts.chunk_digest_prompt`) at the cheap default context, then runs the *existing*
         `document_summary_prompt`/`methods_summary_prompt`/`results_summary_prompt` unchanged
         against the concatenated digests — short enough to stay within the default context even for
         a long document. Metadata extraction is scoped to just the first map-chunk instead (title/
         authors need to survive verbatim; a narrative digest could paraphrase them). Verified
         end-to-end against the real test PDF (8 map-chunks, ~181s total): the general summary
         properly follows its template again, methods/results summaries are detailed and accurate,
         title/authors/keywords extracted correctly, and the metadata JSON-parse failures didn't
         recur. `GenerativeService`'s `num_ctx`/native-API capability was kept — correct, tested
         infrastructure, just not the right tool for whole-long-document prompts on this hardware.
- [x] **Channel-agnostic progress/activity feedback (`llm_engine/frontend/`).** A document upload
      takes minutes; a single `send_chat_action("typing")` call only shows for ~5 seconds, so the
      original implementation went silently stale almost immediately on anything longer than that.
      New `frontend/base.py`'s `ChannelFeedback` is a three-method interface
      (`start(label)`/`progress(label, fraction)`/`finish(label=None)`, all no-ops on the base
      class) that `memory/document_index.py` and `pipeline.py` call at each stage boundary —
      `ingest_and_index_document` now reports 8 stages (extraction 35%, RAG indexing 45%, one
      increment per map-reduce chunk digest up to 80%, the 4 reduce/metadata calls to 100%), and
      `pipeline.handle_incoming_message` separately brackets `generate_with_tools` with
      `start("Thinking")`/`finish()`. `frontend/telegram.py`'s `TelegramFeedback` is the only real
      implementation: `start()` sends one message and spawns a background task that re-sends the
      chat action every ~4s (cancelled in `finish()`) so it never goes stale regardless of run
      length; `progress()` edits that message with a percentage; `finish()` edits to a final state
      or deletes, caller's choice (Telegram's ephemeral "Thinking" message gets deleted once the
      real reply lands; the document-processing one gets a final "✅ Finished" edit). Explicitly
      designed so a future Django front end implements the same interface however suits it
      (websocket push, polled status row, ...) with zero changes to `document_index.py`/
      `pipeline.py` — and so importing those channel-agnostic modules never pulls in
      `python-telegram-bot`: `frontend/__init__.py` re-exports only `ChannelFeedback`, not
      `TelegramFeedback`, since a package's `__init__.py` always runs on any submodule import (an
      early version imported `..frontend.base` directly hoping to dodge this, but that still runs
      `frontend/__init__.py` first — confirmed the hard way via `'telegram' in sys.modules` after
      an import that shouldn't have touched it). Verified: unit-level (mocked `Bot` — message
      lifecycle, keep-alive cadence, cancellation, idempotent `finish`) and end-to-end (real test
      PDF through the actual `pipeline.handle_incoming_message` → `ingest_and_index_document` path,
      mocked bot — 14-step progress trail, 58 chat-action refreshes over a ~230s run, no exceptions).
- [x] **Found and fixed in real usage right after the above shipped: long agent replies crashed
      `reply_text` outright.** First live document-summary request through the actual bot hit
      `telegram.error.BadRequest: Message is too long` — Telegram hard-caps `sendMessage` at 4096
      characters with no automatic splitting or truncation of its own. Confirmed the exact cause
      before fixing anything: `persist_turn` runs before the reply is sent, so the failing turn was
      already in `conversation_turns` — queried it directly rather than assuming, and found
      `result["output"]` was 5054 characters (a structured title/authors/summary response after
      being asked to read and summarize an uploaded paper). Fixed with `webhook_server._reply`
      (wraps `_split_for_telegram`, which prefers the last newline before the limit so a chunk
      boundary doesn't land mid-sentence) — used everywhere a reply's length isn't bounded (the
      agent's final answer, `/summarize`'s output); `start`/`switch_model_command`'s short,
      fixed-shape replies still call `reply_text` directly. Re-verified the fix against the actual
      5054-char output pulled from the DB: splits cleanly into 2 messages (3939 + 1114 chars), both
      under the limit.
- [x] **Found in real usage right after the above: a failed turn silently discarded the model's
      partial output and full trace instead of being dumped anywhere.** The very next real message
      ("Can you try that again?") hit `finish_reason=length` — the model's answer got cut off — and
      `_run_tool_loop` raised a bare `RuntimeError` carrying nothing but a string, so the partial
      text it *had* generated, plus the whole tool-call trace, were gone the moment the exception
      was raised; only a traceback made it to the log. New `tgi.ToolLoopError` (a `RuntimeError`
      subclass) carries `.trace`/`.tokens_used`/`.partial_output` from whichever of
      `_run_tool_loop`'s three failure points raised it (truncation, a mid-loop API failure, or
      `max_steps` exhausted). `pipeline.handle_incoming_message` now catches any exception around
      the `generate_with_tools` call (via `getattr(e, ..., default)`, so it degrades gracefully for
      exception types that don't carry this data too) and calls `persist_turn` with a best-effort
      result dict *before* re-raising — same transcript/DB path a successful turn takes, so a failed
      attempt is still visible for debugging instead of vanishing. User-facing behavior is
      unchanged: the exception still propagates and `webhook_server.py` still replies with a
      generic error message. Verified with a forced `finish_reason="length"` mock at both layers:
      `_run_tool_loop` raises `ToolLoopError` with the exact partial text/tokens, and
      `handle_incoming_message` persists that partial text to both the markdown transcript and
      `conversation_turns` (confirmed by reading both back) before the exception reaches the
      caller. Suspected connection to the previous entry (a long conversation's accumulated
      `conversation_history` pushing close to the 4096-token ceiling) — see the next entry for why
      that turned out to be incomplete.
- [x] **Same truncation hit again minutes later, in a *fresh, empty* conversation — ruling out
      accumulated history as the sole cause, and leading to a UX fix instead of just a dump.** The
      user's bot process hadn't been restarted, so none of the previous entry's fixes were live yet
      (`ToolLoopError`, the persist-on-error dump) — the traceback still showed a bare `RuntimeError`
      at a line number that didn't match either the old or new code, a tell for stale bytecode vs.
      an edited-on-disk file (`linecache` shows current source lines against frames from an already-
      loaded module). Separately, measured that tool schemas alone (`llm_engine.tooling.TOOLS`, all
      5 registered tools) cost ~782 tokens, plus ~150 more from `TOOL_REASONING_PROMPT`/
      `MEMORY_SEARCH_PRIORITY_PROMPT`/`DOCUMENT_SEARCH_GUIDANCE_PROMPT` — **~23% of the 4096-token
      budget consumed before the user's message or the model's answer even start**, on every single
      call regardless of conversation length. That's enough on its own to explain hitting the
      ceiling in a brand-new conversation if the model tries to write a long, structured response.
      Given the recurrence, fixed the user-facing symptom directly rather than just the debugging
      gap: truncation (`finish_reason == "length"`) is no longer treated as a failure in
      `_run_tool_loop` — the partial text is returned as a normal successful `raw_output` (with an
      appended `_(response was cut short...)_` note) instead of raising `ToolLoopError`. The user
      now gets a partial-but-real answer instead of a hard error with nothing. `max_steps`-exhausted
      and genuine mid-loop API failures still raise `ToolLoopError` as before (no well-formed answer
      to fall back on for those). Verified: forced `finish_reason="length"` at both the `tgi.py`
      level (returns success, no exception) and the full `pipeline.handle_incoming_message` level
      (turn persists normally, no `"error"` key, unlike the error-dump path) — plus a regression
      check that `max_steps` exhaustion still raises `ToolLoopError` correctly. The root cause (4096
      tokens being tight given fixed tool-schema overhead, independent of history length) is still
      open — bounding `conversation_history` by token budget would help the long-conversation case,
      but wouldn't touch this fresh-conversation one; a bigger `num_ctx` remains a bad trade on this
      hardware (see the map-reduce entry above).
- [x] **Two new tools: `search_document_chunks` and `query_database`, the latter with real
      database-role-level security, not just query validation.** `search_document_chunks(document_id,
      query, max_results=5)` closes the "Chunk-level document search" backlog item below — a
      cosine-distance nearest-neighbor search over one document's chunks, ownership enforced in the
      same query as the search (`JOIN documents d ON ... AND d.user_id = $user`), so a wrong-owner
      `document_id` yields `[]`, identical to one that doesn't exist. `query_database(sql)` is a
      different kind of tool — the model writes real SQL, not a natural-language query that gets
      embedded — so it's built with a correspondingly different security model: a dedicated
      Postgres role, `alira_agent_readonly` (`NOLOGIN`, only ever assumed via `SET LOCAL ROLE`
      inside a transaction that's always rolled back), granted `SELECT` on five user-filtered views
      in a new `agent_readonly` schema and *nothing else* — not the underlying tables, not any
      write or DDL privilege on anything. Client-side validation (single-statement, `SELECT`-only,
      keyword blocklist) runs first as a cheap filter, but the real boundary is verified to be the
      role's grants, not the string check: `SELECT * FROM documents` (the raw table, bypassing the
      views) fails with Postgres's own `permission denied for table documents`. Also verified:
      cross-user isolation (querying as a fake `user_id` against real data returns `[]`), a
      comment-based structural-manipulation attempt (neutralized by wrapping the model's SQL on its
      own line inside the row-limiting subquery), and clean pooled-connection reuse (five readonly
      queries in a row, then a normal query on the same pool still runs with full `alira`
      privileges — no role/session-variable leakage). Chosen over an unrestricted whole-database
      version after discussing the tradeoff explicitly — today's single real user makes that low-risk
      in practice, but per-user scoping avoids a real privacy hole if this bot ever serves a second
      person, at the cost of the views/role setup above.
- [x] **`search_documents`/`search_document_chunks` upgraded to hybrid search — vector + keyword,
      fused by reciprocal rank, not a weighted blend.** Explicit design discussion up front on three
      forks: scope (both tools, not just document-level), whether to use HyDE (generating a
      hypothetical answer via LLM and embedding *that* instead of the raw query — considered and
      rejected: a real LLM call, a few seconds, on every single search wasn't worth it here, so the
      existing fast direct query embedding stayed), and how to do the keyword half (proper Postgres
      full-text search over the actual text, not just the curated `keywords` table — richer, at the
      cost of a schema migration). Shipped: `documents.search_vector`/`document_chunks.search_vector`
      (`GENERATED ALWAYS AS (to_tsvector('english', ...)) STORED` + GIN index — auto-recomputed on
      every `UPDATE`, including `ingest_and_index_document`'s incremental ones, so existing rows
      backfilled automatically with zero migration script needed beyond the `ALTER TABLE ADD
      COLUMN`, confirmed on the real existing document). Both search functions now run the vector
      and text rankings as separate CTEs (`_CANDIDATE_POOL = 20` each) and combine them via
      **reciprocal rank fusion** (`Σ 1/(60 + rank)` per method, `_RRF_K = 60`, the constant from the
      original RRF paper) rather than a weighted score blend, sidestepping the need to normalize
      cosine distance and `ts_rank` onto a comparable scale. Falls back to pure vector ranking when
      a query has no lexical overlap (the text CTE is just empty, contributing `0`) — `relevance` in
      both tools' output is now this fused score, no longer meaning "1.0 = identical" the way raw
      cosine similarity did, documented as such. `search_document_chunks`'s ownership `JOIN` is
      applied in *both* CTEs, not just one, so the security guarantee holds regardless of which
      signal a chunk matches on. Verified against real data, not just by inspection: a query
      matching both signals at rank 1 scores ~2× one matching only one signal (`2×1/61` vs `1/61`,
      exactly per the formula); pure-vector fallback still finds the right document with zero
      lexical overlap; `websearch_to_tsquery` doesn't crash on empty strings or special characters
      (chosen over the stricter `to_tsquery` specifically for this tolerance); cross-user isolation
      still holds through the new query shape.
- [x] **arXiv/PubMed literature search tools**, adapted from a prior project's
      `core/search/services/db_handlers/` (`llm_engine/tooling/_tooling_deps/arxiv_search.py`/
      `pubmed_search.py` — moved here from `llm_engine/` root during a later cleanup pass, since
      they're implementation detail of one tool module, not general infrastructure; copied
      with zero modification — self-contained, `requests` + stdlib only). Two new agent tools,
      `search_arxiv`/`search_pubmed` (`llm_engine/tooling/literature_tools.py`), each building a
      boolean `all_of`/`any_of`/`none_of` query, translating one shared canonical `field` vocabulary
      (`"all"`/`"title"`/`"abstract"`/`"author"`/`"journal"`) into each backend's own field codes via
      `canonical_to_arxiv_field`/`canonical_to_pubmed_field` (already designed upstream for exactly
      this), and returning a trimmed, context-friendly record list. `PUBMED_EMAIL`/`PUBMED_API_KEY`
      (both optional) read from `.env`, not model-controlled. Verified against the live APIs, not
      mocked: real boolean queries against both returned real, on-topic papers (MIL/pathology
      results from both arXiv and PubMed), and the no-`all_of`/`any_of` error path returns a plain
      string rather than raising. This is the groundwork for the iterative literature-research loop
      discussed alongside it — see the next entry for how that loop actually got built.
- [x] **The iterative literature-research loop: self-extending step budget, lightweight bookmarks,
      live progress, and a real context-overflow bug found and fixed mid-build.** Three design forks
      settled explicitly before building: bookmarks reuse the existing `documents` table (unified
      search with uploads) rather than a new one, distinguished by an explicit `document_kind`
      column (`'upload'`/`'bookmark'`, chosen over a bare boolean for future extensibility) rather
      than inferring it from `dir_path`/`text_path` being `NULL`; loop continuation is a dedicated
      `continue_research(reason)` tool call (not just a bigger fixed `max_steps`), capped by a hard
      admin ceiling it can't override; live visibility reuses `ChannelFeedback` (one `progress()`
      call per tool-loop step) rather than rearchitecting the markdown transcript to write
      incrementally.

      **`save_finding`** (`llm_engine/tooling/literature_tools.py`, backed by `memory.db.
      save_literature_finding`) saves an arXiv/PubMed hit as a `documents` row with
      `document_kind='bookmark'` — title/authors/abstract-as-`summary`/embedding, no `dir_path`/
      `text_path`/chunks. New `source_url`/`external_ref`/`doi` columns carry bookmark provenance;
      `external_ref` (`"{src_db}:{db_id}"`) is a partial-unique-indexed dedup key (`ON CONFLICT
      (user_id, external_ref) WHERE external_ref IS NOT NULL DO UPDATE`) — saving the same paper
      twice updates the existing bookmark, verified directly (same `document_id` both times, exactly
      one row). `search_documents` now also returns `document_kind`/`source_url`/`doi` so the agent
      knows a bookmark has no full text to fall back on.

      **`continue_research`** (`llm_engine/tooling/control_tools.py`) is registered the normal way
      but intercepted by name in `tgi._run_tool_loop`, before normal `TOOL_FUNCTIONS` dispatch — its
      own function body never runs. `_run_tool_loop`'s fixed `for _ in range(max_steps)` became a
      `while steps_taken < steps_budget` loop; calling the tool grows `steps_budget` by
      `_CONTINUE_RESEARCH_STEP_INCREMENT` (`5`), capped at `hard_max_steps` (`.env`'s
      `AGENT_MAX_STEPS`/`AGENT_HARD_MAX_STEPS`, defaults `5`/`20`) — the model can ask for more room,
      never override the ceiling. Deliberately generic, not gated to literature tools. Verified with
      forced mock sequences: a budget of 2 that calls `continue_research` first extends to 7 and
      completes; a request already at the hard ceiling is correctly denied and the loop still raises
      `ToolLoopError` as before; the no-`continue_research`/no-`feedback` path is unchanged (pure
      regression check).

      **Live progress** reuses `ChannelFeedback` exactly as built for document-ingestion progress —
      no transcript rearchitecture. `_run_tool_loop` calls `feedback.progress(f"Step {n}: {tools}",
      steps_taken / steps_budget)` once per step; `pipeline.py`'s existing `feedback.start("Thinking")`
      before the loop means the per-step calls just edit that same Telegram message.
      `hard_max_steps`/`feedback` now flow from `pipeline.py` (`AGENT_MAX_STEPS`/
      `AGENT_HARD_MAX_STEPS` from `.env`) through `generate_with_tools` into `_run_tool_loop`.

      **Then a real end-to-end run exposed a genuine bug, not a success** — the kind of thing only
      an actual live test catches: asking the agent to search arXiv and save a finding truncated to
      an *empty* response after just one `search_arxiv` call. Measured why rather than guessing:
      `search_arxiv`'s original full-record result list (then up to 10 hits, each with full
      `abstract_text`/`hyperlink`/`doi`/etc.) serialized to **~3,900 tokens — ~95% of this
      deployment's 4096-token context budget, by itself**, before the prompt, system prompt, or tool
      schemas (now bigger still with 11 tools registered). Considered shrinking `max_results` as the
      fix; went with a better one instead (the user's own suggestion, weighing it against a
      stateless alternative before building): split `search_arxiv`/`search_pubmed` into an overview
      only (`db_id`/`src_db`/`title`/`authors`/`published_date`/`venue`, ~790 tokens for 10 results)
      plus a new `read_paper(src_db, db_id)` for the rest (abstract, etc., ~400 tokens), fetched only
      for papers actually worth inspecting. `read_paper` is stateless, not a cache — it re-queries
      the live API for just one paper via each module's *existing* `raw_query` escape hatch
      (`id:{db_id}` for arXiv — `"id"` was already in its `VALID_FIELDS`; `{db_id}[uid]` for
      PubMed's exact-PMID search tag) — zero code changes needed to either search module, and zero
      new memory-management surface (no cache eviction, no staleness) in exchange for one extra
      ~1s API call per lookup. Re-ran the *exact* scenario that failed before the split: `search_arxiv`
      → `read_paper` → `save_finding` completed cleanly in 10.5s, correct final answer, no
      truncation, a correctly-saved bookmark, and a live `20% → 40% → 60%` progress trail — verified
      against the real APIs and a real local model, not mocked.
- [x] **`gpt-oss:20b` switched to a real 65536-token context via a new Ollama model tag, after a
      real benchmarking investigation found a meaningfully different result than either a secondhand
      claim or the project's own prior (qwen3:14b) experience would have predicted.** Started from a
      user-heard claim ("Mistral-NeMo:12b is as smart as gpt-oss:20b with 64k context on 16GB") —
      checked rather than trusted: current benchmark aggregators report gpt-oss:20b meaningfully
      ahead on average score, and context-window capacity turned out to be an architecture/training
      property (gpt-oss:20b is *also* rated ~131k tokens) with no relationship to parameter count —
      if anything, gpt-oss:20b's bigger total weights (MoE: all experts resident in VRAM regardless
      of the ~4B active per token) compete harder for the same fixed VRAM than a smaller dense model
      would, the opposite of what "bigger model, more context" would suggest.

      **The KV-cache-per-token formula was validated against a real measurement before trusting it
      for anything else**: `num_kv_heads × (key_dim + value_dim) × 2 bytes × num_layers`, checked
      against qwen3:14b's actual measured VRAM delta from earlier this session (+4.4GB for
      4096→32768 tokens) — predicted 163,840 bytes/token, measured implied ~164,800 — under 1% off.
      Applied the same formula (exact architecture read live from Ollama's own `/api/show`, not an
      aggregator) to gpt-oss:20b: 24 layers, 8 KV heads, 64-dim — ~49KB/token, *cheaper* per-token
      than qwen3:14b despite being the bigger model (fewer, narrower attention layers; MoE routing
      doesn't touch the KV cache at all).

      **The actual safe ceiling was found by binary search against real VRAM, not by trusting the
      calculation** — a real, important gap: the calculation (assuming a ~15GB usable budget)
      predicted ~117,000 tokens would fit; real testing found 65536 (64k) fits fully resident
      (11.92GB, zero spillover) but 73728 (72k) already spills to CPU. Actual usable VRAM on this
      card is closer to ~12GB than the assumed 15-16GB. `gpt-oss-20b-48k` and `gpt-oss-20b-64k`
      (Ollama tags, `FROM gpt-oss:20b` + `PARAMETER num_ctx ...` — no re-download, no code changes,
      works through the existing OpenAI-compatible endpoint) both created and tested this way.

      **Decode-speed-vs-depth was mapped as a real curve (8 points, 530→43,242 tokens), not
      inferred from one number** — flat at 143-173 tok/s throughout, no qwen3:14b-style cliff in
      this range (plausibly because of the same narrower/fewer-layer attention). A single early
      data point (47 tok/s at ~17k tokens) that didn't fit this curve at all was correctly
      identified as an outlier, most likely GPU contention from something else running at that
      moment, not trusted over the controlled, internally-consistent sweep.

      **Three further, deliberately controlled comparisons, each in triplicate (per explicit
      instruction, after one set of triplicate measurements came back with physically-impossible
      numbers from reusing an identical prompt and triggering Ollama's own prompt-cache reuse —
      fixed by using different content per run), settled the real question cleanly**: (1) same
      depth (~48.6k tokens), 48k-ceiling vs 64k-ceiling tags — 141.66 vs 141.65 tok/s, identical;
      (2) each tag at half of its *own* ceiling (~24.3k vs ~32.8k tokens) — both landed almost
      exactly on the already-measured depth curve at those respective depths, confirming "half-full"
      isn't a distinct effect, just depth; (3) the 64k tag at the *48k* tag's depth (~24.3-24.5k
      tokens both) — 158.7 vs 153.3 tok/s, within normal run-to-run noise. **Conclusion, now
      confirmed from multiple independent angles: the configured ceiling itself has no effect on
      speed — only actual tokens-in-use does** — so there's no speed reason to prefer the smaller
      ceiling. Separately found and corrected for: a real ~24% cold-start penalty on the first
      inference after any fresh model load (fixed in later tests with an explicit untimed warm-up
      call first) — relevant because `document_index.py`'s `unload_all_models()` already forces
      this cost onto the chat reply immediately following any document upload.

      **Landed on `gpt-oss-20b-64k`** as the bot's `MODEL_NAME`/`DEFAULT_MODEL_NAME`, targeting
      ~60,000 usable tokens (real margin below the 65,536 ceiling, not cutting it close).
      `MAX_HISTORY_CHARS` raised from `3000` to `190000` accordingly — budget worked out as: ~3,205
      tokens tool schemas (measured directly, 14 tools — up from ~2,780 at 12, confirming schema
      cost scales with tool count as tools keep getting added) + a generous ~9,000-token reserve
      (system prompt additions, this turn's own tool-exchange before the decide/digest/decide loop
      collapses it, the new message, and real room for the model's response) = ~12,000 fixed
      overhead, leaving ~48,000 tokens for history, converted at a conservative 4 chars/token (not
      the ~4.15 measured on clean markdown, since history can carry less token-efficient content).

      Verified end-to-end through the real pipeline, not just at the Ollama API level: `gpt-oss-
      20b-64k` confirmed as the model actually used (11.92GB resident, matching exactly) by a real
      `pipeline.handle_incoming_message` call with no explicit `model_name` — though via
      `GenerativeService.get_client`'s `DEFAULT_SERVER_IP` fallback, not `LLM_ROUTING`'s new
      explicit entry, since **neither `webhook_server.py` nor the Celery worker has been restarted
      since this `.env` change** — both still hold the routing table from before `gpt-oss-20b-64k`/
      `gpt-oss-20b-48k` were added. Chat works right now only because `DEFAULT_SERVER_IP` happens
      to point at the same server; `/switch_model` would not yet recognize either new tag as valid
      until `webhook_server.py` restarts, and `call_model` is relying on a fallback rather than the
      intended explicit route until the worker does too. Both restarts still owed.

## Next up

Implementing the Celery/Redis design in `docs/ARCHITECTURE.md`'s "Planned: Celery/Redis" section,
in this order:

- [x] Dedupe `CONTINUE_RESEARCH_TOOL` — see "Done" below.
- [x] **Redis + a single Celery worker, proven end to end.** `docker-compose.yml` gained a `redis`
      service (`redis:7-alpine`, `--appendonly yes` for persistence across restarts, its own named
      volume). New `llm_engine/tasks/` package: `celery_app.py` (the `Celery` app, Redis as both
      broker and result backend, `worker_prefetch_multiplier=1` so the worker never grabs jobs
      ahead of time — see ARCHITECTURE.md for why that matters for the mode-switch below) and
      `diagnostics.py` (`alira.ping`, a standing health-check task, not just a throwaway proof).
      Verified twice, for real, not just "it imported cleanly": a worker (`--concurrency=1
      --pool=prefork -Q chat,literature`) picked up and correctly executed a dispatched `ping`,
      round-tripping through the result backend; separately, `app.control.cancel_consumer`/
      `add_consumer` were exercised directly — a task dispatched to `literature` while that queue
      was cancelled sat unconsumed (`ready() == False` after 3s) and completed the instant
      consumption resumed — confirming the exact mechanism the chat/literature priority switch
      depends on actually behaves as designed, before anything is built on top of it.
- [x] **Document ingestion moved into a Celery task, found and fixed a real Celery/marker-pdf
      conflict along the way.** `llm_engine/tasks/ingestion_tasks.py`: `run_ingestion` (the worker
      task, `asyncio.run(ingest_and_index_document(...))`, unmodified internally, exactly as
      predicted) and `dispatch_ingestion` (the main-process counterpart — dispatches, relays the
      worker's progress back through a real `ChannelFeedback` via a Redis pub/sub channel
      subscribed to *before* dispatch so an early event can't be lost, awaits the result via
      `asyncio.to_thread`). New `llm_engine/frontend/redis_relay.py`'s `RedisRelayFeedback`
      publishes `start`/`progress`/`finish` as JSON events — the worker never imports
      `python-telegram-bot` or touches Telegram at all, per the design. `content` (PDF bytes)
      travels as base64 in the task's args rather than a shared file path, specifically so this
      stays correct if ingestion's worker ever runs on a different machine than the one that
      received the upload (see "Multi-machine scaling" above) — no shared filesystem assumed.
      `pipeline.handle_incoming_message` now calls `dispatch_ingestion(..., queue="chat")` instead
      of `ingest_and_index_document` directly, so a live chat upload is never stuck behind a
      literature-processing batch; `webhook_server.py` needed zero changes.

      **A real run under the actual worker surfaced a genuine bug, not a success on the first
      try:** `AssertionError: daemonic processes are not allowed to have children`, from deep
      inside `marker-pdf` — traced to `pdftext` (marker's PDF text backend), which defaults to 4
      internal workers and spins up a `ProcessPoolExecutor` for per-page extraction. A Celery
      `prefork` worker's child process is marked daemonic by Python's `multiprocessing`, which
      forbids it from spawning children of its own — so any document long enough to actually
      trigger `pdftext`'s worker path would fail under Celery specifically, a failure mode the
      original (non-Celery) benchmarking never could have hit. Fixed via the same `ConfigParser`
      mechanism `documents.py` already used for the `MARKER_*_BATCH_SIZE` knobs: `pdftext_workers`
      forced to `1` unconditionally (confirmed in `pdftext`'s own source: `workers <= 1` skips the
      process pool entirely and runs inline) — there's no throughput to gain from pdftext's own
      internal parallelism once extraction itself only ever runs one at a time anyway, serialized
      through the same queue this would otherwise conflict with. Re-ran the exact real scenario
      end to end against the actual test PDF, not mocked: the full 14-step progress trail arrived
      correctly through the Redis relay, and the result was verified directly against Postgres and
      disk afterward (not just trusted) — correct title extracted, a properly-templated summary,
      51 real chunks, the extracted markdown/images/tables actually on disk. Test data cleaned up
      after.
- [ ] Build the literature pipeline on top (`literature_candidates`, triage, `pull_pdf`,
      abort-with-rollback, the end-of-cycle digest).
- [x] **Chat's `call_model` moved into Celery — the narrow boundary, verified, not just the whole
      loop reasoned about in theory.** New `llm_engine/tasks/chat_tasks.py`'s `call_model` resolves
      its own client (`GenerativeService.get_client(model_name)` — an `OpenAI` client instance
      can't cross a process boundary) and returns the raw response as a plain dict
      (`response.model_dump()`), translating any client-side failure into a bare `RuntimeError`
      first since `openai`'s own exception types may not reconstruct cleanly through Celery's
      JSON-based result serialization. `tgi._run_tool_loop` dropped its `client: OpenAI` parameter
      entirely (nothing inside it calls the client directly anymore) and now dispatches
      `call_model.apply_async(..., queue="chat").get(timeout=600)` once per loop iteration, reading
      the response via dict access instead of attribute access; its `except` clause now also
      catches `RuntimeError`/Celery's own `TimeoutError` alongside the original `APIError`, same
      `ToolLoopError` contract as before. Tool dispatch, `current_user_id`, `run_on_main_loop`, and
      `generate_with_tools`'s surrounding code are **completely unmodified** — exactly as designed,
      since they never needed to leave this process in the first place.

      Verified with a real multi-step run (not mocked): `generate_with_tools("Search the web for
      today's weather in Vancouver, BC...")` against a real worker produced a genuine 3-step
      tool-calling loop — `web_search` called, a hallucinated `browser.run` call correctly handled
      by the pre-existing unknown-tool fallback (unrelated to this migration, just exercised by
      it), `web_search` called again, then a correct final answer — with live progress updates
      (`Step 1: web_search (20%)`, etc.) firing correctly through the **unchanged**
      `run_on_main_loop` bridge the whole time. First test run caught a real gap in the test
      itself, not the code: calling `generate_with_tools` from a bare script with no
      `set_main_loop()` ever called reproduced the exact "Main event loop not set" error
      `run_on_main_loop` is supposed to raise in that situation — confirming the bridge's own
      unchanged behavior, once the test was fixed to mirror how `webhook_server.py` actually sets
      it up.
- [ ] Hybrid search for `search_conversations` (and any future search tool, as a standing rule).
- [x] **`GenerativeService._native_chat`/`_native_base_url` deleted, along with `generate_text`'s
      `num_ctx` parameter.** Re-litigated deliberately, not an oversight: kept earlier as "correct,
      tested infrastructure... just not the right tool for whole-long-document prompts on this
      hardware" (see the map-reduce entry above), but confirmed via a fresh grep that zero live
      callers anywhere pass a real `num_ctx` — and the standing design goal going forward is to
      never need a bigger context window at all, not just "didn't work for this one case." Also
      removed `generate_text`'s now-unreachable `except requests.RequestException` handler
      (`_native_chat` was the only thing in that method that could ever raise it — the OpenAI SDK
      doesn't). `unload_all_models` is unaffected; it talks to `OLLAMA_API_URL` directly, never
      through `_native_base_url`. Caught a genuine, unrelated syntax error while verifying the
      import afterward — `print(test_response))`, one extra closing paren in the `__main__` block
      at the bottom of `tgi.py`, most likely from an editor auto-format earlier in the session —
      fixed in the same pass since it had broken the whole module's importability.
- [x] **Live, granular activity log (`turn_actions`), distinct from the once-per-turn `conversation_turns`
      record.** New table: insert-only, multiple rows per turn, `turn_id` (a plain `uuid4`, minted at
      the top of `pipeline.handle_incoming_message`, before `conversation_turns`' own row exists)
      groups everything that happened during one turn — `turn_start`, one `llm_call` row per
      `call_model` round trip, one `tool_call` row per individual tool execution (not grouped by
      loop step), `pipeline_start`/`pipeline_step`/`pipeline_end` for a nested multi-stage
      sub-process (document ingestion today), `turn_end` — queryable as `WHERE turn_id = $1 ORDER BY
      created_at` to replay the whole story. `conversation_turns` gained one new column, `turn_id`,
      linking a finished turn back to its own detailed history; its shape/timing is otherwise
      completely unchanged. Errors fold into whichever `_end` event is the nearest enclosing
      boundary (`status: 'success'|'error'` + message) rather than a separate type — the same
      `except` blocks that already existed (around `generate_with_tools` in `pipeline.py`, around
      `ingest_and_index_document`'s body) just also write this now.

      `trace` is gone entirely — not just moved. `_run_tool_loop`'s in-memory `trace`/`step`
      bookkeeping and `ToolLoopError.trace` are deleted outright: grepped every reader first and
      confirmed the only consumer anywhere was markdown formatting, and `turn_actions` already gives
      a *better* version of what `ToolLoopError.trace` existed for (surviving even a hard process
      crash, not just a caught exception, since each row commits the instant it happens).

      Two different write paths for two different contexts, both reusing existing mechanisms — no
      new bridging invented: `tgi.py`'s new `_log_action()` (sync, runs inside the worker thread
      `_run_tool_loop` already runs in) goes through the same `run_on_main_loop` bridge
      `feedback.progress()` already used, guarding against that bridge itself failing in addition to
      the write failing; `llm_engine/frontend/logging_feedback.py`'s new `LoggingFeedback` (async,
      wraps a real `ChannelFeedback` and logs on every `start()`/`progress()`/`finish()` before
      delegating unchanged) is what gives ingestion its `pipeline_*` rows with **zero changes to
      `document_index.py`** — wrapped once in `pipeline.py` right before the attachment loop.
      `memory.db.write_turn_action` itself is unconditionally best-effort (catches and logs rather
      than raising) — a live-log write going wrong must never break the actual turn or pipeline it's
      describing.

      Verified twice, for real, against the actual dev DB — not just import-checked: a live
      web-search chat turn produced exactly 5 rows in the right order (`turn_start` → `llm_call`
      [tool_calls] → `tool_call` [web_search, full arguments+result] → `llm_call` [truncated] →
      `turn_end`, `status: 'success'` even though truncated, consistent with the existing
      graceful-degradation design); a real document upload through the same turn produced 21 rows —
      `turn_start`, the full ingestion pipeline (`pipeline_start` + all 14 of its real
      `feedback.progress()` stages, in order, with the exact labels/fractions `document_index.py`
      already used + `pipeline_end`), then the chat reply's own `llm_call`/`tool_call`
      (`read_document`)/`llm_call`/`turn_end` — all under the one `turn_id`, `conversation_turns`
      correctly linked to it. `ON DELETE CASCADE` from `conversations` confirmed actually cleaning
      up `turn_actions` rows too when test data was removed afterward, not just assumed from the
      schema.
- [x] **`arxiv_search.py`/`pubmed_search.py` moved to `llm_engine/tooling/_tooling_deps/`.**
      Codebase reorganization, not a behavior change: confirmed via grep that `tooling.
      literature_tools` is the *only* thing in the whole codebase that imports either module —
      unlike `embeddings.py`/`documents.py`/`context.py`, which genuinely are shared across
      multiple unrelated parts of the codebase and stay at `llm_engine/` root. `_tooling_deps/` is
      now the place for a tool's own private helper scripts, distinct from general infrastructure.
      Both the package-relative import and the standalone-script bare-fallback path (dotted as
      `tooling._tooling_deps.arxiv_search`, matching the existing `tgi.py`/`control_tools.py`
      precedent for reaching two levels into a sibling package) verified working after the move,
      not just the former.
- [x] **`live_viewer.py` — a simple, read-only browser view of `turn_actions` as it fills in.**
      New FastAPI app at the repo root (alongside `webhook_server.py`, same import/logging
      conventions), run standalone (`uvicorn live_viewer:app --host 0.0.0.0 --port
      $LIVE_VIEWER_PORT`) — entirely separate process from the bot/worker, reads the DB directly.
      One endpoint, `GET /api/actions?since_id=&limit=`, backed by new `memory.db.
      get_recent_turn_actions`; the page itself is a single inlined HTML/CSS/JS string (no build
      step, no frontend framework) that polls it every ~1.5s and renders turns as cards — grouped
      by `turn_id`, newest first, each action line styled by `action_type`, the card's own badge
      flips from "running" to success/error once `turn_end` arrives. Polling chosen deliberately
      over a websocket/push: `turn_actions` already gets a row the instant something happens, so
      a short poll interval is already near-real-time for this purpose at a fraction of the
      complexity. No authentication — intended for the same Tailscale network everything else
      here already sits on, not public exposure.

      Verified against the real DB, not just loaded in isolation: `/api/actions?since_id=0` and a
      fresh `since_id=<last seen id>` fetch were both checked against a real chat turn's rows,
      confirming the incremental-fetch contract the frontend depends on actually holds (already-
      seen ids never come back, only strictly-newer ones do) — and that JSON serialization of the
      `UUID`/`timestamptz`/`JSONB` columns round-trips cleanly through FastAPI. `fastapi`/`uvicorn`
      needed no new installs -- both were already in `pyproject.toml`'s dependency list.
- [x] **Chased down repeated `finish_reason=length` truncation to two real, measured causes, and
      fixed both.** Tool schemas alone now cost ~2,780 tokens (12 registered tools) — ~68% of the
      4096-token window, before anything else — up from ~930 tokens (~23%) when last measured with
      5 tools; schema overhead scales with tool count, so it only gets tighter as more tools are
      added. On top of that, `web_search` (`tooling/websearch.py`) had a real, independent bug:
      it ignored its own `max_results`/`search_depth` parameters (hardcoded `max_results=10,
      search_depth="basic"` inside the call regardless of what was passed in) and returned every
      result's full, uncapped `content` — confirmed directly in a live test, one call's result ran
      to several KB of raw scraped page text, fed straight back into the same tight window for the
      model's next call.

      Fixed both, deliberately as LLM digestion rather than character truncation (chosen explicitly
      over a cheaper char-cap: truncation cuts wherever the count lands, which can discard the
      actually-relevant part of a page — a digest keeps the gist regardless of where it falls):
      `web_search` now respects its own parameters, and any result over `_DIGEST_THRESHOLD_CHARS`
      (600) gets condensed via its own `call_model` dispatch (`prompts.websearch_result_digest_prompt`)
      before being returned — short results pass through unchanged, no wasted round trip. Routed
      through the same "chat" Celery queue as everything else, deliberately, not a direct Ollama
      call: a direct call from inside a tool would let it run concurrently with whatever else is
      using the GPU, quietly reopening the exact race condition the whole Celery migration exists
      to close.

      Separately, `tgi._run_tool_loop` now splits each tool-using round into two model calls
      instead of one: a decide call (tools attached, unchanged), then — once those tools have run —
      a digest call with **no tool schemas attached at all**, condensing the raw tool output into a
      short note that then *replaces* it in `messages` before the next decide call (the same
      map-reduce instinct already used for document ingestion, applied to tool output instead of
      document text). Costs one extra round trip per tool-using step — accepted deliberately, since
      the Celery worker is on the same machine and the context squeeze was judged the more pressing
      problem. A real test run caught a genuine bug in the first version of this, not a success on
      the first try: collapsing the exchange into a bare `role="assistant"` message left the
      conversation ending on its own prior turn, with nothing to respond to — confirmed directly (a
      real run came back `finish_reason="stop"` with empty content right after). Fixed by following
      the pattern this codebase already uses for exactly this (`eval_retry_feedback`): the collapsed
      digest is a `role="user"` message instead, explicitly asking the model to continue. Re-ran the
      same real scenario after the fix — correct, complete, untruncated final answer, and the full
      `turn_actions` trace for that turn (decide → tool_call, respecting its own params → digest →
      decide → turn_end) confirmed the whole shape working exactly as designed.

      **Also found, mid-verification, unrelated to the fix itself: three separate Celery workers
      were running simultaneously** (one ad-hoc one from an earlier stuck-message fix, never
      stopped; one the user started themselves in a `tmux` session; one started to test this very
      change) — each independently enforcing its own concurrency=1, so the system's actual effective
      concurrency was 3, silently defeating the whole point of the Celery migration. Resolved down
      to one (confirmed which was the user's intentional one before stopping the others, rather than
      guessing). **Operational takeaway, not yet automated: exactly one worker must be running
      whenever the bot is, and it's the user's tmux-managed one specifically** — restarting it is
      also what's needed to pick up any `tgi.py`/`tooling/` change (the digest-loop code itself
      runs in the main process via `pipeline.py`, not the worker, so it didn't need this — but
      `call_model`/`run_ingestion` and everything the worker actually executes does).
- [x] **Long conversations were truncating too — `conversation_history` was capped by turn
      *count* (`MAX_HISTORY_TURNS`), which never actually bounded how many tokens it cost.** A
      handful of turns with big (even post-digest) answers could already eat most of the window by
      themselves; the turn count was never the real constraint. Fixed at the root: `pipeline.
      _turns_to_history` now walks backward from the most recent turn and stops once a character
      budget (`MAX_HISTORY_CHARS`, default `3000`, replacing `MAX_HISTORY_TURNS` entirely) is
      spent, always keeping at least the single most recent turn even if it alone exceeds the
      budget. Returns `(history, included_count)` now, not just `history`.

      Older turns aren't lost, just not kept in context by default — two new tools give the model a
      way to look them up on demand, the same two-tier shape `search_documents`/
      `search_document_chunks` already use: **`recall_conversation_history(skip_recent, limit)`**
      (new `memory.db.get_older_turns`) pulls older turns' plain `user_prompt`/`output` straight
      from `conversation_turns` — the common case, "what did we discuss back then"; **`recall_turn_
      detail(turn_id)`** (new `memory.db.get_turn_actions_for_turn`, ownership-checked by `user_id`
      since the model supplies `turn_id` itself, picked from a prior `recall_conversation_history`
      result) drills into one specific turn's full action-by-action trace via `turn_actions`, for
      when the short answer alone isn't enough — e.g. needing to see exactly what a tool returned,
      not just the conclusion drawn from it. A new `context.current_conversation_id` `ContextVar`
      (same never-model-settable pattern as `current_user_id`) scopes the first tool to the live
      conversation; `prompts.recall_history_note` replaces `current_transcript_note` as the
      system-prompt hint — instead of pointing at a markdown file path, it tells the model exactly
      how many turns it can currently see and what `skip_recent` to pass for the rest, read from the
      DB rather than a file (a deliberate step in the user's own stated direction of moving off
      markdown as the source of truth).

      Verified end-to-end against a real seeded conversation, not just the trimming logic in
      isolation: 14 turns, one early one containing a made-up fact (a deliberately distinctive
      invented word, to rule out a lucky guess) no longer in the live window by design, asked a
      question only that turn could answer. The real `turn_actions` trace confirmed it worked for
      the right reason, not luck — `recall_conversation_history(skip_recent=8, limit=20)`, using the
      exact count the system prompt supplied, found the right turn, digested it, and answered
      correctly. `.env`'s `MAX_HISTORY_TURNS` and every doc reference to it (`ARCHITECTURE.md`,
      `SCRIPTING.md`) updated to `MAX_HISTORY_CHARS` — old `ROADMAP.md` "Done" entries describing
      the prior behavior left as-is, as the historical record of what was true then.

## Backlog / open questions

Things flagged along the way but not yet scoped or committed to:

- [ ] **`search_memory`/`search_conversations` upgraded to hybrid search, matching
      `search_documents`/`search_document_chunks`.** Currently pure vector (cosine distance over
      `conversations.embedding`) — no keyword/full-text signal, unlike the document search tools.
      Explicit user stance: *any* search tool in this project should be hybrid (vector + full-text
      via RRF), not just the document ones — `search_conversations` is the one holdout. Needs a
      `conversations.search_vector` generated column (over `summary`, same `tsvector`/GIN pattern)
      plus the same RRF-fusion rewrite `search_documents` already went through. Apply the same
      standard to any future search tool from the start, rather than adding hybrid search as a
      follow-up each time.
- [ ] **Daily/scheduled reset**, as opposed to purely idle-timeout-based. Not requested — idle
      timeout was judged sufficient — but worth remembering as a variant if idle-only turns out to
      leave conversations open too long in practice.
- [ ] **Citation-backed answers.** `prompts.LOOSE_RAG_PROMPT` exists but nothing wires it in yet —
      looks like groundwork for making `search_memory` (or `web_search`) results show up as cited
      sources in the final answer, rather than just silently informing it.
- [ ] **Tests / CI.** Nothing automated exists yet; verification so far has been manual
      (mocked-client scripts, live smoke tests against the real bot).
- [ ] **Multi-chat / concurrency limits.** `_conversations` is in-process memory with no rate
      limiting or per-chat isolation guarantees beyond what asyncio gives for free.
- [ ] **Evaluation-on-by-default?** `evaluate=True` (the LLM-judge retry loop) is currently opt-in
      per call and off by default in the bot, since it doubles round trips per message. Revisit if
      answer quality becomes a problem.
- [x] ~~Chunk-level document search~~ — **done.** `search_document_chunks(document_id, query,
      max_results=5)` (`llm_engine/tooling/document_tools.py`, backed by `memory.db`'s function of
      the same name) does a cosine-distance nearest-neighbor search over one document's
      `document_chunks`, returning each match's `content`/`start_line`/`end_line`/`relevance` — the
      drill-deeper counterpart to `search_documents`, letting the agent point straight at
      `read_document(..., start_line=..., end_line=...)` for full context around a match, per the
      plan below. Ownership enforced in the same query as the search (`JOIN documents d ON d.id =
      dc.document_id WHERE dc.document_id = $1 AND d.user_id = $2`), not a separate pre-check — a
      `document_id` that exists but belongs to someone else yields `[]`, identical to one that
      doesn't exist at all. Verified against a real document with a real query (sensible,
      on-topic matches) and a real cross-user check (querying a real `document_id` as a different
      `user_id` returns `[]`, not that user's chunks).
- [ ] **Table-reading tool.** Extracted tables are saved as CSVs per upload but nothing reads them
      back — could extend `read_document` or add a dedicated tool once needed.
- [x] ~~Chunked/map-reduce document summarization~~ — **done**, see the "Ollama's 4096-token context
      was silently truncating whole-document LLM calls" entry above. `document_summary_prompt`/
      `methods_summary_prompt`/`results_summary_prompt` now run against concatenated per-chunk
      digests, not the full extracted text.
- [ ] **Upload latency.** Each upload now makes a variable number of map-step LLM calls (one per
      `MAP_CHUNK_SIZE`-sized chunk) + 4 reduce/metadata calls + up to 3 embedding calls on
      top of extraction and chunk embedding — noticeably slower than before the metadata/summary-
      flavor work. Accepted deliberately for now (see the sequential-vs-concurrent decision above).
      `unload_all_models()` adds a bit more: whatever's currently loaded gets unloaded right before
      extraction, and reloads on the summary/metadata calls a few steps later — a real added cost
      on shared-GPU hardware, accepted since it's what prevents the extraction OOM (see above). If
      this becomes a real problem: a proper task queue (Redis/Celery, matching the reference
      project's own GPU-task setup) is the identified path, not more `asyncio` concurrency against
      a server whose parallelism we can't confirm.
- [ ] **LLM cleanup pass on extracted content.** `marker-pdf`'s OCR/extraction output goes straight
      into chunking/summarizing/storage as-is — no cleanup step for OCR artifacts (garbled
      characters, misaligned table columns, broken formatting). The reference project had this for
      tables specifically (`_clean_table_chunked` in `marker_pdf2md.py`: a cheap model,
      `qwen2.5-coder:3b`, cleans one table at a time in row-group chunks — chunking matters there
      because dumping a whole table into a small model at once makes it summarize/condense instead
      of just fixing formatting). Worth revisiting once OCR quality issues actually show up in
      practice, for tables and/or the general extracted text.
- [ ] **Document-type-aware processing profiles ("semantic toolsets").** Every upload currently
      gets the full research-article treatment — chunking, general summary, title/authors/keyword
      extraction, and both flavor summaries (methods/results) — regardless of what it actually is.
      A course syllabus, invoice, or letter doesn't have "results" or "methodology"
      to summarize; forcing that template on it produces a strained, low-value answer for no
      benefit, and costs the same 5 LLM calls + embeddings as a real paper. The idea: a
      classification step (cheap heuristic or a quick LLM call) picks which processing steps
      actually run for a given upload — a "research article" profile gets the full flavor-summary
      + chunking treatment, a "simple document" profile gets just the general summary, and other
      profiles could exist for other document shapes. Directly addresses the "Upload latency" item
      above by skipping unneeded work rather than just accepting it. Naming note: this is a
      distinct concept from the `@tool`/`TOOLS` agent-tool registry (`llm_engine/tooling/`) despite
      the similar name — these would be *document-processing* profiles selected at ingestion time,
      not tools the model calls at chat time. Needs a real name that doesn't collide before this
      gets built.

## Ideas (unscoped)

Raised during discussion, not yet designed:

- More tools beyond `web_search` (the registry pattern supports adding these cheaply).
- Deployment story for `webhook_server.py` beyond "run it in a terminal" (systemd service? restart
  policy? currently must be started manually and conversations don't survive a restart).
- Redis/Celery-based task queue for document ingestion, if/when upload throughput or multi-user
  GPU contention actually becomes a problem — user has said they'll set this up themselves if
  needed, rather than build it speculatively now.
- **Document management interface** — let the user view all `documents` rows (uploads + bookmarks)
  and delete any they no longer want. Raised while designing the literature pipeline's
  candidate-ledger expiry: clearing a stale `literature_candidates` ledger deliberately never
  deletes an already-saved document (see the pipeline design notes/ARCHITECTURE.md once that
  lands) — this would be the actual user-facing deletion path, once wanted. Out of scope for the
  current build.
