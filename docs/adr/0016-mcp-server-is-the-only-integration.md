# ADR-0016: An MCP server over stdio is the only integration

**Status:** Accepted — 2026-10-10
**Supersedes:** [ADR-0005](0005-integrate-via-extensions-not-mcp.md)

## Context

ADR-0005 chose dcode extensions over MCP. It rejected MCP because MCP "buys
portability for a migration that is not planned". That premise is false now:
the user works with Claude Code, dcode and local models, and the tools must
work in each of them ([ADR-0015](0015-rescope-to-literature-rag-tool.md)).

Claude Code and dcode both load MCP servers. dcode 0.1.73 has `--mcp-config`
and `dcode mcp` (checked 2026-10-08). A connection was not tested.

## Decision

phdcode gives its tools to a harness only through one MCP server, with the `mcp`
SDK (FastMCP), over **stdio**. The harness starts the server for each session.

The tools:

| Tool | What it does |
| --- | --- |
| `search_sources(query, source)` | A live search of arXiv, PubMed or OpenAlex. Returns an overview. |
| `get_paper(id)` | The details of one paper. |
| `save_paper(id)` | Stores a paper as abstract-only. |
| `ingest_pdf(id or path)` | Makes a full-text paper. Uses Surya only if it already runs. |
| `search_papers(query, facet)` | Hybrid search over one facet of the papers. |
| `search_chunks(query, paper_ids)` | Hybrid search over the chunks of the given papers. |
| `add_saved_search`, `list_saved_searches`, `cancel_saved_search` | Manage the saved searches. |

No tool starts or waits for a GPU workload ([ADR-0017](0017-gpu-main-slot.md)). The drain is a CLI only.

## Consequences

- Any MCP client is a harness for phdcode. A change of harness is a change of
  configuration, not of code.
- No dcode middleware ([ADR-0010](0010-observability-via-extension-middleware.md)
  is superseded). The harness keeps its own transcript.
- A tool call never blocks on the GPU. Embeddings run on the CPU.
- The tool results are serialized as text. The overview and details split stays,
  because one full search result used 95% of a 4K context.

## Alternatives rejected

- **MCP over HTTP.** It needs a service that runs all the time. A Postgres
  connection takes milliseconds, so a warm pool does not justify it.
- **dcode extensions (ADR-0005).** They work only in dcode.
