# Glossary

The vocabulary the [ADRs](adr/README.md) use. Each entry says what a term means
here — and, where the term is easy to blur, what it is *not*. The negatives are
the useful part: most design drift starts with two people using one word for two
things.

---

**Agent** — the thing that runs a reasoning loop and calls tools. There is
exactly one: dcode, driven by Bonsai 2 27B (ADR-0001). *Not* MedGemma, which
cannot call tools at all — its chat template has no tool-call construct.

**Harness** — the program that owns the agent loop, its filesystem and shell
tools, its subagents, and its approval gates. dcode is the harness. We do not
have one of our own and no longer maintain one (ADR-0002).

**Run** — one invocation of the agent, recorded in the `runs` table: trigger,
prompt, thread, exit status, output (ADR-0003). The unit of work. A user message
and a timer firing are two *triggers* for the same thing, not two pipelines.

**Trigger** — what caused a run. Either a message from an adapter, or a due hook.

**Hook (ALiRA2)** — a row in the `hooks` table describing scheduled work, fired
by the ticker (ADR-0004). *Not* to be confused with a dcode hook.

**Hook (dcode)** — a shell handler dcode invokes on `SessionStart`,
`PreToolUse`, `PostToolUse` or `Stop`. We use **middleware** instead (ADR-0010);
the term appears only when discussing dcode's own mechanisms.

**Thread** — dcode's record of one ongoing conversation, stored under
`DEEPAGENTS_HOME`. Resumed across runs with `--resume`. The authoritative record
of what was *said* (ADR-0009).

**Capability** — anything the system can do for the user. Every capability is
exposed as a tool, a library, or (usually) both.

**Tool** — a capability the agent discovers and invokes by name. Returns
quickly. Registered into dcode via `register_tool` (ADR-0005). A tool never
takes the GPU lock itself.

**Library** — a capability the agent imports or shells out to, because it loops,
runs long, or needs exclusive GPU. Takes the GPU lock internally. Ships with a
CLI, because `execute` is how the agent reaches for it first (ADR-0006).

**The split rule** — *A capability is a tool if the agent should discover and
invoke it by name; a library if the agent may need to compose or customize it.
Most are both. Anything touching the GPU takes the lock inside the library,
never in the wrapper.* The practical test: **can this block someone else's
turn?**

**Extension** — Python loaded into dcode's process that registers tools and
middleware (ADR-0005). How ALiRA2's capabilities reach the agent.

**Middleware** — an extension-registered interceptor around the agent loop. Used
for the tool-call mirror, the web-search cache, and the thread summary
(ADR-0010).

**GPU lock** — an OS-level advisory `flock` that every GPU consumer acquires:
llama-server requests, MedGemma batch runs, marker-pdf extraction (ADR-0007).
Advisory, not enforced — correctness depends on GPU work living in libraries
that take it. The only arbiter that covers scripts the agent writes itself.

**Conversation** — what was said. Lives in dcode threads. *Not* stored turn-by-
turn in Postgres any more (ADR-0009).

**Knowledge** — what was learned: documents, papers, findings, summaries, and
their embeddings, in Postgres with pgvector. Ours, and searchable. The other
half of the memory split.

**Finding** — a durable observation worth keeping: a saved paper, a note about
an image, a conclusion. Knowledge, not conversation.

**Adapter** — a thin translation layer between a channel and the run interface.
Telegram is one; the web app will be another (ADR-0012). An adapter holds no
agent logic and never touches the model.

**Route** — a named model endpoint with its own concurrency, locality and retry
policy. Local routes contend for the GPU lock; hosted routes do not.

**Backend** — a model server. Ollama (quantized chat models), llama-server
(Bonsai 2, router mode), transformers in-process (MedGemma), and hosted APIs.

**Document / chunk** — an ingested source and its retrievable pieces, with
separate embeddings and hybrid vector plus full-text search fused by reciprocal
rank. Knowledge.

**Patch** — one tile of a whole-slide image. The unit MedGemma processes in
batch; the reason the batch path is a library rather than a tool.

**Turn action** — one row in `turn_actions` recording a single agent action,
written live and rendered by `live_viewer.py`. Under the new design these are
produced by middleware observing dcode (ADR-0010).

---

## Retired terms

Kept so older documents and commits stay readable.

**Tool loop** — `tgi.py`'s `_run_tool_loop`. Deleted (ADR-0002); dcode's loop
replaces it.

**Digest round** — the pass that collapsed raw tool output into a short note
before the next model call. Deleted with the loop. Returns as middleware if the
need reappears.

**Step budget / `continue_research`** — the self-extending cap on model↔tool
round trips. No dcode equivalent; `--recursion-limit` is a ceiling, not a grant.

**`discover_tools`** — progressive tool revelation, to keep tool schemas out of
every call. Dcode's concern now.

**Chat queue** — the Celery `concurrency=1` queue that serialized GPU calls.
Replaced by the GPU lock (ADR-0007, ADR-0008).
