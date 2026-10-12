# Glossary

The vocabulary the [ADRs](adr/README.md) use. Each entry says what a term means
here — and, where the term is easy to blur, what it is *not*. The negatives are
the useful part: most design drift starts with two people using one word for two
things.

---

## Literature

**Paper** — one publication from arXiv, PubMed or OpenAlex that the tool
holds. *Avoid:* document, bookmark.

**Reference** — `<source>:<source_id>`, for example `arxiv:2401.12345`: names a
paper at its source, stored or not. An arXiv reference has no version, so a new
version updates the same paper. *Not* a paper id, which only a stored paper
has.

**Abstract-only paper** — a paper whose only text is its metadata and abstract.
An answer that cites it says so.

**Full-text paper** — a paper whose PDF the tool has parsed into chunks.

**Chunk** — one retrievable piece of the full text of a paper, with its page,
its embedding and its keyword index. The source of a quoted passage.

**Parse mode** — how the PDF of a full-text paper was turned into text:
*text* (the text layer only, no OCR) or *balanced* (a vision model reads
the pages where the text layer is bad).

**Upgrade** — a second parse of a full-text paper from *text* to *balanced*
mode. It replaces the chunks, but not the summaries.

**Facet** — one aspect of a paper that search can target: *concept* (the
abstract), *methods* or *results* (summaries of the full text). Each facet has
its own embedding. An abstract-only paper has only the concept facet.

**Citation** — the paper, the page and an exact quote. *Not* a chunk ID, which
changes at an upgrade.

**PDF queue** — the abstract-only papers whose PDF waits for a parse: each has
the URL or the local path of its PDF. `phdcode ingest` parses them; a PDF that
fails leaves the queue with its reason. *Not* the drain, which makes summaries.

**Reference list** — a BibTeX export of the papers that the user cites (Zotero).
`phdcode import` makes an abstract-only paper of each entry with an ID, and
puts its PDF into the PDF queue.

## Surveillance

**Saved search** — a standing query to arXiv, PubMed or OpenAlex, with an
interest statement. *Avoid:* hook, alert.

**Interest statement** — the natural-language description of what makes a hit
relevant to a saved search. Triage judges each hit against it.

**Hit** — a paper that a saved search returns. A hit is *new* until triage
judges it for that saved search.

**Sweep** — one pass over all saved searches that collects new hits, triages
them, stores the relevant ones as abstract-only papers, and writes a digest.

**Triage** — the decision, with a probability, whether one hit is relevant to an
interest statement. Made by a decision model, or by an LLM that replies with
the probability only. *Not* a generated summary.

**Digest** — the dated note of the sweeps of one day: for each saved search,
the relevant hits, each with its triage probability, and the hits that wait
for triage.

**Drain** — the timer pass after the sweep that makes the pending summaries
(`phdcode summarize`). CLI only, never a tool. The upgrade of *text* papers to
*balanced* is deferred (ADR-0017).

**Cleared GPU** — the state between `phdcode gpu clear` and `phdcode gpu
restore`: no phdcode GPU user starts, so that training has the whole card.

## System

**Harness** — the program that owns the agent loop: Claude Code, dcode, or any
other MCP client. phdcode has none of its own (ADR-0016).

**Agent** — the model that runs in the harness and calls phdcode's tools. Any
model with tool calling. *Not* a part of phdcode.

**Capability** — anything the tool can do for the user. Exposed as a tool, a
library, or both.

**Tool** — a capability the agent discovers and invokes by name, through the
MCP server. Returns quickly. Never starts or waits for a GPU workload.

**Library** — a capability the agent imports or calls as a CLI, because it
loops, runs long, or needs the GPU (ADR-0006).

**The split rule** — *A capability is a tool if the agent should discover and
invoke it by name; a library if the agent may need to compose or customize it.
Most are both.* The practical test: **can this block someone else's turn?**

**GPU lock** — the exclusive `flock` that a phdcode tenant holds for its full
life (ADR-0017). *Not* a lock for each request.

**Tenant** — the one workload that holds the GPU for a long time: the local
LLM or training. Embeddings are never a tenant; they run on the CPU.

**Short job** — a GPU job of seconds (the text parse, triage) that uses the GPU
only if that is faster than the CPU, waits included. *Not* a tenant.

**Agent backend** — the setting that names the model that drives the harness. If
it is local, the local LLM is the usual tenant. *Not* chosen by
phdcode: the user sets the harness and this setting to match.

**Route** — a named model endpoint, with its locality. A local route is a GPU
tenant; a hosted route is not. *Not* a lock for each call.

**Backend** — a model server: llama-server, vLLM, Ollama, or a hosted API.

---

## Retired terms

Kept so older documents and commits stay readable.

**Document** — the old name for a paper. The old tables `documents` and
`document_chunks` are now `papers` and `chunks`.

**Bookmark** — `document_kind = 'bookmark'`. Now an abstract-only paper.

**Run / trigger** — one invocation of dcode, recorded in a `runs` table, and its
cause (ADR-0003). Superseded by ADR-0015: the harness keeps its own transcript.

**Hook (ALiRA2)** — a row in the `hooks` table for scheduled work (ADR-0004).
Now a saved search and a systemd timer.

**Hook (dcode)**, **Extension**, **Middleware** — dcode's in-process integration
points (ADR-0005, ADR-0010). Replaced by the MCP server (ADR-0016).

**Thread**, **Conversation**, **Knowledge**, **Finding** — the two halves of the
memory split (ADR-0009). Conversation memory is out of scope.

**Adapter** — a channel layer such as Telegram or a web app (ADR-0012). The
shell is the only interface now.

**Patch** — one tile of a whole-slide image for MedGemma. Out of scope.

**Turn action** — one row in `turn_actions` for `live_viewer.py`. Deleted.

**Tool loop** — `tgi.py`'s `_run_tool_loop`. Deleted (ADR-0002).

**Digest round** — the pass that collapsed raw tool output into a short note
before the next model call. Deleted with the loop. *Not* a digest.

**Step budget / `continue_research`** — the self-extending cap on model↔tool
round trips. Deleted with the loop.

**`discover_tools`** — progressive tool revelation. Deleted with the loop.

**Chat queue** — the Celery `concurrency=1` queue that serialized GPU calls.
Replaced by the GPU lock (ADR-0007, then ADR-0017).
