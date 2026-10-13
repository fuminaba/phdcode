# phdcode

phdcode is a literature RAG tool. It keeps the papers that you read, finds new papers each day, and gives a chat the tools to answer from them, with the title, the page and an exact quote for each claim.

phdcode has no chat of its own. An **MCP server** gives its tools to any harness: Claude Code, dcode, or another MCP client. The harness and its model do the conversation; phdcode does the storage, the search and the parsing.

## What phdcode keeps

- **Papers** from arXiv, PubMed and OpenAlex, and the PDFs that you add.
  - An **abstract-only paper** has its metadata and its abstract.
  - A **full-text paper** also has its parsed PDF in chunks, with pages, sections, tables and figures, and a methods summary and a results summary.
- **Chunk links** inside each full-text paper: a mention of a figure or a table to its caption, and a citation to the reference list.
- **Saved searches**: standing queries, each with an interest statement. The daily sweep runs them and writes a digest.

## Where to start

| To do this | Read |
| --- | --- |
| Understand the parts and the data flow | [How it works](how-it-works.md) |
| Connect a harness, add papers, ask questions | [Using phdcode](using.md) |
| Change a model, a path or a key | [Configuration](configuration.md) |
| Look up a tool or a command | [MCP tools](reference/tools.md), [The phdcode command](reference/cli.md) |
| Look up a word | [Glossary](GLOSSARY.md) |
| Know why a part is as it is | [Decisions](adr/README.md) |

## The design in three rules

1. **The MCP server is the only integration** ([ADR-0016](adr/0016-mcp-server-is-the-only-integration.md)). phdcode has no agent loop. A new capability is a tool, a library function, or both ([ADR-0006](adr/0006-capability-split-tools-vs-libraries.md)).
2. **The GPU has one tenant at a time** ([ADR-0017](adr/0017-gpu-main-slot.md)): the local LLM, a parse, or training. The embeddings run on the CPU, so a search never waits for the GPU.
3. **The summaries are optional.** The chunks are stored before any model step, so a paper is searchable even when the summary model is not available. `phdcode summarize` makes the pending summaries later.
