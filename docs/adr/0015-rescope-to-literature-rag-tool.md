# ADR-0015: Rescope phdcode to a literature RAG tool

**Status:** Accepted — 2026-10-10
**Supersedes:** ADR-0001, 0002, 0003, 0004, 0009, 0010, 0011, 0012. With it,
[ADR-0016](0016-mcp-server-is-the-only-integration.md) supersedes 0005 and
[ADR-0017](0017-gpu-main-slot.md) supersedes 0007 and 0008.

## Context

ADR-0001 to ADR-0012 designed a general assistant: dcode with a local model as
the only agent, Telegram and later a web app, more than one user, and Gmail,
Calendar, reminders and conversation memory. Four premises of that design are
false now:

| Old premise | Now | ADRs |
| --- | --- | --- |
| A local model drives the agent. | Claude, open models and local models drive it. Training often holds the GPU. | 0001, 0002, 0007 |
| One harness. Portability is "not planned". | Portability across harnesses is a requirement. | 0001, 0005, 0010 |
| Telegram, then a web app, more than one user. | The shell, one user, open source. | 0003, 0011, 0012 |
| A general assistant. | A literature tool for arXiv, PubMed and OpenAlex. | 0004, 0008, 0009 |

The new schema uses new table names (`papers`, `chunks`, `saved_searches`), so
it is created beside the old tables with no migration. The old tables hold 2
documents, 51 chunks, 24 conversations and 7 hooks (`count(*)`, 2026-10-10; a
check on 2026-10-08 read the table statistics, which showed 0 rows).

## Decision

phdcode is a personal, open-source literature tool. It keeps a user current
with arXiv, PubMed and OpenAlex, and it answers questions with citations through
any agent harness. The vocabulary is in [GLOSSARY.md](../GLOSSARY.md).

1. **Interface.** An MCP server holds the tools ([ADR-0016](0016-mcp-server-is-the-only-integration.md)).
   phdcode has no agent loop and no harness of its own.
2. **Papers and facets.** An abstract-only paper has one facet: the concept
   facet, which is the abstract. A full-text paper also has a methods facet
   and a results facet. These are summaries of the text, made after the text
   parse. Each facet has its own embedding and its own keyword index. A second
   save of the same paper never erases a value that the new call does not
   give.
3. **Chunks.** A full-text paper keeps its full text as chunks. Each chunk has
   a page, an embedding and a keyword index, so an answer can quote the text.
4. **Search.** Hybrid search (vector and keyword, fused by reciprocal rank) runs
   on one facet of the papers, then on the chunks of the selected papers. The
   search inside selected papers uses an iterative index scan, so that the
   filter does not lose chunks as the table grows.
5. **Citations.** An answer cites the paper, the page and an exact quote. Chunk
   IDs stay internal, because an upgrade replaces the chunks. An answer that
   uses an abstract-only paper says so.
6. **Saved searches and the sweep.** A saved search is a row in Postgres: a
   query and an interest statement. A systemd user timer runs the sweep and
   then the drain. The default time is daily at 06:00, and the user changes it
   with `systemctl --user edit`. The sweep writes a dated digest to a folder
   that a setting names (`DIGEST_DIR`).
   Each sweep searches the papers of the last 7 days, newest first, so that
   it also finds a paper that a source indexes late. A table of triaged hits
   keeps a hit from a second triage; a hit that waits for triage has no row.
   OpenAlex filters by the publication date, because its filter on the date
   that it added a paper needs a paid plan.
7. **Triage.** Triage is a typed decision with a probability:
   `decide(hit, interest_statement) -> probability`, with a threshold for each
   saved search. It uses a System One decision model or, as the fallback, an
   LLM with structured output. It runs on local models only. A labelled set of
   about 100 papers selects the model and the threshold. The decision model
   is a pointwise reranker (`TRIAGE_RERANKER`, default Qwen3-Reranker-0.6B):
   it scores each pair (interest statement, paper) alone and gives P("yes")
   from its logits, so a reply can never be invalid. Its scores are not
   calibrated. The fallback LLM is `TRIAGE_MODEL` (default `SUMMARY_MODEL`).
   Tested on 37 hits (2026-10-11): the reranker gave the same order as
   Bonsai 2 27B, in 16 ms for each hit on the GPU and 3 s on the CPU.
   A listwise reranker (one prompt with many papers) is rejected: the
   score of a paper would depend on the other papers of the day. Embedding similarity
   only sets the order of the hits in the digest. If the model needs the GPU
   and the GPU is not available, the hits wait for the next sweep.
8. **Ingest.** A sweep stores only abstracts. A paper gets its full text only on
   request: `ingest_pdf` parses the PDF at once, in `balanced` mode if Surya
   runs, or else in text mode on the CPU. The drain does the upgrade to
   `balanced` mode later ([ADR-0017](0017-gpu-main-slot.md)). The
   `parse_mode` of a paper is the queue. There is no queue table.
9. **Model calls.** Summaries and other generated text use a model name that
   `llm_engine/routes.py` resolves to an endpoint. The summary model is the
   setting `SUMMARY_MODEL`; its default is the agent backend. Embeddings call
   the native API of the server on the route of the embedding model, so each
   server has one setting.
10. **Records.** The harness keeps the transcript of each session. The sweep and
    the drain write to the systemd journal. There is no runs table and no
    middleware.

### Out of scope

- MedGemma and the analysis of pathology images.
- Telegram, a web app, and more than one user.
- Gmail, Calendar, reminders, and memory of conversations.
- Skill development and teaching. The vault and the `teach` skill own these.

### Kept ADRs

- [ADR-0006](0006-capability-split-tools-vs-libraries.md): the split rule stays.
  Its examples (Gmail, Calendar, MedGemma) are out of scope now.
- [ADR-0014](0014-new-repository-phdcode.md): the repository and its history.

## Consequences

- The code to delete: `tgi.py` (keep `unload_all_models()`), `pipeline.py`,
  `tasks/`, `frontend/`, `webhook_server.py`, `live_viewer.py`, the Gmail,
  Calendar, reminder and memory tools, and the tables for conversations, hooks
  and turn actions.
- The code to keep and move: the arXiv and PubMed clients, the split of a search
  result into an overview and details, `gpu.py`, `routes.py`, `documents.py`,
  `document_index.py`, the `documents` and `document_chunks` tables, `_rrf_cte`
  and the search functions, and the three summary prompts.
- The code to add: an OpenAlex client (it needs an API key), a facet parameter
  that selects the vector column, a page for each chunk (marker
  `--paginate_output`), the saved-search table, the sweep CLI, the drain CLI,
  `decide()`, and the MCP server.
- Rejected: an LLM-generated concept summary. The abstract is the author's own
  summary, and a model adds cost and errors.
- Rejected: download of open-access PDFs during the sweep. It puts work on the
  GPU that nobody asked for. The tool can add it later with no schema change.
- Rejected: triage by embedding similarity alone. It is seldom reliable for
  relevance.
- Rejected: a hosted triage model (Jev). There is no access, and triage must run
  locally.
