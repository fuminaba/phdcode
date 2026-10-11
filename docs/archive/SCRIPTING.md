# Scripting Reference

Function-by-function reference for every module in the project. See [ARCHITECTURE.md](ARCHITECTURE.md)
for how these pieces fit together.

Conventions used below:
- `async` next to a signature means it must be awaited.
- "Internal" means it's not part of the module's intended public surface (leading underscore),
  documented here anyway because it's where most of the actual logic lives.

---

## `utils.py`

### `setup_logging(script_name: str, level: int = logging.INFO) -> logging.Logger`

Configures Python's root logger to write to both a timestamped file and stdout/stderr, and
returns a logger named `script_name`.

- **Args:** `script_name` — used both as the returned logger's name and as part of the log
  filename. `level` — logging level, default `INFO`.
- **Returns:** a `logging.Logger`.
- **Side effects:** creates `LOG_DIR` (from `.env`, default `"logs"`) if missing; creates a file
  named `{LOG_DIR}/{YYYY-MM-DD %Hh-%Mm} {script_name}.log`; calls `logging.basicConfig` (global,
  so call this once per process — a second call elsewhere won't reconfigure handlers).
- **Example:** `logger = setup_logging("webhook_server")`.

---

## `llm_engine/tgi.py` — `GenerativeService`

A classmethod-only service wrapping an OpenAI-compatible client (pointed at Ollama by default).
No instances are created; all state (`_client`, `model_routing`) is on the class.

### `GenerativeService.get_client(model_name: str = "qwen3:14b") -> OpenAI`

Returns a sync `OpenAI` client configured for `model_name`, looked up in `model_routing` (parsed
from the `LLM_ROUTING` env var). Falls back to `DEFAULT_SERVER_IP` + a dummy API key if the model
isn't in the routing table.

- **Note:** creates a *new* client on every call (models can live on different servers) — don't
  call this in a hot loop expecting a cached client.

### `GenerativeService.unload_all_models(wait_timeout: float = 5.0) -> None`

Frees GPU VRAM immediately by telling Ollama to unload every currently-loaded model
(`keep_alive=0`), instead of waiting out its default 5-minute idle timeout. Called by
`memory.document_index.ingest_and_index_document` right before `marker-pdf` extraction starts — on
hardware where Ollama and the extraction process share one GPU, a resident model can leave too
little VRAM for marker to even load its own weights (a real, verified `torch.OutOfMemoryError`, not
theoretical — see ARCHITECTURE.md), independent of any batch-size tuning.

- Queries Ollama's *native* `/api/ps` (`OLLAMA_API_URL`, no `/v1` suffix) and, for each loaded
  model, `POST`s `/api/generate` with `{"model": name, "keep_alive": 0}`. Deliberately bypasses the
  OpenAI-compatible endpoint the rest of this class uses — passing `keep_alive` via the OpenAI
  SDK's `extra_body` is silently ignored by Ollama
  ([ollama/ollama#11458](https://github.com/ollama/ollama/issues/11458)).
- Polls `/api/ps` for up to `wait_timeout` seconds so the caller's next GPU-heavy step doesn't race
  the actual eviction; returns as soon as the model list is empty, or once the timeout elapses.
- Best-effort: swallows request failures (logs a warning) rather than raising — a model that fails
  to unload just reloads on the next real request either way, same as if this was never called.
- No-op if nothing is currently loaded.

### `GenerativeService._native_base_url(model_name: str) -> str` *(internal)*

Ollama's native API base (no `/v1` suffix) for whichever server `model_name` is routed to —
mirrors `get_client()`'s own routing lookup, so a native-API call targets the same host a regular
OpenAI-compatible call for that model would.

### `GenerativeService._native_chat(model_name: str, messages: list[dict], num_ctx: int) -> tuple[str, int]` *(internal)*

Calls Ollama's native `/api/chat` directly (`requests.post`), bypassing the OpenAI-compatible
client — the only way to actually set `num_ctx` (confirmed:
[ollama/ollama#6544](https://github.com/ollama/ollama/issues/6544)/[#7063](https://github.com/ollama/ollama/issues/7063)).
Returns `(content, tokens_used)` — `tokens_used` is `prompt_eval_count + eval_count` from the
native response, for parity with `generate_text`'s usual `tokens_used`.

### `GenerativeService.generate_text(prompt, system_prompt=None, conversation_history=None, response_schema=None, as_json=False, model_name="qwen3:14b", num_ctx=None) -> dict[str, Any]`

One-shot (no tools) text generation.

- **Args:**
  - `prompt` — the user message.
  - `system_prompt` — optional system message; if `as_json`, a JSON-schema instruction is
    appended to it automatically.
  - `conversation_history` — optional list of prior `{"role", "content"}` messages, inserted
    between the system message and the new prompt (i.e. `[system, *conversation_history, user]`).
  - `response_schema` — dict of `{"field": (type, default_or_...)}`, pydantic-`create_model`
    style. `...` means required. Only used when `as_json=True`.
  - `as_json` — if `True`, instructs the model to reply as JSON matching `response_schema`, then
    parses/validates the response through a pydantic model built from that schema.
  - `model_name` — passed to `get_client`.
  - `num_ctx` — if set, routes this call through `_native_chat` (Ollama's native `/api/chat`)
    instead of the OpenAI-compatible client, requesting this context window instead of Ollama's
    default (4096 tokens on this deployment). Costs real VRAM proportional to `num_ctx` for as long
    as the model stays loaded at that size, and — measured directly, not theoretical — can cause a
    catastrophic decode-speed cliff at real document lengths (~0.85 tok/s at `num_ctx=32768` with a
    ~14k-token prompt, vs. ~77 tok/s at the default context). Prefer chunking + reducing over
    per-chunk digests (see `memory.document_index.ingest_and_index_document`) over reaching for a
    large `num_ctx` on a whole long document. `None` (default) leaves existing behavior unchanged.
- **Returns:** `{"output": str | dict, "tokens_used": int}`. `output` is a `dict` only when
  `as_json=True` and parsing succeeded.
- **Raises:** `RuntimeError` on API failure, or if JSON/schema validation fails (logs the error
  first, via `log.error`).
- **Example:**
  ```python
  GenerativeService.generate_text(
      "What is the capital of France?",
      response_schema={"capital": (str, ...), "year": (int, ...)},
      as_json=True,
  )
  # -> {"output": {"capital": "Paris", "year": 987}, "tokens_used": 42}
  ```

### `GenerativeService._run_tool_loop(client, messages, tools, model_name, max_steps, hard_max_steps=None, feedback=None) -> dict[str, Any]` *(internal)*

Drives the model↔tool loop to a final answer, **mutating `messages` in place** as it appends
assistant/tool messages. Used by `generate_with_tools` and re-invoked directly for eval retries.

- **The step budget isn't fixed at `max_steps`.** It's a `while steps_taken < steps_budget` loop,
  `steps_budget` starting at `max_steps`; calling the `continue_research` tool (see
  `tooling/control_tools.py`) mid-loop grows it by `_CONTINUE_RESEARCH_STEP_INCREMENT` (`5`), capped
  at `hard_max_steps` (defaults to `max_steps`, i.e. no extension allowed, if not given).
  `continue_research` is intercepted by name (`CONTINUE_RESEARCH_TOOL` constant) *before* normal
  `TOOL_FUNCTIONS` dispatch — its own registered function body never runs; the result injected back
  to the model is `{"granted": True, "new_step_budget": N}` or `{"granted": False, "reason": ...}`,
  computed here directly.
- `feedback` (see `frontend/base.py`), if given, gets one `progress(label, fraction)` call per step
  — `label` names whichever tools were called that step (or `"reasoning"` if none), `fraction` is
  `steps_taken / steps_budget` recomputed each step (so it can move backward right after a
  `continue_research` grant — an honest signal that more work was just added, not a bug). Defaults
  to a no-op `ChannelFeedback()`. Dispatched via `run_on_main_loop` (`.context`), same
  sync-tool-in-worker-thread bridge the tools themselves use — `_run_tool_loop` runs inside
  `asyncio.to_thread` via `generate_with_tools`, same as any tool function.
- **Returns:** `{"raw_output": str, "tokens_used": int, "trace": list[dict]}`. Each `trace` entry
  is `{"thought": str | None, "actions": [{"tool": str, "arguments": dict, "result": Any}, ...]}`.
- **Truncation (`finish_reason == "length"`) is not a failure.** The model did produce a partial
  answer, just not a complete one, so it's returned as a normal success — `raw_output` is the
  partial text plus an appended `_(response was cut short — it exceeded the model's output
  limit)_` note — rather than raising. Changed after this happened live: a fresh, empty
  conversation still hit it, since tool schemas + reasoning/guidance system prompts alone consume
  ~930 tokens (~23%) of this deployment's 4096-token context before the user's message or the
  model's answer even start, and the old behavior was a hard failure with no reply at all.
- **Raises:** `ToolLoopError` (a `RuntimeError` subclass) on a genuine mid-loop API failure, or if
  the step budget (`steps_budget`, see above) is exhausted without a tool-call-free response —
  cases with no well-formed answer to fall back on. Always carries whatever `.trace`/`.tokens_used`
  exist at that point, and `.partial_output` (the last message's content, if any) for the
  budget-exhausted case. Added after a real
  truncation in production silently discarded the model's partial answer and the whole trace (back
  when truncation still raised) — `pipeline.handle_incoming_message` catches this (via `getattr`,
  so it degrades gracefully for any other exception type too) and persists a best-effort turn
  before re-raising, so a failed attempt still shows up in the transcript/DB instead of vanishing.

### `GenerativeService._judge_output(original_prompt, output, model_name) -> dict[str, Any]` *(internal)*

Scores `output` against `original_prompt` using `prompts.judge_prompt` and `schemas.JUDGE_SCHEMA`
via `generate_text(..., as_json=True)`.

- **Returns:** `generate_text`'s return shape — `{"output": {"passed": bool, "score": int,
  "feedback": str}, "tokens_used": int}`.

### `GenerativeService.generate_with_tools(prompt, tools=TOOLS, system_prompt=None, conversation_history=None, response_schema=None, as_json=False, model_name="qwen3:14b", max_steps=5, hard_max_steps=None, feedback=None, explain_reasoning=True, evaluate=False, eval_model_name=None, max_eval_retries=1) -> dict[str, Any]`

The main agent entry point: runs the model with function-calling enabled, dispatching any
`tool_calls` through `TOOL_FUNCTIONS` and feeding results back until a final text answer comes
back (or the step budget is exhausted — see `_run_tool_loop`, including how the model can extend
that budget itself via `continue_research`).

- **Args (beyond `generate_text`'s):**
  - `tools` — OpenAI-style tool schema list; defaults to the full registered index
    (`llm_engine.tooling.TOOLS`). Pass a filtered subset to restrict which tools are offered.
  - `max_steps` — starting cap on model↔tool round trips **per attempt** (an eval retry gets a
    fresh budget, back to `max_steps` — extensions from a previous attempt don't carry over).
  - `hard_max_steps` — ceiling the step budget can't exceed, however many times the model calls
    `continue_research`. `pipeline.py` sets this from `.env`'s `AGENT_MAX_STEPS`/
    `AGENT_HARD_MAX_STEPS` (defaults `5`/`20`); left as `None` here, `_run_tool_loop` falls back to
    `max_steps` itself (no extension allowed).
  - `feedback` — forwarded straight to `_run_tool_loop` (see its entry above for what it does with
    it). Defaults to a no-op.
  - `explain_reasoning` — if `True` (default), appends `prompts.TOOL_REASONING_PROMPT` so the
    model states which tool it's using and why before each call. That reasoning is captured into
    the returned `trace`, not shown to the end user.
  - `evaluate` — if `True`, runs `_judge_output` after the loop finishes. A failing verdict
    (`passed: False`) feeds `prompts.eval_retry_feedback(feedback)` back into the conversation
    and re-runs the tool loop, up to `max_eval_retries` times. The last attempt is always
    returned, even if it never passes.
  - `eval_model_name` — model used for judging; defaults to `model_name`.
- **Also appends** `prompts.MEMORY_SEARCH_PRIORITY_PROMPT`, `prompts.DOCUMENT_SEARCH_GUIDANCE_PROMPT`,
  and/or `prompts.LITERATURE_RESEARCH_PROMPT` to the system prompt automatically, whenever
  `"search_memory"`/`"search_documents"`/`"search_arxiv"` or `"search_pubmed"` (respectively) is one
  of the names in `tools` (not gated by `explain_reasoning` — these are a separate concern: how to
  weigh/cite search results or run an iterative investigation, not whether to narrate tool choices).
- **Returns:**
  ```python
  {
      "output": str | dict,        # dict only if as_json=True
      "tokens_used": int,          # summed across every attempt + every judge call
      "trace": list[dict],         # one entry per tool-calling step, across all attempts
      "evaluation": dict | None,   # last judge verdict, or None if evaluate=False
  }
  ```
- **Raises:** `RuntimeError` — API failure, JSON/schema validation failure, truncated output, or
  `max_steps` exhausted on any single attempt.
- **Threading note:** synchronous/blocking. Call via `asyncio.to_thread(...)` from async code
  (see `webhook_server.py`).

---

## `llm_engine/prompts.py`

All prompt text lives here so it isn't duplicated across `tgi.py`/`memory/summarize.py`.

| Name | Kind | Purpose |
|---|---|---|
| `TOOL_REASONING_PROMPT` | `str` constant | Appended to the system prompt when `explain_reasoning=True`, asking the model to narrate tool choices. |
| `MEMORY_SEARCH_PRIORITY_PROMPT` | `str` constant | Appended whenever `search_memory` is offered as a tool: instructs the model to prefer recent matches over older, merely conceptually-related ones. |
| `DOCUMENT_SEARCH_GUIDANCE_PROMPT` | `str` constant | Appended whenever `search_documents` is offered: instructs the model to prefer the most relevant document match and cite which document (by filename) an answer came from. |
| `LITERATURE_RESEARCH_PROMPT` | `str` constant | Appended whenever `search_arxiv`/`search_pubmed` is offered: frames literature search as iterative (tweak keywords and re-search rather than settling for a weak first attempt), points at `read_paper` for full detail before judging/saving a specific paper, encourages saving useful finds as they're found (not just at the end) via `save_finding`, and names `continue_research` as the way to ask for more room mid-investigation. |
| `LOOSE_RAG_PROMPT` | `str` constant | *(currently unused by any caller — reserved for a future retrieval/citation flow.)* |
| `json_schema_instruction(response_schema: dict) -> str` | function | The "reply as JSON matching this schema" instruction text, shared by `generate_text` and `generate_with_tools`. |
| `judge_prompt(original_prompt: str, output: str) -> str` | function | Builds the grading prompt for `_judge_output`. |
| `eval_retry_feedback(feedback: str) -> str` | function | Builds the "please revise" message appended to the conversation on a failing eval. |
| `summarize_prompt(conversation_text: str) -> str` | function | Builds the summarization prompt, instructing the exact `Topics / Key facts / Decisions / Open questions` template used by `summarize_and_embed_conversation`. |
| `chunk_digest_prompt(chunk_text: str) -> str` | function | Builds the map-step prompt for one map-reduce chunk: a short, dense digest (topic, methodology, results) of just that excerpt, explicitly told to say so rather than pad if the excerpt has none of those (e.g. a references-only chunk). Used by `ingest_and_index_document`; the digests are concatenated and fed to the prompts below. |
| `document_summary_prompt(document_text: str) -> str` | function | Builds the general document summarization prompt, instructing the `Topic / Key points / Core concepts / theoretical framing / Notable data or figures` template used by `ingest_and_index_document`. The "core concepts" section absorbs what used to be a separate conceptual-summary flavor/call. Despite the parameter name, `ingest_and_index_document` now passes the concatenated chunk digests, not the raw document text (see map-reduce summarization in ARCHITECTURE.md). |
| `document_metadata_prompt(document_text: str) -> str` | function | Builds the prompt for structured title/authors/keywords extraction (used with `as_json=True` + `schemas.DOCUMENT_METADATA_SCHEMA`). |
| `methods_summary_prompt(document_text: str) -> str` | function | Builds the prompt for the methodology-focused summary flavor. Receives the concatenated chunk digests from `ingest_and_index_document`, not the raw document text — see `document_summary_prompt` above. |
| `results_summary_prompt(document_text: str) -> str` | function | Builds the prompt for the results/findings-focused summary flavor. Same digest-text caveat as `methods_summary_prompt`. |
| `recall_history_note(included_count: int) -> str` | function | Tells the model exactly how many of the most recent turns it can currently see, and to call `recall_conversation_history(skip_recent=included_count)` for anything older — pulled from `conversation_turns` via the DB, not a file. Passed as `system_prompt` by `pipeline.handle_incoming_message`, only when `_turns_to_history` actually left turns out (`included_count < len(turns)`). |
| `document_note(filename: str, text_path: str) -> str` | function | Tells the model a document was just uploaded, its filename, and the path to its extracted text — and that `read_document` reads it. Folded into the effective prompt text by `pipeline.handle_incoming_message` (not just a system-prompt aside), so it survives into later turns via `conversation_history`. |

---

## `llm_engine/schemas.py`

### `JUDGE_SCHEMA`

`{"passed": (bool, ...), "score": (int, ...), "feedback": (str, ...)}` — the `response_schema`
passed to `generate_text` by `_judge_output`. Score is expected 1–10 (not enforced, just prompted).

### `DOCUMENT_METADATA_SCHEMA`

`{"title": (str, ...), "authors": (list[str], ...), "keywords": (list[str], ...)}` — the
`response_schema` passed to `generate_text` by `ingest_and_index_document`'s metadata-extraction
call. `authors`/`keywords` are expected `[]` (not omitted) when the document doesn't have them.

---

## `llm_engine/context.py`

Cross-thread state shared between `webhook_server.py` and the memory tools, kept out of the model's
control (never a tool parameter).

### `current_user_id: ContextVar[Optional[str]]`

Set by the caller (e.g. `handle_message`, right before `generate_with_tools`) to identify who's
asking. Read by `search_memory`/`read_conversation` to scope results to that user. A `ContextVar`
rather than a global — each concurrent request gets its own value (propagates correctly across
`asyncio.to_thread`, via `contextvars.copy_context()`), so concurrent messages from different
chats never leak into each other's tool calls.

### `set_main_loop(loop: asyncio.AbstractEventLoop) -> None`

Call once from inside the app's main event loop at startup (`webhook_server._on_startup`), so
`run_on_main_loop` has somewhere to dispatch to.

### `run_on_main_loop(coro: Coroutine) -> Any`

Runs `coro` on the main event loop and blocks for its result. Needed because `@tool` functions are
synchronous and run inside a worker thread (`generate_with_tools` is invoked via
`asyncio.to_thread`), but the asyncpg connection pool is bound to the main loop — a plain
`asyncio.run(...)` inside the worker thread would create a second pool on a second loop, which
asyncpg doesn't allow mixing. `asyncio.run_coroutine_threadsafe` is the correct bridge.

- **Raises:** `RuntimeError` if called before `set_main_loop`.

---

## `llm_engine/embeddings.py`

### `embed(text: str, model_name: Optional[str] = None) -> list[float]`

Embeds `text` via the OpenAI-compatible `/v1/embeddings` endpoint.

- **Args:** `model_name` — defaults to `EMBEDDING_MODEL` env var (`"nomic-embed-text"`).
- **Returns:** a list of floats. **Must** match `EMBEDDING_DIM` (`.env`) / the `embedding` column
  width in `schema.sql` (currently both `768`) — mismatched dimensions will fail the Postgres
  insert, not this function.
- **Blocking:** synchronous HTTP call; wrap in `asyncio.to_thread` from async code.

---

## `llm_engine/documents.py`

Document (PDF) ingestion via `marker-pdf` (pinned to `1.10.2` — see ARCHITECTURE.md for why).

### `Attachment(filename: str, content: bytes)` / `ExtractedDocument(filename: str, raw_path: Path, text_path: Path, image_paths: list[Path], table_paths: list[Path])`

Plain `NamedTuple`s. `Attachment` is what a channel adapter constructs from raw upload bytes;
`ExtractedDocument` is what `ingest_document` returns — `image_paths`/`table_paths` are `[]` when
the document has none.

### `_get_converter()` *(internal)*

Lazily builds and module-level-caches a `marker.converters.pdf.PdfConverter`. Reused across calls
*within one process* — but unlike the abandoned vLLM-backed approach this replaced, there's no
persistent server to keep warm across process restarts: each fresh process just reloads the
(now-cached-on-disk) model weights directly, taking ~8-9s including a one-time Triton JIT kernel
compile, and holds zero GPU memory once a call returns.

Reads `MARKER_DETECTION_BATCH_SIZE`/`MARKER_LAYOUT_BATCH_SIZE`/`MARKER_RECOGNITION_BATCH_SIZE`/
`MARKER_TABLE_REC_BATCH_SIZE` from `.env` (`_BATCH_SIZE_ENV_TO_CONFIG_KEY`), builds a
`marker.config.parser.ConfigParser` config dict from whichever are actually set, and passes it as
`PdfConverter(config=...)` — unset ones fall through to surya's own CUDA defaults (36/32/256/32).
Note this only bounds *activation* memory during a batched forward pass; it does not by itself
prevent a resident Ollama model from starving marker's own weight-loading of VRAM (see
`ingest_and_index_document` / `GenerativeService.unload_all_models` for the actual fix to that, and
ARCHITECTURE.md for the verified failure mode).

### `_extract(path: Path) -> tuple[str, dict]` *(internal)*

Runs the converter, returns `(markdown_text, images)` — `images` maps filename → `PIL.Image`,
whatever marker-pdf pulled out of the PDF (figures, embedded pictures), `{}` if none.

### `_markdown_table_to_dataframe(markdown_table: str) -> polars.DataFrame` *(internal)*

Parses one markdown table (header row, `---` separator, data rows) into a polars DataFrame.
Adapted directly from a prior project's marker-pdf pipeline
(`~/Desktop/dev/ALiRA/core/document_processor/services/marker_pdf2md.py`).

### `_extract_tables(markdown_text: str) -> list[str]` *(internal)*

Regex-finds runs of consecutive `|...|` lines in the extracted markdown — each match is one
table's raw markdown text, fed to `_markdown_table_to_dataframe`.

### `ingest_document(dir_path: Path, filename: str, content: bytes) -> ExtractedDocument`

Saves the raw upload to `dir_path/{filename}`, runs it through marker-pdf, and saves:
- extracted markdown to `dir_path/{filename}.md`
- any embedded images to `dir_path/images/{name}`
- any detected tables, as CSVs, to `dir_path/tables/{i}.csv` (a table that fails to parse — e.g.
  OCR-mangled — is silently skipped rather than failing the whole upload)

`dir_path` is created by the caller (see `memory/document_index.py`), named after a `documents`
row's own id, so two uploads never collide even with the same filename.

- **Blocking, CPU/GPU-heavy** (~8-9s typical) — call via `asyncio.to_thread` from async code (see
  `memory.document_index.ingest_and_index_document`).
- Requires a working C compiler on `PATH` (Triton JIT-compiles a kernel on first use per process;
  `build-essential` on Ubuntu). Without one, this raises `RuntimeError: Failed to find C compiler`.

---

## `llm_engine/memory/document_index.py`

Document indexing orchestration: extraction, chunking, embedding, summarizing, persistence — the
document analog of `memory/summarize.py`. `CHUNK_SIZE`/`CHUNK_OVERLAP` read from `.env`
(`DOCUMENT_CHUNK_SIZE`/`DOCUMENT_CHUNK_OVERLAP`, defaults `1500`/`200`) at import time — this is the
small, RAG-retrieval-granularity chunk size. `MAP_CHUNK_SIZE` (`.env`'s `DOCUMENT_MAP_CHUNK_SIZE`,
default `8000`) is a second, larger chunk size used only for map-reduce summarization (see
`ingest_and_index_document` below) — same `chunk_text` function, different `chunk_size` argument.
`MAP_CHUNK_OVERLAP` is a fixed `300`, not `.env`-configurable (an internal implementation detail of
the map-reduce pipeline, not something worth exposing the way RAG chunk size/overlap is).

### `class ChunkResult(NamedTuple)`

`content: str`, `start_line: int`, `end_line: int` (both 1-indexed, inclusive) — one chunk plus
where in the source text it came from. Returned by `chunk_text`, consumed by `save_document_chunks`.

### `chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[ChunkResult]`

Splits `text` into overlapping chunks that only ever break at a sentence or line boundary — never
mid-sentence. Internally: splits `text` into lines, splits each line into sentences
(`re.split(r'(?<=[.!?])\s+', ...)`), giving a flat ordered list of `(line_number, sentence)` units;
greedily accumulates units into a chunk up to `chunk_size` characters (a single unit longer than
`chunk_size` is still taken whole, never truncated); steps back through the previous chunk's
trailing units — by their approximate character length, not a raw slice — to re-include roughly
`overlap` characters of trailing context in the next chunk. `chunk_size`/`overlap` are targets, not
hard character caps — actual chunk length varies since it always lands on a sentence/line boundary.
Returns `[]` for empty/whitespace-only text.

### `ingest_and_index_document(user_id: str, conversation_id: Optional[int], filename: str, content: bytes, model_name: Optional[str] = None, feedback: Optional[ChannelFeedback] = None) -> tuple[int, str]` *(async)*

The full RAG-oriented pipeline for one uploaded document. Everything below runs **sequentially,
one call at a time** — deliberately not concurrent (see ARCHITECTURE.md's "Document indexing is
sequential, not concurrent, by choice" for why). The actual stage work is delegated to the internal
`_run_ingestion` helper, wrapped by `feedback.start()`/`finish()` (see below) — split out so
`ingest_and_index_document` itself can report `finish("❌ Failed to process {filename}")` on any
exception and re-raise, versus `finish("✅ Finished processing {filename}")` on success, rather than
a single `finally` that couldn't tell the two apart:

1. `create_document` — registers the `documents` row, returns its id.
2. Builds `dir_path` from that id (`{UPLOADS_DIR}/{user_id}/{conversation_id or "standalone"}/{id}/`),
   calls `GenerativeService.unload_all_models()` off-thread to clear GPU VRAM for marker-pdf (see
   ARCHITECTURE.md — a resident Ollama model can otherwise starve marker's own weight-loading of
   VRAM), then calls `ingest_document` off-thread. → `feedback.progress("Extracted text", 0.35)`.
3. `set_document_paths` — writes `dir_path`/`text_path` onto the row.
4. `chunk_text` the extracted text at RAG-retrieval granularity (`CHUNK_SIZE`), `embed()` each
   chunk (one at a time), and `save_document_chunks` in one bulk insert. →
   `feedback.progress("Indexed for search", 0.45)`.
5. **Map:** `chunk_text` the same extracted text again, this time at the larger `MAP_CHUNK_SIZE` —
   distinct chunking pass, distinct purpose. For each resulting chunk, `generate_text` +
   `prompts.chunk_digest_prompt` at the default (cheap) context, collecting one short digest per
   chunk, reporting `feedback.progress(f"Digesting section {i+1}/{n}", ...)` after each one (spread
   linearly from 0.45 to 0.80). Necessary because Ollama's default context window (4096 tokens on
   this deployment) would otherwise silently truncate a whole-document prompt — see ARCHITECTURE.md
   for why a large `num_ctx` was tried instead and rejected (a ~90x decode-speed cliff at real
   document lengths). The digests are joined into one `digest_text` (short enough to stay within the
   default context even for a long document).
6. **Reduce:** general summary: `generate_text` + `prompts.document_summary_prompt(digest_text)` →
   `embed()` → `update_document_summary` → `feedback.progress("Generated summary", 0.85)`.
7. Metadata: `generate_text(..., as_json=True, response_schema=DOCUMENT_METADATA_SCHEMA)` +
   `prompts.document_metadata_prompt` — on just the *first* map-chunk's raw text, not the digests
   (title/authors need to survive verbatim; a narrative digest could paraphrase them) — →
   `update_document_metadata` (title/authors) + `add_document_keywords` (keywords, normalized) →
   `feedback.progress("Extracted metadata", 0.90)`.
8. **Reduce:** methods summary: `generate_text` + `prompts.methods_summary_prompt(digest_text)` →
   `embed()` → `update_document_methods_summary` → `feedback.progress("Summarized methodology", 0.95)`.
9. **Reduce:** results summary: same shape, `prompts.results_summary_prompt(digest_text)` →
   `update_document_results_summary` → `feedback.progress("Summarized results", 1.0)`.
10. Returns `(document_id, note)` — `note` is `prompts.document_note(filename, text_path)`, meant
    to be folded into the caller's effective prompt text (see `pipeline.handle_incoming_message`).

- `conversation_id=None` is valid (a document not tied to any conversation) — the folder uses the
  literal segment `"standalone"` instead of `str(None)`.
- **`feedback`:** defaults to a no-op `ChannelFeedback()` if omitted (see `frontend/base.py`). The
  percentages above are rough weight estimates from one real document's timing (extraction 35%, RAG
  indexing 10%, map digests 35% split evenly across chunks, the 4 reduce/metadata calls 5% each),
  not precisely measured per stage — expect drift for very different document shapes.
- LLM call count now varies with document length: one map call per `MAP_CHUNK_SIZE`-sized chunk,
  plus the 4 reduce calls (3 summaries + 1 metadata extraction, general summary also covering
  conceptual/theoretical framing) plus 3 embedding calls, on top of extraction and RAG-chunk
  embedding. Verified end-to-end against a real ~56K-character paper (8 map-chunks): ~181s total,
  correct template-following summary, correct methods/results content, correct title/authors, no
  truncation, no decode-speed cliff.

---

## `llm_engine/pipeline.py`

Channel-agnostic message-ingestion pipeline — conversation lifecycle, attachment ingestion, agent
invocation, and persistence. `webhook_server.py` (Telegram) is a thin adapter over this; a future
channel would call these same functions instead of duplicating them. See ARCHITECTURE.md's
"Channel-agnostic core, thin adapters" for the rationale.

Module-level state: `_conversations: dict[str, tuple[ConversationTranscript, int, float]]` (keyed
by `chat_id`, value is `(transcript, conversation_id, last_active_monotonic)`) and
`_background_tasks: set[asyncio.Task]` (keeps fire-and-forget archive tasks alive until they
finish). `DEFAULT_MODEL_NAME`, `CONVERSATION_IDLE_TIMEOUT_SECONDS`, `MAX_HISTORY_CHARS` are read
from `.env` at import time. `_model_overrides: dict[str, str]` (keyed by `chat_id`) holds any
per-chat model choice from `set_model_override` (e.g. a `/switch_model` command) — in-process only,
does not survive a restart.

### `set_model_override(chat_id: str, model_name: str) -> None` / `get_model_override(chat_id: str) -> Optional[str]` / `clear_model_override(chat_id: str) -> None`

Plain dict accessors over `_model_overrides`. `get_conversation(chat_id, user_id)` and archiving
never consult this — only `handle_incoming_message`'s own `model_name` resolution does (see below).

### `get_conversation(chat_id: str, user_id: str) -> tuple[ConversationTranscript, int]` *(async)*

Returns the chat's open `(transcript, conversation_id)`. Creates one if none is cached, or if the
cached one has been idle longer than `CONVERSATION_IDLE_TIMEOUT_SECONDS` (archiving the stale one
in the background first via `_archive_in_background`). Otherwise reuses the cached entry and
refreshes its `last_active` timestamp.

### `_archive_in_background(conversation_id: int, chat_id: str, model_name: str) -> None` *(internal, schedules a task)*

Wraps `_archive_conversation` in `asyncio.create_task`, tracked in `_background_tasks`. Shared by
`get_conversation` (idle timeout) and `start_new_conversation` (`/start`).

### `_archive_conversation(conversation_id: int, chat_id: str, model_name: str) -> None` *(async, internal)*

Calls `summarize_and_embed_conversation` with logging/exception handling for fire-and-forget use;
an empty-conversation `ValueError` is logged and swallowed, not raised.

### `_turns_to_history(turns: list[dict]) -> tuple[list[dict], int]` *(internal)*

Projects `get_turns()`'s output into a `conversation_history` list (`{"role": "user"/"assistant",
"content": ...}` pairs, dict outputs JSON-stringified), walking backward from the most recent turn
and stopping once `MAX_HISTORY_CHARS` is spent — a character budget, not a turn count, since a
handful of big turns can cost as much context as many small ones. Always includes at least the
single most recent turn even if it alone exceeds the budget. Returns `(history, included_count)` —
`included_count` is how many of the most recent turns made it in, used by
`handle_incoming_message` to tell the model exactly what to pass `recall_conversation_history`'s
`skip_recent` as. The bot's live, short-term memory of the current conversation; anything older is
recalled from the DB on demand instead of kept in context by default.

### `start_new_conversation(chat_id: str, model_name: Optional[str] = None) -> None` *(async)*

Pops and archives (in the background) whichever conversation is open for `chat_id`, if any. The
next `handle_incoming_message` call for that `chat_id` opens a fresh one. Backs `/start`.

### `summarize_open_conversation(chat_id: str, model_name: Optional[str] = None) -> str` *(async)*

Summarizes + embeds the chat's current, still-open conversation on demand, without closing it.
Backs `/summarize`.

- **Raises:** `ValueError` if there's no open conversation for `chat_id`, or it has no turns yet.

### `handle_incoming_message(user_id: str, chat_id: str, text: str, attachments: Optional[list[Attachment]] = None, model_name: Optional[str] = None, feedback: Optional[ChannelFeedback] = None) -> dict[str, Any]` *(async)*

The main entry point. Gets/creates the conversation; ingests any `attachments` via
`memory.ingest_and_index_document` (extraction + chunk/embed + summarize/embed, all persisted) and
folds each returned note directly into the effective prompt text — not just a system-prompt aside,
so the reference survives into later turns' `conversation_history` too, via the persisted
`user_prompt`; builds live history via `_turns_to_history`; sets `prompts.recall_history_note` as
`system_prompt` if any turns were left out of that live history; sets `context.current_user_id`/
`context.current_conversation_id`; runs `GenerativeService.generate_with_tools` off-thread,
bracketed by `feedback.start("Thinking")`/`feedback.finish()`; awaits `persist_turn`; returns its
result dict.
Callers send `result["output"]` back over their own channel — this function never touches anything
channel-specific.

- **`model_name` resolution:** explicit argument > `get_model_override(chat_id)` (set via e.g. a
  `/switch_model` command) > `DEFAULT_MODEL_NAME`.
- **`feedback`:** defaults to a no-op `ChannelFeedback()` if omitted. Passed straight through to
  `ingest_and_index_document` for any attachments (see `frontend/base.py` and
  `memory/document_index.py` for the stage-by-stage progress it reports); separately wraps the
  `generate_with_tools` call so a long tool-calling turn keeps reporting activity too, ending with
  an unlabeled `finish()` (Telegram's `TelegramFeedback` deletes the message in that case, rather
  than leaving a stale "Thinking" placeholder once the real reply arrives).
- **On failure:** catches any exception from `generate_with_tools`, calls `feedback.finish(f"❌
  Failed: {e}")`, and persists a best-effort turn (`getattr(e, "partial_output"/"trace"/
  "tokens_used", ...)`, plus `"error": str(e)`) via `persist_turn` *before* re-raising — so a
  `ToolLoopError` (or any other exception) still leaves a durable record in the transcript/DB
  instead of vanishing. The exception still propagates to the caller (`webhook_server.py` catches
  it and replies with a generic error message); this only changes what gets persisted, not what the
  user sees.

---

## `llm_engine/frontend/`

Channel-agnostic progress/activity feedback — see ARCHITECTURE.md's "Progress/activity feedback is
channel-agnostic too" for the design rationale. `memory/document_index.py` and `pipeline.py` import
`ChannelFeedback` from `frontend.base` specifically (not the package root) — `frontend/__init__.py`
only re-exports `ChannelFeedback`, deliberately not `TelegramFeedback`, since a package's
`__init__.py` always runs on any submodule import; re-exporting `TelegramFeedback` there would force
`python-telegram-bot` to load as a side effect of importing `llm_engine.frontend` at all, defeating
the point for channel-agnostic callers that only need the abstract base.

### `class ChannelFeedback` (`frontend/base.py`)

The interface. Three `async` methods, all no-ops on the base class itself:

- `start(label: str) -> None` — called once, before the tracked operation begins.
- `progress(label: str, fraction: float) -> None` — called at each stage boundary, `fraction` in
  `[0, 1]`.
- `finish(label: Optional[str] = None) -> None` — called once when the operation ends, success or
  failure; callers call this from a `finally` (or an `except`/success-path pair, see
  `ingest_and_index_document`) so cleanup always happens.

Because every method is a no-op, this class doubles as the "no feedback wired up" default — call
sites do `feedback = feedback or ChannelFeedback()` once and then call unconditionally, no
`if feedback:` checks scattered around. A future front end (Django, ...) implements this interface
however suits it; `document_index.py`/`pipeline.py` never change.

### `class TelegramFeedback(ChannelFeedback)` (`frontend/telegram.py`)

Telegram's implementation. `__init__(bot, chat_id, action="typing", keep_alive_interval=4.0)`.

- `start(label)` — sends one message (`f"⏳ {label}"`, capturing its `message_id`) and spawns a
  background `asyncio.Task` that re-sends `send_chat_action` every `keep_alive_interval` seconds.
  Necessary because a single `send_chat_action` call only shows for ~5 seconds — nowhere near
  enough for a multi-minute document pipeline, which is what silently going stale looked like
  before this existed.
- `progress(label, fraction)` — edits that same message to `f"⏳ {label} ({percent}%)"`. Swallows
  Telegram's "message is not modified" `BadRequest` (two stages can round to the same percent) —
  anything else is logged as a warning, not raised, since a failed progress update shouldn't fail
  the underlying operation.
- `finish(label=None)` — cancels the keep-alive task first, then either edits the message to
  `label` if given, or deletes it if not (used for the ephemeral "Thinking" message in
  `pipeline.handle_incoming_message`, which would otherwise sit stale once the real reply lands
  right after it). Safe to call more than once (a no-op after the first).
- **Verified** with a mocked `Bot` (unit-level: message lifecycle, keep-alive cadence, cancellation
  on `finish`, idempotent `finish`) and end-to-end against the real test PDF (integration-level:
  full 14-step progress trail through `pipeline.handle_incoming_message` →
  `ingest_and_index_document`, 58 chat-action refreshes over a ~230s run).

---

## `llm_engine/tooling/registry.py`

### `tool(fn: Callable) -> Callable`

Decorator. Inspects `fn`'s type hints and docstring (via `inspect` + pydantic's
`create_model(...).model_json_schema()`) to build an OpenAI function-calling schema, appends it to
module-level `TOOLS`, and registers `TOOL_FUNCTIONS[fn.__name__] = fn`. Returns `fn` unchanged.

- **To add a new tool:** write a function with type-hinted params and a docstring (the docstring
  becomes the tool's `description` — write it for the model, not for a human reader), decorate it
  with `@tool`, and import the module from `tooling/__init__.py` so the decorator actually runs.
- **Caveat:** parameter types must be things pydantic can turn into JSON Schema (`str`, `int`,
  `Literal[...]`, `Optional[...]`, `list[...]`, etc.).

### `TOOLS: list[dict]`, `TOOL_FUNCTIONS: dict[str, Callable]`

Module-level registries, populated by every `@tool`-decorated import. `TOOLS` is what you pass as
the `tools=` argument to an OpenAI-style `chat.completions.create` call (or `generate_with_tools`,
which defaults to the full list). `TOOL_FUNCTIONS` is the name → callable dispatch table used by
`_run_tool_loop`.

---

## `llm_engine/tooling/websearch.py`

### `@tool web_search(query: str, max_results: Optional[int] = 5, search_depth: Literal["basic", "advanced"] = "basic") -> list[dict]`

Searches the web via Tavily. Requires `TAVILY_TOKEN` in `.env`.

- **Returns:** a list of `{"title": str, "url": str, "content": str}`. On an empty result set,
  currently returns `None` (not `[]`) — see the `if not results: return` branch; callers should
  handle a falsy return, not assume a list.
- **Raises:** `ValueError` if `TAVILY_TOKEN` is unset. Network/API errors are caught internally
  and printed, not re-raised (the function returns whatever partial `search_results` it had, which
  may be `[]`).
- **Standalone use:** `python llm_engine/tooling/websearch.py <query>` (or interactive prompt with
  no args) runs it directly and pretty-prints JSON.

---

## `llm_engine/tooling/memory_tools.py`

The retrieval half of the memory system — lets the agent look up and read the current user's past
conversations. Both tools read `context.current_user_id`; if it's unset (e.g. called outside the
Telegram flow, with no context set), they return an empty/safe result rather than erroring.

Uses lazy, in-function imports for `embeddings`/`memory.db`/`memory.transcript` (see the comment in
the source) — importing them at module level would be a circular import, since `tgi.py` imports
`tooling` (which imports this module) as part of *its own* top-level imports, before `tgi.py`'s
`GenerativeService` class exists yet for `embeddings.py` to import back.

### `@tool search_memory(query: str, max_results: Optional[int] = 5) -> list[dict[str, Any]]`

Embeds `query`, then does a cosine-distance nearest-neighbor search over `conversations.embedding`
**scoped to `current_user_id.get()`** (via `memory.db.search_conversations`).

- **Returns:** a list of `{"conversation_id": int, "summary": str, "file_path": str, "created_at":
  str (ISO), "relevance": float}`, ordered most-relevant first. `relevance` is cosine similarity
  (`1 - distance`; `1.0` = identical). Conversations with no summary/embedding yet (never
  archived) are excluded. Returns `[]` if there's no current user context.
- **Intended follow-up:** if a summary isn't detailed enough, the model is instructed (via its
  docstring) to call `read_conversation` with the returned `file_path`.

### `@tool read_conversation(file_path: str) -> str`

Reads the full markdown transcript at `file_path`. Two intended uses: a past conversation
surfaced by `search_memory` (when its summary isn't detailed enough), or — via
`prompts.current_transcript_note` — the model's own *currently open* conversation, to recall
turns that scrolled out of its live `conversation_history`. No summary/embedding is required for
either; this just reads whatever markdown file is at the given path.

- **Security:** resolves `file_path` and verifies it's actually inside
  `{LOG_DIR}/conversations/{current_user_id}/` before reading — rejects anything outside that
  directory with `"Access denied: ..."`, regardless of what path the model asks for. This is
  defense in depth: `search_memory` only ever hands out paths already scoped to the right user, but
  `read_conversation` doesn't trust that — it re-checks independently in case the model hallucinates
  or is steered toward an arbitrary path.
- **Returns:** the file's text, truncated to 8000 chars (`_MAX_TRANSCRIPT_CHARS`) with a note if
  cut off. `"No active user context."` if `current_user_id` is unset, `"That conversation
  transcript no longer exists."` if the (allowed) path doesn't exist.

---

## `llm_engine/tooling/document_tools.py`

The counterpart to `memory_tools.py` for uploaded documents rather than past conversations.
`..documents` is a plain module-level import (no circular-import concern — `documents.py` doesn't
transitively import `tgi.py`); `..embeddings`/`..memory.db` are lazy, on-call imports inside
`search_documents` for the same reason `memory_tools.search_memory` needs them lazy.

### `@tool search_documents(query: str, max_results: Optional[int] = 5) -> list[dict[str, Any]]`

Hybrid search over `documents`, **scoped to `current_user_id.get()`** (via `memory.db.
search_documents`) — the document-level counterpart to `memory_tools.search_memory`. Embeds `query`
and reciprocal-rank-fuses two independent rankings: cosine distance against `documents.embedding`
(the *general* summary embedding), and Postgres full-text search (`websearch_to_tsquery`) against
`documents.search_vector` (title + all three summary flavors — see ARCHITECTURE.md's "hybrid
search" entry for why RRF over a weighted blend, and why HyDE was considered and rejected).

- **Returns:** a list of `{"document_id": int, "filename": str, "title": str | None, "authors":
  list[str], "keywords": list[str], "summary": str, "methods_summary": str, "results_summary": str,
  "text_path": str, "created_at": str (ISO), "relevance": float}`, ordered most-relevant first.
  `relevance` is now the *fused* RRF score, not a raw cosine similarity — higher is still more
  relevant, but it no longer means "1.0 = identical" the way `1 - distance` did. All three summary
  flavors come back together, not just the one used for ranking — enough for a multi-angle overview
  from one call (the general summary covers conceptual/theoretical framing too, so there's no
  separate "conceptual" field). Documents with no summary/embedding yet (upload still in progress,
  or failed before that step) are excluded. Returns `[]` if there's no current user context.
- **Intended follow-up:** if the summaries still aren't detailed enough, the model is instructed
  (via its docstring, and `prompts.DOCUMENT_SEARCH_GUIDANCE_PROMPT`) to call `search_document_chunks`
  (a specific fact/passage) or `read_document` (the full text) with the returned `document_id`/
  `text_path`.

### `@tool search_document_chunks(document_id: int, query: str, max_results: Optional[int] = 5) -> list[dict[str, Any]]`

The drill-deeper counterpart to `search_documents`, and hybrid in the same way: embeds `query` and
reciprocal-rank-fuses cosine distance (per-chunk embedding) with Postgres full-text search against
`document_chunks.search_vector` (the chunk's `content`), scoped to just `document_id`'s chunks (via
`memory.db.search_document_chunks`) **scoped to `current_user_id.get()`**. For a specific fact,
figure, or passage the whole-document summary flavors don't cover — `search_documents` first to
find the right document, then this to find the right passage within it.

- **Returns:** a list of `{"chunk_index": int, "content": str, "start_line": int, "end_line": int,
  "relevance": float}`, ordered most-relevant first (`relevance` is the fused RRF score, same
  caveat as `search_documents` above). `start_line`/`end_line` let the model follow up with
  `read_document(text_path, start_line=..., end_line=...)` for more surrounding context than the
  chunk alone provides. Returns `[]` if there's no current user context, or if `document_id`
  doesn't belong to the current user (same result either way — the ownership `JOIN` is applied in
  *both* the vector and text-search CTEs, so a wrong-owner lookup is indistinguishable from an
  empty result on either signal, not an error that would hint the ID exists). Verified directly:
  querying a real `document_id` as a different user returns `[]`, not that user's chunks.

### `@tool read_document(file_path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str`

Reads the extracted markdown text of a document the current user uploaded, given the `file_path`
from that upload's note (see `pipeline.handle_incoming_message` / `prompts.document_note`) or the
`text_path` returned by `search_documents`. Paginated by line range rather than a flat truncation —
a research paper is routinely longer than fits in one call.

- **Args:** `start_line`/`end_line` are 1-indexed and inclusive. Omitted, reading starts at line 1.
  `end_line` omitted reads to the end of the document *or* `_MAX_LINES_PER_CALL` (`400`), whichever
  comes first — so a caller never needs to know the document's length up front to get a first page.
- **Security:** same pattern as `read_conversation` — resolves `file_path` and verifies it's
  inside `{LOG_DIR}/uploads/{current_user_id}/` before reading, independent of whether the model's
  path came from a legitimate note or not.
- **Returns:** the requested lines, joined, with a trailing `[lines A-B of N total]` note. If
  `B < N`, the note also states the exact `start_line` to continue reading from — the model doesn't
  need to track pagination state itself, just read the note. A per-call char ceiling
  (`_MAX_CHARS_PER_CALL`, `8000`) still applies underneath the line cap, in case a single line is
  unexpectedly huge. Error strings (not exceptions) for bad input/access: `"No active user
  context."`, `"Access denied: ..."`, `"That document no longer exists."`, `"start_line (...) is
  past the end of the document (...)."`, `"Invalid range: start_line (...) is after end_line
  (...)."`.

---

## `llm_engine/tooling/database_tools.py`

General-purpose, read-only SQL access — distinct in kind from every other search tool, which take a
natural-language `query` and run one specific, hand-written statement. See ARCHITECTURE.md's
"General-purpose database access" for the full security design; layered enforcement summary below.

### `@tool query_database(sql: str) -> list[dict[str, Any]] | str`

Runs `sql` (a single `SELECT` the model writes itself) against five pre-scoped views in a separate
`agent_readonly` Postgres schema — `my_conversations`, `my_conversation_turns`, `my_documents`,
`my_document_chunks`, `my_document_keywords` (columns listed in the tool's own docstring, which is
what the model actually sees). For exploratory questions the curated `search_*` tools don't answer
directly — counting, filtering by date, joining across documents and chunks, etc.

- **Client-side validation (cheap, not the real boundary):** rejects anything that isn't a single
  statement starting with `SELECT` (case-insensitive `re.match`), and anything matching a keyword
  blocklist (`INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/`TRUNCATE`/`GRANT`/`REVOKE`/`CREATE`/`CALL`/
  `COPY`/`VACUUM`/`EXECUTE`/`DO`/`MERGE`). A semicolon anywhere (other than a single trailing one,
  which is stripped) is rejected outright as a multi-statement attempt.
- **The real boundary is `memory.db.run_readonly_query`** — see that entry below.
- **Returns:** a list of row dicts (capped at 50 by `run_readonly_query`'s `LIMIT`), or a plain
  error string (`"No active user context."`, `"Only a single statement is allowed."`, `"Only SELECT
  statements are allowed."`, `"That statement isn't allowed -- ..."`, or `"Query failed: {e}"` for
  anything the database itself rejects — e.g. a `SELECT` against a raw table instead of a view,
  which fails with Postgres's own `permission denied`, not a Python-level check).
- **Verified directly**, not just by inspection: a legitimate join across `my_documents`/
  `my_document_keywords` returns correct rows; every rejection path (`DELETE` on a view, `DELETE`
  on the raw table name, multi-statement injection via `;`, a raw-table `SELECT` bypassing the
  views) is individually exercised; querying as a different/fake `user_id` against real data
  returns `[]`, not another user's rows; a trailing SQL comment can't comment out the query
  wrapper's own closing parenthesis (see `run_readonly_query`); five queries in a row followed by a
  normal full-privilege query on the same connection pool confirm no role/session-variable leakage
  between pooled connections.

---

## `llm_engine/tooling/_tooling_deps/arxiv_search.py` / `pubmed_search.py`

Live under `tooling/_tooling_deps/`, not `llm_engine/` directly — they're implementation detail of
one tool module (`tooling.literature_tools`, the only thing that imports either), not general
infrastructure the rest of the codebase reaches for. `_tooling_deps/` is where a tool's own
non-tool helper scripts belong, as opposed to `llm_engine/`-root modules like `embeddings.py`/
`documents.py` that multiple unrelated parts of the codebase share.

External literature search — arXiv (`arxiv_search.py`) and PubMed/NCBI E-utilities
(`pubmed_search.py`). Adapted directly (no modification needed — both are self-contained,
`requests` + stdlib only) from a prior project's `core/search/services/db_handlers/`. Each exposes
`build_*_query(all_of, any_of, none_of, field, ...)` (boolean AND/OR/NOT query construction) and
`search_*(...)` (runs the query, returns a list of dicts in a schema shared across both modules:
`db_id`, `src_db`, `title`, `authors`, `published_date`, `updated_date`, `venue`, `categories`,
`hyperlink`, `abstract_text`, `doi`, `pdf_url`, `is_open_access`, `matched_query`, `raw`), plus
`canonical_to_arxiv_field`/`canonical_to_pubmed_field` translating a shared canonical field
vocabulary (`"all"`/`"title"`/`"abstract"`/`"author"`/`"journal"`) into each backend's own field
codes — designed upstream specifically so a caller (here, `tooling.literature_tools`) can expose
one consistent `field` parameter across both instead of two backend-specific vocabularies.
`pubmed_search.py`'s `search_pubmed` additionally rate-limits itself between E-utilities calls
(0.34s without an API key, 0.11s with one) and can fetch full abstract text via a second `efetch`
call (`include_abstract=True`).

## `llm_engine/tooling/literature_tools.py`

Agent-facing wrappers over the two modules above. Two-tier by design, after a real measurement
forced the point: `search_arxiv`/`search_pubmed` return only `_OVERVIEW_FIELDS` (`db_id`, `src_db`,
`title`, `authors`, `published_date`, `venue`) per hit, not the full record — a single default
10-result `search_arxiv` call serialized to ~3,900 tokens, ~95% of this deployment's 4096-token
context budget, and a real agent turn using it was truncated to an empty response as a direct
result (see ARCHITECTURE.md's literature-search entry for the full measurement). `read_paper`
fetches one paper's full detail only when the model actually asks for it.

### `@tool search_arxiv(all_of=None, any_of=None, none_of=None, field="all", max_results=10) -> list[dict[str, Any]] | str`

Builds a boolean query from `all_of`/`any_of`/`none_of` (same semantics as the underlying
`build_arxiv_query`), translates `field` through `canonical_to_arxiv_field`, and returns
`_OVERVIEW_FIELDS` only per hit (~790 tokens for 10 results, measured). Errors (`ValueError`/
`RuntimeError` from the underlying module — e.g. neither `all_of` nor `any_of` given, or the arXiv
API request failing) are caught and returned as a plain `"Search failed: {e}"` string rather than
raising, so a bad query doesn't crash the tool loop.

### `@tool search_pubmed(all_of=None, any_of=None, none_of=None, field="all", max_results=10) -> list[dict[str, Any]] | str`

Same shape as `search_arxiv`, against `search_pubmed` instead — `field` translated through
`canonical_to_pubmed_field`. Always passes `include_abstract=True` (not model-controlled — needed
for `read_paper` to have an abstract to return later) and `email`/`api_key` from `.env`
(`PUBMED_EMAIL`/`PUBMED_API_KEY`, both optional — NCBI asks for an email as a courtesy identifier
for API traffic; an API key raises E-utilities' rate limit but isn't required). Same error-string
handling as `search_arxiv`.

### `@tool read_paper(src_db: str, db_id: str) -> dict[str, Any] | str`

The detail half of the two-tier split: re-queries the source API for just this one paper (not a
cache — trades one extra ~1s API call per lookup for zero memory-management surface) and returns
`_DETAIL_FIELDS` (`db_id`, `src_db`, `title`, `authors`, `published_date`, `updated_date`, `venue`,
`categories`, `hyperlink`, `abstract_text`, `doi`, `pdf_url`, `is_open_access`, ~400 tokens
measured). Dispatches on `src_db`: arXiv via `search_arxiv(raw_query=f"id:{db_id}", max_results=1)`
(arXiv's own `VALID_FIELDS` already includes `"id"`, so this needed zero code changes to
`arxiv_search.py`); PubMed via `search_pubmed(raw_query=f"{db_id}[uid]", max_results=1,
include_abstract=True, ...)` (PubMed's `[uid]` search tag matches one exact PMID, same zero-change
reuse of `pubmed_search.py`'s existing `raw_query` escape hatch). Returns a plain string (`"Unknown
src_db ..."`, `"Lookup failed: {e}"`, `"No {src_db} paper found for db_id=...""`) for anything that
isn't a clean single-record hit.

### `@tool save_finding(db_id, src_db, title, authors, abstract_text, hyperlink, doi=None, categories=None) -> str`

Saves a paper as a lightweight `documents` bookmark for the current user — see ARCHITECTURE.md's
"Literature findings save as lightweight documents bookmarks" entry for the full design (unified
with uploads via `document_kind`, dedup via `external_ref`). Embeds `abstract_text` itself
(`embeddings.embed`) so the bookmark is findable via `search_documents`' hybrid search immediately.
Delegates the actual write to `memory.db.save_literature_finding`, run via `run_on_main_loop` (same
sync-tool-in-worker-thread bridge every other tool here uses).

- **Returns:** `"No active user context."` if `current_user_id` is unset; `"Can't save a finding
  with no abstract_text -- call read_paper first to get it."` if `abstract_text` is empty (since
  `search_arxiv`/`search_pubmed`'s own results don't include it); otherwise `"Saved as
  document_id={id}. Findable later via search_documents."` — the same `document_id` comes back on a
  repeat save of the same `db_id`+`src_db` (update, not a new row), verified directly.

---

## `llm_engine/tooling/control_tools.py`

### `@tool continue_research(reason: str) -> dict`

Generic, not gated to literature tools: lets the model ask `_run_tool_loop` for more room mid-loop.
Registered the normal way (`@tool`, so its schema is introspected and added to `TOOLS` like any
other) purely so the model has something to call — `_run_tool_loop` intercepts
`CONTINUE_RESEARCH_TOOL` ("continue_research") by name *before* normal `TOOL_FUNCTIONS` dispatch,
so this function's own body (`return {"acknowledged": True}`) never actually runs. See
`_run_tool_loop`'s entry above for what actually happens when it's called.

---

## `llm_engine/memory/db.py`

Low-level asyncpg layer. All functions lazily use the shared pool from `get_pool()`.

### `get_pool() -> asyncpg.Pool` *(async)*

Lazily creates (once) and returns the shared connection pool, built from `DATABASE_URL`. Every
new connection gets the `vector` extension ensured, the pgvector codec registered
(`pgvector.asyncpg.register_vector`), and a JSONB codec registered (Python `dict` ↔ JSON directly,
no manual `json.dumps`/`loads` needed at call sites).

- **Raises:** `RuntimeError` if `DATABASE_URL` isn't set.

### `init_schema() -> None` *(async)*

Reads `schema.sql`, substitutes `{embedding_dim}` with `EMBEDDING_DIM`, and executes it. Fully
idempotent (`CREATE ... IF NOT EXISTS` throughout) — safe to call on every startup, which is what
`webhook_server.py`'s `_on_startup` hook does.

### `create_conversation(user_id: str, chat_id: str) -> int` *(async)*

Inserts a new `conversations` row (`file_path` starts `NULL`) and returns its `id`. `user_id` is
a person identifier — currently just the Telegram user id, standing in until real auth exists;
`chat_id` is the interface thread identifier (for Telegram, the chat id — same as `user_id` in a
private chat, different in a group chat).

### `set_conversation_file_path(conversation_id: int, file_path: str) -> None` *(async)*

Writes `file_path` onto a `conversations` row. Called after `ConversationTranscript` builds its
path — the transcript filename embeds `conversation_id`, so the row has to exist (and its id be
known) before the file does, hence the two-step create → build path → set_conversation_file_path
sequence.

### `save_turn(conversation_id: int, user_id: str, user_prompt: str, result: dict, model_name: Optional[str] = None) -> None` *(async)*

Inserts one row into `conversation_turns` — the JSONB backup of a turn. `result` should be a
`generate_text`/`generate_with_tools` return dict; it's stored as-is (JSONB). `user_id` is a
denormalized copy of the parent conversation's `user_id`, so per-user turn queries don't need a
join.

### `get_turns(conversation_id: int) -> list[dict]` *(async)*

Fetches all turns for a conversation, oldest first: `[{"user_prompt": str, "result": dict,
"created_at": datetime}, ...]`. Used by `summarize_and_embed_conversation` as its raw material.

### `search_conversations(user_id: str, query_embedding: list[float], max_results: int = 5) -> list[dict]` *(async)*

Nearest-neighbor search: rows from `conversations` where `user_id` matches and `embedding IS NOT
NULL`, ordered by `embedding <=> query_embedding` (pgvector cosine distance) ascending. Returns
`{"id", "summary", "file_path", "created_at", "distance"}` per row. Used by
`tooling.memory_tools.search_memory`, which additionally converts `distance` to a `relevance`
(similarity) score for the model.

### `update_conversation_summary(conversation_id: int, summary: str, embedding: list[float]) -> None` *(async)*

Writes `summary` and `embedding` onto a `conversations` row and bumps `updated_at`. Overwrite-only
— never touches `conversation_turns`.

### `create_document(user_id: str, conversation_id: Optional[int], filename: str) -> int` *(async)*

Inserts a new `documents` row (`dir_path`/`text_path` start `NULL`) and returns its `id`.
`conversation_id` may be `None` (a document not tied to any conversation).

### `set_document_paths(document_id: int, dir_path: str, text_path: str) -> None` *(async)*

Writes `dir_path`/`text_path` onto a `documents` row — same two-step reasoning as
`set_conversation_file_path`: the per-upload folder embeds this row's own id, so it can only be
created (and the row updated) after the row exists.

### `save_document_chunks(document_id: int, chunks: list[ChunkResult], embeddings: list[list[float]]) -> None` *(async)*

Bulk-inserts a document's chunks in one round trip (`conn.executemany`) — verified to correctly
encode `vector` parameters the same way single-row `execute` calls do. `chunks` (each a
`document_index.ChunkResult`, or anything duck-typing `.content`/`.start_line`/`.end_line`) and
`embeddings` must be the same length and in order; `chunk_index` is assigned from position, and
`start_line`/`end_line` are taken from each chunk.

### `update_document_summary(document_id: int, summary: str, embedding: list[float]) -> None` *(async)*

Writes the *general* `summary`/`embedding` onto a `documents` row and bumps `updated_at`.
Overwrite-only — never touches `document_chunks`.

### `update_document_metadata(document_id: int, title: Optional[str], authors: list[str]) -> None` *(async)*

Writes `title`/`authors` onto a `documents` row and bumps `updated_at`.

### `add_document_keywords(document_id: int, keywords: list[str]) -> None` *(async)*

Upserts each keyword into the shared, deduped `keywords` table and links it to `document_id` via
`document_keywords`. Each word is `.strip().lower()`'d before upsert, so casing/whitespace
variants of the same keyword collapse to one row (`"Machine Learning"` and `"machine learning"`
dedupe together). Uses `INSERT ... ON CONFLICT (word) DO UPDATE ... RETURNING id` to get the
keyword's id whether it was just inserted or already existed. No-op for an empty list.

### `update_document_methods_summary(document_id: int, summary: str, embedding: list[float]) -> None` / `update_document_results_summary(...)` *(async)*

Two near-identical functions (not one parameterized-by-column-name helper, deliberately — column
names can't be safely bound as query parameters, and explicit functions are simpler and safer than
validating a whitelist before interpolating SQL). Each writes its flavor's
`{flavor}_summary`/`{flavor}_embedding` columns and bumps `updated_at`. (A third, `_conceptual`,
existed briefly and was removed — that flavor was folded back into the general summary, see
`update_document_summary` above and ARCHITECTURE.md.)

### `search_documents(user_id: str, query: str, query_embedding: list[float], max_results: int = 5) -> list[dict]` *(async)*

Hybrid search: rows from `documents` where `user_id` matches, combining two independently-ranked
candidate sets via reciprocal rank fusion (see ARCHITECTURE.md's "hybrid search" entry for the
full design and why RRF over a weighted score blend):

- `vector_ranked` CTE — `embedding IS NOT NULL`, ordered by `embedding <=> query_embedding`
  (pgvector cosine distance) ascending, capped at `_CANDIDATE_POOL` (`20`) rows, each assigned a
  `ROW_NUMBER()` rank.
- `text_ranked` CTE — `search_vector @@ websearch_to_tsquery('english', query)`, ordered by
  `ts_rank(search_vector, websearch_to_tsquery('english', query))` descending, same cap, same
  ranking mechanism. Empty (not an error) when `query` has no lexical overlap with any document.
- `fused` CTE — `FULL OUTER JOIN`s the two on `id`, scores each as `Σ 1.0 / (_RRF_K + rank)` over
  whichever CTE(s) it appears in (`_RRF_K = 60`, the standard constant from the original RRF
  paper), `COALESCE`d to `0` for a CTE it's absent from.

Final `SELECT` joins `fused` back to `documents` (for the full row) and `document_keywords`/
`keywords` (for the aggregated keyword list), ordered by `fused.score` descending, capped at
`max_results`. Returns `{"id", "filename", "title", "authors", "text_path", "created_at",
"summary", "methods_summary", "results_summary", "document_kind", "source_url", "doi", "score",
"keywords"}` per row — `keywords` is aggregated via a correlated subquery (`COALESCE(...,
ARRAY[]::TEXT[])`, so it's always a list, never `NULL`). `document_kind`/`source_url`/`doi` (added
for `save_literature_finding`'s bookmarks, see below) let the caller tell a `'bookmark'` apart from
a real `'upload'` — a bookmark has no `text_path`/chunks, so `read_document`/`search_document_chunks`
won't work on one. The document-level counterpart to `search_conversations` (which is still pure
vector search, not hybrid — not asked for there). Used by `tooling.document_tools.search_documents`.

### `save_literature_finding(user_id, conversation_id, src_db, db_id, title, authors, abstract, embedding, source_url, doi, categories) -> int` *(async)*

Inserts (or updates) a `documents` row with `document_kind='bookmark'` — `title`/`authors`/
`abstract`-as-`summary`/`embedding` set, `dir_path`/`text_path` left `NULL` (no local file, ever,
for a bookmark). `external_ref` is built as `f"{src_db}:{db_id}"` and used as the `ON CONFLICT
(user_id, external_ref) WHERE external_ref IS NOT NULL DO UPDATE` target — saving the same paper
again updates the existing row's `title`/`authors`/`summary`/`embedding`/`source_url`/`doi` rather
than creating a duplicate. `categories` are stored via the existing `add_document_keywords` (same
normalization — `.strip().lower()`'d, deduped — as an upload's LLM-extracted keywords). Returns the
`document_id` (new, or the existing bookmark's on an update). Used by
`tooling.literature_tools.save_finding`.

### `search_document_chunks(user_id: str, document_id: int, query: str, query_embedding: list[float], max_results: int = 5) -> list[dict]` *(async)*

Hybrid search over one document's `document_chunks` — same reciprocal-rank-fusion shape as
`search_documents` above (`vector_ranked`/`text_ranked`/`fused` CTEs, `_RRF_K`/`_CANDIDATE_POOL`),
against `dc.embedding`/`dc.search_vector` (the chunk's `content`) instead of the document-level
columns. Returns `{"chunk_index", "content", "start_line", "end_line", "score"}` per row.
`JOIN documents d ON d.id = dc.document_id WHERE dc.document_id = $1 AND d.user_id = $2` is applied
inside *both* the `vector_ranked` and `text_ranked` CTEs (not just one) — the ownership check
happens in the same queries as the search itself, not as a separate pre-check, so a `document_id`
that exists but belongs to someone else fails the JOIN condition on every path into `fused` and
just yields `[]`, identical to a `document_id` that doesn't exist at all (no signal either way that
would let a caller enumerate other users' document ids). Used by
`tooling.document_tools.search_document_chunks`.

### `run_readonly_query(user_id: str, sql: str, limit: int = 50) -> list[dict[str, Any]]` *(async)*

Runs `sql` (already string-validated by the caller — see `tooling.database_tools.query_database`)
as the `alira_agent_readonly` role, on the same shared pool everything else uses. Layered, in order
of how much each layer is actually trusted:

1. **The role's own grants are the real boundary** — `SELECT` on the five `agent_readonly.my_*`
   views only (see `schema.sql`), not the underlying tables, not `INSERT`/`UPDATE`/`DELETE`/DDL on
   anything. `NOLOGIN`: the role can only ever be assumed via `SET LOCAL ROLE` inside a transaction
   (never connected to directly), reverting automatically when that transaction ends.
2. `set_config('app.current_user_id', user_id, true)` (the `true` third arg = transaction-local)
   sets the session variable the views filter on, scoped to just this query.
3. `SET LOCAL statement_timeout = '5s'` bounds any runaway query (e.g. a pathological join or a
   recursive CTE) even though it structurally can't write.
4. `sql` is wrapped as `SELECT * FROM (\n{sql}\n) AS _query_database_query LIMIT {limit}` — the
   row cap applies regardless of what the query itself requested, and putting `sql` on its own
   line(s) means a trailing `--` comment in it can only consume up to that inserted newline, never
   the wrapper's own closing parenthesis after it.
5. The transaction is explicitly rolled back in a `finally`, unconditionally — this path should
   never persist anything, so there's no reason it would ever commit.

Verified end-to-end (see `database_tools.py`'s entry above for the full list) including that
pooled connections come back out clean: five calls to this function in a row, then a plain query on
the same pool still runs as the normal `alira` role with full privileges — no role or session
variable leaks between reuses of a pooled connection.

### `close_pool() -> None` *(async)*

Closes and clears the shared pool. Not currently called anywhere in `webhook_server.py` (the bot
just runs until killed) — relevant for scripts/tests that want a clean shutdown.

---

## `llm_engine/memory/transcript.py`

### `class ConversationTranscript(user_id: str, chat_id: str, conversation_id: int, path: Optional[Path] = None)`

Manages one conversation's markdown log file.

- **Constructor behavior:** if `path` is given, appends to that existing file (creating parent
  dirs if needed). If omitted, creates a new file at
  `{LOG_DIR}/conversations/{user_id}/conv{conversation_id}_chat{chat_id}_{YYYY-MM-DD %Hh-%Mm}.md`
  and writes a header recording all three ids. **Conversation boundaries (new file vs. continue an
  existing one) are entirely the caller's decision** — this class has no concept of "when a
  conversation ends."
- **Note:** `conversation_id` is baked into the filename, so callers must create the DB row first
  (`create_conversation`, to get its id) before constructing this, then call
  `set_conversation_file_path` afterward so the DB row points at the file that was just named
  using its own id. See `pipeline.get_conversation` for the full sequence.
- **Attributes:** `self.user_id`, `self.chat_id`, `self.conversation_id`, `self.path` (a `Path`).

#### `append_turn(user_prompt: str, result: dict, model_name: Optional[str] = None) -> None` *(async)*

Formats one turn (timestamp/model heading, user prompt as a blockquote, reasoning trace if
present, evaluation verdict if present, final answer, token count) and appends it to the file.
The actual file write runs via `asyncio.to_thread` so it never blocks the event loop.

- **Note:** long tool results in the trace are truncated to 500 chars (`_RESULT_TRUNCATE_CHARS`)
  to keep the file readable.

---

## `llm_engine/memory/persist.py`

### `persist_turn(transcript: ConversationTranscript, conversation_id: int, user_id: str, user_prompt: str, result: dict, model_name: Optional[str] = None) -> None` *(async)*

Convenience wrapper: runs `transcript.append_turn(...)` and `db.save_turn(...)` concurrently via
`asyncio.gather`, so the markdown write and the JSONB insert never block each other.

- **Intended usage:** `await` this *after* you've already replied to the user — persistence
  latency should never be on the user-facing critical path.

---

## `llm_engine/memory/summarize.py`

### `summarize_and_embed_conversation(conversation_id: int, model_name: Optional[str] = None) -> str` *(async)*

Fetches every turn for `conversation_id`, builds a `User: ... / Assistant: ...` transcript,
summarizes it via `generate_text` + `prompts.summarize_prompt` (the fixed `Topics / Key facts /
Decisions / Open questions` template), embeds the resulting summary text (`embeddings.embed`), and
writes both back via `update_conversation_summary`.

- **Returns:** the summary text (also what gets stored).
- **Raises:** `ValueError` if the conversation has no turns yet.
- **Idempotent / repeatable:** safe to call on an open conversation (e.g. `/summarize`, doesn't
  end it) or a just-closed one (e.g. archiving on `/start`) — it only ever overwrites
  `summary`/`embedding`, never touches `conversation_turns`.
- **Model note:** the summarizer call defaults to `"qwen3:14b"` if `model_name` is omitted; the
  embedding call always uses `EMBEDDING_MODEL` regardless of `model_name` (that param only
  affects the summarization LLM call, not the embedding model).

---

## `webhook_server.py`

The Telegram bot entry point — a thin channel adapter over `llm_engine/pipeline.py`. Not meant to
be imported — run directly (`python webhook_server.py`). Conversation lifecycle, live history, and
document ingestion all live in `pipeline.py` now (see that section above); this file's handlers
just extract Telegram's own objects into plain strings and call into it.

### `_split_for_telegram(text: str, limit: int = 4096) -> list[str]` *(internal)*

Splits `text` into `<=limit`-char pieces, preferring the last newline before the limit so a long
reply doesn't get sliced mid-sentence (falls back to a hard cut at `limit` if there's no newline to
break on). Telegram's `sendMessage` hard-caps at 4096 characters and raises `BadRequest("Message is
too long")` past it, with no automatic splitting or truncation of its own — confirmed the hard way:
a real document-summary reply hit 5054 characters and crashed `reply_text` in production.

### `_reply(message, text: str) -> None` *(async, internal)*

`message.reply_text`, but split via `_split_for_telegram` first — sends one Telegram message per
chunk. Used everywhere a reply's length isn't bounded (the agent's final answer, `/summarize`'s
summary); `start`/`switch_model_command`'s short, fixed-shape replies still call `reply_text`
directly since they can't realistically exceed the limit.

### `start(update, context) -> None` *(async, `/start` handler)*

`chat_id = str(update.effective_chat.id)`; calls `pipeline.start_new_conversation(chat_id,
model_name=MODEL_NAME)`, then replies with a greeting. The *next* message opens a fresh
conversation.

### `summarize_command(update, context) -> None` *(async, `/summarize` handler)*

Calls `pipeline.summarize_open_conversation(chat_id, model_name=MODEL_NAME)` (awaited, not
backgrounded) and replies with the summary text via `_reply` (length-unbounded, unlike this
handler's other, short, fixed-shape replies). Does not close the conversation. Replies with a
friendly message instead of erroring if there's no active conversation or nothing to summarize yet
(catches the `ValueError`).

### `switch_model_command(update, context) -> None` *(async, `/switch_model` handler)*

With no args: replies with the chat's current model (override if set, else `MODEL_NAME`) and the
list of models known to `GenerativeService.model_routing` (i.e. what's in `LLM_ROUTING`). With one
arg: validates it against that same list — rejects with an "Unknown model" message (listing valid
options) rather than silently accepting a typo — and if valid, calls
`pipeline.set_model_override(chat_id, requested)`. The override is per-chat, in-process only (lost
on bot restart), and persists across `/start` (starting a new conversation doesn't reset your
model choice). Telegram command names can't contain hyphens, hence `/switch_model` not
`/switch-model`.

### `handle_message(update, context) -> None` *(async, text-message handler)*

Extracts `user_id`/`chat_id`/`update.message.text`, builds a `TelegramFeedback(context.bot, chat_id,
action="typing")`, and calls `pipeline.handle_incoming_message(user_id, chat_id, text,
feedback=feedback)` — no explicit `model_name`, so `pipeline` resolves this chat's `/switch_model`
override (if any) before falling back to `MODEL_NAME` itself. Replies with `result["output"]` only,
via `_reply` (splits across messages if the agent's answer exceeds Telegram's 4096-char limit —
confirmed to actually happen), logs token/step counts. Any exception is caught, logged with a
traceback, and answered with a generic `❌ Something went wrong` reply.

### `handle_document(update, context) -> None` *(async, document-message handler)*

Downloads the uploaded file's bytes via `context.bot.get_file(...).download_as_bytearray()`, uses
the message's caption as the prompt text (or a generic fallback if there isn't one), builds a
`TelegramFeedback(context.bot, chat_id, action="upload_document")` — the semantically-closer chat
action for this handler — and calls `pipeline.handle_incoming_message(...,
attachments=[Attachment(filename, file_bytes)], feedback=feedback)`. Same reply/logging/
error-handling shape as `handle_message`. Registered for `filters.Document.ALL` — any file type is
accepted at the Telegram layer; only PDFs are actually handled correctly by
`documents.ingest_document` today (see ARCHITECTURE.md — images are explicitly out of scope for
now).

### `_on_startup(app: Application) -> None` *(async)*

`post_init` hook — calls `context.set_main_loop(asyncio.get_running_loop())` (so the memory/
document tools' `run_on_main_loop` bridge works) and `init_schema()` before the bot starts polling.

### `main() -> None`

Builds the `Application`, registers the `/start`, `/summarize`, `/switch_model`, document, and
text-message handlers, and calls `app.run_polling()` (blocking).
