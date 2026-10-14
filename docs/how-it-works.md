# How phdcode works

phdcode keeps your literature and gives tools to a chat. The chat (the harness and its model) is outside phdcode: Claude Code, dcode, or another MCP client. MCP connects the two. Some parts run with no chat: the import of a reference list, the PDF queue, the summaries, the daily sweep, the backup and the GPU rules.

## 1. The parts

```mermaid
flowchart LR
  subgraph you["You"]
    CC["Claude Code<br/>model: Claude"]
    DC["dcode<br/>model: Bonsai 2 27B"]
    UI["The interface<br/>web page; the importance,<br/>the daily question"]
    CLI["The phdcode command<br/>import, queue, ingest,<br/>summarize, rechunk, backup"]
  end
  subgraph phd["phdcode on the server"]
    MCP["phdcode-mcp<br/>10 tools over MCP"]
    SW["phdcode sweep<br/>timer, 06:00"]
    SUM["phdcode summarize"]
    PARSE["marker text parse<br/>GPU if faster, else CPU"]
    EMB["embeddings<br/>Ollama, CPU"]
    TRI["triage<br/>the summary model:<br/>does the student with<br/>this library want it?"]
  end
  subgraph store["Storage"]
    PG[("Postgres + pgvector<br/>papers, chunks, chunk links,<br/>saved searches, triaged hits")]
    FILES[["logs/papers/ID/<br/>PDF, Markdown, figures, tables"]]
    DIG[["logs/digests/<br/>one file each day"]]
  end
  SRC[("arXiv · PubMed · OpenAlex")]
  LLM["llama-server, port 8080<br/>Bonsai 2 27B, on demand"]
  DRIVE[("Google Drive<br/>one-way backup")]
  CC -- "tool calls" --> MCP
  DC -- "tool calls" --> MCP
  DC -- "chat" --> LLM
  CLI --> PARSE
  CLI --> SRC
  MCP -- "search_sources, get_paper" --> SRC
  MCP -- "ingest_pdf" --> PARSE
  PARSE --> FILES
  PARSE --> EMB
  EMB --> PG
  MCP -- "search_papers, search_chunks,<br/>get_chunks" --> PG
  SW --> SRC
  SW --> TRI
  TRI -- "one question<br/>for each hit" --> LLM
  TRI --> PG
  SW --> DIG
  SUM -- "summaries" --> LLM
  SUM --> PG
  UI --> PG
  UI --> FILES
  UI --> DIG
  PG -- "phdcode backup" --> DRIVE
  FILES -- "phdcode backup" --> DRIVE
```

A paper is **abstract-only** (its metadata and its abstract) or **full-text** (its parsed PDF in chunks, with pages, and a methods summary and a results summary).

## 2. A RAG question

```mermaid
sequenceDiagram
  participant You
  participant H as Harness (Claude Code or dcode)
  participant P as phdcode-mcp
  participant DB as Postgres
  You->>H: a question
  H->>P: search_papers(query, facet)
  P->>DB: hybrid search: meaning (vectors) + keywords, fused by rank
  DB-->>P: the best papers
  P-->>H: titles, abstracts or summaries, scores
  H->>P: search_chunks(query, paper_ids)
  P->>DB: the same hybrid search over the passages
  P-->>H: passages, each with its pages, section, figures and links
  opt the passage needs more
    H->>P: get_chunks(paper_id, chunk_indexes)
    P-->>H: the neighbours, or the figure, table or reference that a link names
  end
  H-->>You: the answer: title, page and an exact quote for each claim
```

The facet selects what `search_papers` searches: **concept** (the title and the abstract, all papers), **methods** or **results** (the summaries, full-text papers only).

## 3. Adding papers

```mermaid
flowchart TD
  BIB["phdcode import FILE.bib<br/>(a Zotero export)"] --> LOOK["look up each entry:<br/>OpenAlex by DOI, PubMed, arXiv"]
  SAVE["save_paper(ref)<br/>(chat)"] --> AO
  LOOK --> AO["abstract-only paper<br/>metadata + abstract + its embedding"]
  LOOK -- "open-access PDF link<br/>or a Zotero copy" --> Q[("the PDF queue")]
  LOCAL["phdcode queue FILES<br/>(PDFs that you downloaded)"] --> FIND["find the paper of each PDF"]
  FIND --> Q
  Q --> ING["phdcode ingest<br/>download, parse, chunk, embed"]
  ING -- "download refused" --> FAIL["PDF failed:<br/>download it yourself, then phdcode queue"]
  ING --> FT1["full-text paper,<br/>summaries pending"]
  FT1 --> SUMM["phdcode summarize<br/>(the summary model)"]
  CHAT["ingest_pdf(ref or local PDF)<br/>(chat)"] --> FIND2["find the paper<br/>(a local PDF)"]
  FIND2 --> ONE["parse, chunk, embed, summarize<br/>in one call"]
  SUMM --> FT["full-text paper"]
  ONE --> FT
```

**How phdcode finds the paper of a local PDF**, in this order. Steps 0 to 2 need no network.

| Step | Check | Result |
| --- | --- | --- |
| 0 | A paper already has this file in its PDF queue | that paper (a second `phdcode queue` of the file makes no duplicate) |
| 1 | A DOI on the first two pages is stored. An arXiv ID counts as the DOI `10.48550/arxiv.<id>` | the stored paper |
| 2 | The title of a stored paper is on the first page. The longest title wins | the stored paper |
| 3 | The DOI that OpenAlex gives for that DOI is stored. OpenAlex leads a preprint DOI to the journal version | the stored paper |
| 4 | OpenAlex knows the DOI | a new paper, with its metadata from OpenAlex |
| 5 | Nothing matches | a new empty paper: the summary model reads its title, authors and abstract from the text |

A paper that already has full text is not parsed again.

## 4. A paper as a graph of chunks

The parse gives one Markdown file for each paper. The chunker cuts it into chunks of about 1,500 characters. A table, and a figure with its caption, stay in one chunk. Each chunk keeps its pages and its **section path**: the headings above it, for example `3 Methods > 3.2 Data`.

```mermaid
flowchart LR
  C11["chunk 11<br/>Results<br/>'... shown in Table 1 ...'"] -- "table: Table 1" --> C12["chunk 12<br/>Results<br/>Table 1 and its caption"]
  C11 -- "figure: Fig. 3" --> C14["chunk 14<br/>Results<br/>Fig. 3 and its caption"]
  C11 -- "citation: Smith, 2020" --> C40["chunk 40<br/>References<br/>Smith, J. 2020 ..."]
  C10["chunk 10"] -. "order" .-> C11 -. "order" .-> C12
```

A **chunk link** joins two chunks of one paper:

| Kind | From | To |
| --- | --- | --- |
| `figure` | a mention, for example "Fig. 3" or "Extended Data Fig. 1" | the chunk with the caption of that figure |
| `table` | a mention, for example "Table 2" | the chunk with the caption of that table |
| `ref` | an internal link of the PDF | the chunk with its target |
| `citation` | an internal link of the PDF | the chunk with its target, in the reference list |

The order of the chunks needs no link: the neighbours of chunk *i* are *i* − 1 and *i* + 1. `search_chunks` returns the links of each passage, and `get_chunks` reads their targets. `phdcode rechunk` makes the chunks and the links again from the parsed text, with no new parse.

## 5. The daily sweep

```mermaid
flowchart TD
  T["06:00 timer"] --> Q["each saved search: its query at its source,<br/>since its last complete sweep (at least 7 days),<br/>at most 50 hits"]
  Q --> N{"judged before?"}
  N -- "yes" --> X["skip"]
  N -- "no" --> R["triage: the probability that the PhD student<br/>with this library wants to read the hit"]
  R --> H["record it in triaged hits"]
  H --> TH{"at or above the threshold?"}
  TH -- "yes" --> SV["save as an abstract-only paper<br/>+ a line in today's digest"]
  TH -- "no" --> NO["only the count in the digest"]
  SV --> SM["then: phdcode summarize, phdcode backup<br/>(phdcode ingest runs before the sweep)"]
```

A **saved search** is a standing query at one source, with an **interest statement** and a threshold (0.3 by default). Triage does not read the interest statement. It asks the summary model one question for each hit: given the titles of your **library** (the papers that you added, and the hits that you marked as relevant; not the sweep's own papers), would this PhD student want to read the paper? The interest statement orders the relevant hits in the digest ([ADR-0018](adr/0018-triage-by-the-library.md)).

## 6. The GPU: one tenant at a time

```mermaid
flowchart LR
  L["llama-server, 14.4 GB<br/>starts at the first request,<br/>stops 30 minutes after the last"] --> G[("RTX 5070 Ti, 16 GB<br/>/tmp/gpu.lock")]
  J["short jobs: the parse (about 5 GB)<br/>GPU only if faster, waits included;<br/>else the CPU"] --> G
  TR["training<br/>phdcode gpu clear, train,<br/>phdcode gpu restore"] --> G
```

The embeddings always run on the CPU, so a search never waits for the GPU. Triage uses llama-server, so it waits during training: the next sweep judges the waiting hits, with the same model.

## The tools that a chat can call

| Tool | What it does | Writes? |
| --- | --- | --- |
| `search_sources` | Searches arXiv, PubMed or OpenAlex live. | no |
| `get_paper` | The details of one paper, at the source or stored. | no |
| `search_papers` | Hybrid search over the stored papers, by facet. | no |
| `search_chunks` | Hybrid search over the full text: passages with pages, section, figures and links. | no |
| `get_chunks` | Reads up to 10 chunks of one paper: the text around a passage, or the target of a link. | no |
| `list_saved_searches` | The active saved searches. | no |
| `save_paper` | Stores an abstract-only paper. | yes |
| `ingest_pdf` | Makes a full-text paper from a reference or a local PDF. | yes |
| `add_saved_search` | Adds a standing query with its interest statement and threshold. | yes |
| `cancel_saved_search` | Stops a saved search. | yes |

A tool that writes needs your approval in Claude Code and in the dcode TUI. Headless dcode (`dcode -n`) can run only the tools that do not write.
