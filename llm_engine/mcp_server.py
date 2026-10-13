"""The MCP server: phdcode's only integration (ADR-0016).

Any MCP client starts it over stdio, for example:

    claude mcp add phdcode -- ~/miniconda3/envs/alira/bin/phdcode-mcp

stdout carries the protocol, so nothing in this process prints: logging goes
to stderr, and the PDF parse runs in a subprocess (documents.py).
"""
import asyncio
import functools
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import requests
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import sources
from .documents import PAPERS_DIR
from .embeddings import embed
from .memory import (
    FACETS,
    add_saved_search as db_add_saved_search,
    cancel_saved_search as db_cancel_saved_search,
    close_pool,
    download_pdf,
    find_paper,
    get_chunks as db_get_chunks,
    get_paper as db_get_paper,
    ingest_full_text,
    init_schema,
    list_saved_searches as db_list_saved_searches,
    paper_for_pdf,
    queue_pdf,
    save_record,
    search_chunks as db_search_chunks,
    search_papers as db_search_papers,
)

log = logging.getLogger(__name__)

# The phdcode interface (dashboard.py), for the links to figures.
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://100.110.224.47:8620")
_FIGURE = re.compile(r"!\[[^\]]*\]\(([^)/]+)\)")

INSTRUCTIONS = """\
phdcode keeps the user's literature: papers from arXiv, PubMed and OpenAlex,
stored as abstract-only papers (title and abstract) or as full-text papers
(the parsed PDF in chunks, with pages, and methods and results summaries).

- To answer from what the user has: search_papers, then search_chunks on the
  papers it finds. To find new papers: search_sources, then get_paper.
- Cite each claim with the paper title, the page and an exact quote from a
  chunk. If a claim rests only on an abstract (parse_mode null), say so.
- A passage can hold a table (Markdown rows) or figures (each with its file
  and its link). When a table or a figure answers the question, include it:
  the table as Markdown, a figure by its caption and its link. A client that
  reads images can open the file to describe the figure.
- Each passage has its chunk_index, its section and its links (to the
  figures, tables and other parts of the paper that it refers to, and to
  the reference list). To read the text around a passage (chunk_index - 1
  and + 1), or a linked figure or table, call get_chunks.
- save_paper keeps an abstract-only paper. ingest_pdf makes a full-text paper.
- A saved search is a standing query that a daily sweep runs; triage judges
  whether the user, given the papers of their library, wants each new hit.
"""


@asynccontextmanager
async def _lifespan(_server: MCPServer):
    await init_schema()
    try:
        yield None
    finally:
        await close_pool()


server = MCPServer("phdcode", instructions=INSTRUCTIONS, lifespan=_lifespan)


def tool(fn=None, *, read_only=False):
    """server.tool(), and an expected failure (bad input, a source or a
    download that fails) becomes a ToolError, so that the agent reads the
    reason. MCPServer hides the message of any other exception and logs it
    as a crash.

    read_only=True sets readOnlyHint: a client can then run the tool with no
    approval (headless dcode rejects each MCP tool without it).

    No output schema (structured_output=False): the result is JSON text. A
    schema makes the MCP client validate the result, and in dcode 0.1.73 that
    validation imports jsonschema on the server event loop, which stops the
    run with a BlockingError."""
    if fn is None:
        return functools.partial(tool, read_only=read_only)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (ValueError, RuntimeError, requests.RequestException) as e:
            raise ToolError(str(e)) from e
    annotations = ToolAnnotations(read_only_hint=True) if read_only else None
    return server.tool(annotations=annotations, structured_output=False)(wrapper)


async def _embed_one(text: str) -> list[float]:
    [vector] = await asyncio.to_thread(embed, [text])
    return vector


def _overview(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": record["ref"],
        "title": record["title"],
        "authors": record["authors"],
        "published_date": (record["published_date"] or "")[:10],
        "venue": record["venue"],
    }


async def _lookup(ref: str) -> dict[str, Any]:
    record = await asyncio.to_thread(sources.lookup, ref)
    if record is None:
        raise ValueError(f"No paper found for {ref!r}.")
    return record


@tool(read_only=True)
async def search_sources(query: str, source: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Searches arXiv, PubMed or OpenAlex live. Returns an overview of each hit (ref,
    title, authors, date, venue); call get_paper(ref) for the abstract.

    query uses the native syntax of the source:
    - arXiv: field prefixes ti:, abs:, au:, cat:, all: with AND, OR, ANDNOT,
      e.g. 'abs:"nuclei segmentation" AND cat:eess.IV'.
    - PubMed: field tags [tiab], [au], [mh] with AND, OR, NOT,
      e.g. '"nuclei segmentation"[tiab] AND histopathology[tiab]'.
    - OpenAlex: the value of its filter parameter, filters joined by commas
      (AND); title_and_abstract.search takes AND, OR, NOT and quotes,
      e.g. 'title_and_abstract.search:"nuclei segmentation",type:article'.
      Each OpenAlex search uses a small daily budget: prefer arXiv or PubMed.
    source: "arxiv", "pubmed" or "openalex".
    """
    records = await asyncio.to_thread(sources.search, source, query, max_results)
    return [_overview(r) for r in records]


@tool(read_only=True)
async def get_paper(paper: str) -> dict[str, Any]:
    """The details of one paper. paper is a reference from search_sources
    ('arxiv:2401.12345', 'pubmed:33649564', 'openalex:W7221365635') or a
    stored paper_id ('17').

    A reference is looked up live at the source. If the paper is stored, the
    result also has its paper_id, parse_mode (null = abstract-only), chunk
    count, and methods and results summaries.
    """
    if paper.strip().isdigit():
        stored = await db_get_paper(int(paper))
        if stored is None:
            raise ValueError(f"No stored paper has paper_id {paper}.")
        return {"paper_id": stored.pop("id"), **stored}

    record = await _lookup(paper)
    details = {
        **_overview(record),
        "abstract": record["abstract_text"],
        "doi": record["doi"],
        "url": record["hyperlink"],
        "pdf_url": record["pdf_url"],
        "keywords": record["categories"],
        "paper_id": None,
    }
    paper_id = await find_paper(record["src_db"], record["source_id"])
    if paper_id is not None:
        stored = await db_get_paper(paper_id)
        details.update(
            paper_id=paper_id, parse_mode=stored["parse_mode"], chunks=stored["chunks"],
            methods_summary=stored["methods_summary"], results_summary=stored["results_summary"],
        )
    return details


@tool
async def save_paper(paper: str) -> dict[str, Any]:
    """Stores a paper as abstract-only: its metadata and its abstract, which
    search_papers then finds. No PDF. paper is a reference from
    search_sources, e.g. 'arxiv:2401.12345'. A second save updates the
    stored paper. Returns its paper_id."""
    record = await _lookup(paper)
    return {"paper_id": await save_record(record), "title": record["title"]}


@tool
async def ingest_pdf(paper: str) -> dict[str, Any]:
    """Makes a full-text paper: parses the PDF into chunks with pages, and
    makes the methods and results summaries. Takes from about 1 minute to
    several minutes (the parse runs on the CPU while the GPU is busy).

    paper is a reference ('arxiv:2401.12345'; the paper is saved first, and
    its PDF is downloaded) or the absolute path of a local PDF. A local PDF
    goes to its stored paper (found by its DOI or its title), else to a new
    paper (from OpenAlex by its DOI, else the model also extracts its title,
    authors and abstract). Returns the paper_id, the chunk count and, under
    "pending", each step that failed. A pending step does not stop the
    search: the chunks are stored.
    """
    path = Path(paper).expanduser().resolve()
    if path.is_file():
        content = path.read_bytes()
        if not content.startswith(b"%PDF"):
            raise ValueError(f"{path} is not a PDF.")
        paper_id, _ = await paper_for_pdf(path)
        stored = await db_get_paper(paper_id)
        if stored["parse_mode"]:
            raise ValueError(f"{path} is the PDF of paper {paper_id}, which already has full text.")
        await queue_pdf(paper_id, str(path))  # clears an earlier failure of its download
        filename, local = path.name, stored["title"] is None
    else:
        record = await _lookup(paper)
        if not record["pdf_url"]:
            raise ValueError(f"{paper} has no PDF link. Download the PDF and give its local path.")
        content = await asyncio.to_thread(download_pdf, record["pdf_url"])
        paper_id = await save_record(record)
        filename, local = f"{record['source_id'].replace('/', '_')}.pdf", False

    pending = await ingest_full_text(paper_id, filename, content, extract_metadata=local)
    stored = await db_get_paper(paper_id)
    return {
        "paper_id": paper_id,
        "title": stored["title"],
        "parse_mode": stored["parse_mode"],
        "chunks": stored["chunks"],
        "pending": pending,
    }


@tool(read_only=True)
async def search_papers(query: str, facet: str = "concept", max_results: int = 5) -> list[dict[str, Any]]:
    """Hybrid search (meaning and keywords) over the stored papers.

    facet selects what is searched and returned as "text":
    - "concept": the title and the abstract (all papers).
    - "methods" or "results": the summaries of the full text (full-text
      papers only).
    parse_mode null = abstract-only paper (no chunks).
    """
    if facet not in FACETS:
        raise ValueError(f"facet must be one of {FACETS}.")
    rows = await db_search_papers(query, await _embed_one(query), facet, max_results)
    return [
        {
            "paper_id": r["id"],
            "ref": f"{r['source']}:{r['source_id']}" if r["source"] else None,
            "title": r["title"],
            "authors": r["authors"],
            "published_date": r["published_date"],
            "venue": r["venue"],
            "parse_mode": r["parse_mode"],
            "keywords": r["keywords"],
            "text": r["text"],
            "score": round(r["score"], 4),
        }
        for r in rows
    ]


def _passage(r: dict[str, Any]) -> dict[str, Any]:
    """A chunk as search_chunks and get_chunks return it."""
    return {
        "paper_id": r["paper_id"],
        "title": r["title"],
        "chunk_index": r["chunk_index"],
        "section": r["section"],
        "pages": str(r["page_start"]) if r["page_start"] == r["page_end"]
        else f"{r['page_start']}-{r['page_end']}",
        "content": r["content"],
        "figures": [
            {
                "file": str(PAPERS_DIR / str(r["paper_id"]) / "images" / name),
                "url": f"{DASHBOARD_URL}/papers/{r['paper_id']}/files/images/{name}",
            }
            for name in _FIGURE.findall(r["content"])
        ],
        "links": json.loads(r["links"]),
    }


@tool(read_only=True)
async def search_chunks(
        query: str, paper_ids: Optional[list[int]] = None, max_results: int = 5,
) -> list[dict[str, Any]]:
    """Hybrid search over the full text of full-text papers. paper_ids
    limits it to these papers (from search_papers); none = all papers.
    Returns each passage with its paper and pages: quote the content exactly
    and cite the title and the page. A table in a passage keeps its rows.
    "figures" lists each figure that the passage shows (`![](name)`), with
    its file on the server and its link in the phdcode interface; its
    caption is usually the next line of the content. "section" is the
    headings above the passage; "links" are the chunks that it refers to
    (kind figure, table, ref or citation): get_chunks reads them."""
    rows = await db_search_chunks(query, await _embed_one(query), paper_ids, max_results)
    return [{**_passage(r), "score": round(r["score"], 4)} for r in rows]


@tool(read_only=True)
async def get_chunks(paper_id: int, chunk_indexes: list[int]) -> list[dict[str, Any]]:
    """Reads chunks of one full-text paper by their chunk_index, in text
    order, at most 10: the text before or after a passage (its chunk_index
    - 1 or + 1), or the targets of its links (a figure, a table, a section,
    an entry of the reference list)."""
    if not 1 <= len(chunk_indexes) <= 10:
        raise ValueError("Give 1 to 10 chunk indexes.")
    rows = await db_get_chunks(paper_id, chunk_indexes)
    if not rows:
        raise ValueError(f"Paper {paper_id} has no chunks with these indexes.")
    return [_passage(r) for r in rows]


@tool
async def add_saved_search(source: str, query: str, interest: str, threshold: float = 0.3) -> dict[str, Any]:
    """Adds a standing query that the daily sweep runs.

    source and query are as in search_sources. interest is a sentence or two
    about what the search is for; the digest orders the relevant hits by it.
    Triage judges each new hit against the user's library, not against the
    interest. threshold (0 to 1) is the triage probability a hit needs to go
    into the digest; 0.3 kept 41 of 71 hits and missed 2 of 24 relevant ones
    in the test of 2026-10-12.
    """
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be between 0 and 1.")
    saved_search_id = await db_add_saved_search(sources.check_source(source), query, interest, threshold)
    return {"saved_search_id": saved_search_id}


@tool(read_only=True)
async def list_saved_searches() -> list[dict[str, Any]]:
    """The saved searches that are not cancelled."""
    return [
        {
            **{k: v for k, v in r.items() if k not in ("id", "created_at", "last_swept_at")},
            "saved_search_id": r["id"],
            "threshold": round(r["threshold"], 3),  # a REAL column: 0.6 reads as 0.6000000238
            "created_at": r["created_at"].isoformat(),
            "last_swept_at": r["last_swept_at"].isoformat() if r["last_swept_at"] else None,
        }
        for r in await db_list_saved_searches()
    ]


@tool
async def cancel_saved_search(saved_search_id: int) -> str:
    """Stops a saved search. Its old digests stay."""
    if not await db_cancel_saved_search(saved_search_id):
        raise ValueError(f"No active saved search has id {saved_search_id}.")
    return f"Saved search {saved_search_id} is cancelled."


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server.run()


if __name__ == "__main__":
    main()
