import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, NamedTuple, Optional

import requests

from dotenv import load_dotenv

from ..documents import PAPERS_DIR, PARSE_VRAM_MIB, first_pages, parse_seconds, text_path_for
from ..gpu import short_job
from ..embeddings import embed
from ..generation import generate
from ..prompts import (
    chunk_digest_prompt,
    document_metadata_prompt,
    methods_summary_prompt,
    results_summary_prompt,
)
from ..sources import lookup

from .db import (
    add_paper_keywords,
    find_paper_by_doi,
    find_paper_by_pdf_source,
    find_paper_in_text,
    full_text_papers,
    get_paper,
    pending_papers,
    queue_pdf,
    queued_pdfs,
    save_paper,
    set_facet,
    set_full_text,
    set_pdf_error,
    update_paper_metadata,
)

load_dotenv()

log = logging.getLogger(__name__)

CHUNK_SIZE = int(os.getenv("DOCUMENT_CHUNK_SIZE", "1500"))
CHUNK_OVERLAP = int(os.getenv("DOCUMENT_CHUNK_OVERLAP", "200"))

# Map-reduce chunk size for summary/methods/results generation -- distinct
# from CHUNK_SIZE above (that one is tuned for RAG retrieval granularity).
# Ollama's default context window (4096 tokens on this deployment) silently
# truncates a whole-document prompt passed in one call -- confirmed cause of
# summaries drifting toward just a document's tail, and likely some
# metadata-extraction JSON failures. Raising num_ctx per-call was tried and
# rejected: on this hardware it triggers a catastrophic decode-speed cliff
# at real document lengths (measured ~0.85 tok/s at a ~14k-token prompt,
# vs. ~77 tok/s at the default context -- a ~90x slowdown, likely GQA
# attention cost growing with a much deeper KV cache, possibly compounded
# by immature long-context kernels on this Blackwell-generation GPU). Map
# each chunk into a short digest at the cheap default context (fast, no
# truncation risk since each chunk is small), then reduce over the
# concatenated digests -- which stays well within the default context even
# for long documents -- using the existing methods/results summary prompts
# unchanged.
MAP_CHUNK_SIZE = int(os.getenv("DOCUMENT_MAP_CHUNK_SIZE", "8000"))
MAP_CHUNK_OVERLAP = 300

# documents.py parses with no OCR, on the GPU if that is faster, else on the
# CPU (ADR-0017). The drain upgrades
# a 'text' paper to 'balanced'.
PARSE_MODE = "text"

# The metadata of a local PDF, from its first chunk.
PAPER_METADATA_SCHEMA = {
    "title" : (str, ...),
    "authors" : (list[str], ...),
    "keywords" : (list[str], ...),
    "abstract" : (str, ...),
}

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# marker's paginate_output line before each page: "{0}" + 48 dashes.
_PAGE_SEPARATOR_RE = re.compile(r"^\{(\d+)\}-{3,}$")
_FIGURE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_BLOCK = ("|", "![")  # the first characters of a table or a figure unit
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _clean_heading(title: str) -> str:
    """The plain text of a heading: no HTML anchor, no bold or italic marks."""
    return " ".join(re.sub(r"<[^>]+>|[*_]+", "", title).split())


class Chunk(NamedTuple):
    content: str
    page_start: int  # 1-indexed
    page_end: int    # 1-indexed, inclusive
    section: str = ""  # the headings above the chunk, "Methods > Data"


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[Chunk]:
    """Splits text into overlapping chunks that only ever break at sentence
    or line boundaries — never mid-sentence — and reports the pages of each
    chunk. A page separator line from marker sets the page of the lines
    after it; text before the first separator is page 1.

    Text is first split into lines, then each line into sentences, giving a
    flat ordered list of (page, sentence) units. Units are greedily
    accumulated into a chunk until adding the next one would exceed
    chunk_size; overlap re-includes enough trailing units from the previous
    chunk (by approximate character length, not raw slicing) that the next
    chunk starts with recent context instead of cold. Returns [] for empty/
    whitespace-only text.

    Two kinds of block are one unit each, with their lines, so that a search
    result can show them: a Markdown table (consecutive lines that start
    with "|"), and figure links ("![](name)") with the caption line after
    them. A large block is a chunk of its own, and the overlap never repeats
    a block.

    Each chunk has the section path of its first unit: the Markdown headings
    above it, from the top level down.
    """
    units: list[tuple[int, str, str]] = []  # (page, text, section)
    page = 1
    block: list[str] = []  # a table, or figure links that wait for their caption
    headings: list[tuple[int, str]] = []  # (level, title) from the top level down

    def section() -> str:
        return " > ".join(title for _, title in headings)

    def end_block() -> None:
        if block:
            units.append((page, "\n".join(block), section()))
            block.clear()

    for line in text.splitlines():
        stripped = line.strip()
        is_row = stripped.startswith("|")
        if block and is_row != block[0].startswith("|"):
            end_block()  # a table ends, or figures with no caption end
        if is_row or _FIGURE_RE.fullmatch(stripped):
            block.append(stripped)
            continue
        if not stripped:
            continue
        separator = _PAGE_SEPARATOR_RE.match(stripped)
        if separator:
            end_block()
            page = int(separator.group(1)) + 1
            continue
        if block:  # the caption of the figures
            block.append(stripped)
            end_block()
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            while headings and headings[-1][0] >= level:
                headings.pop()
            if title := _clean_heading(heading.group(2)):
                headings.append((level, title))
        for sentence in _SENTENCE_SPLIT_RE.split(stripped):
            sentence = sentence.strip()
            if sentence:
                units.append((page, sentence, section()))
    end_block()

    chunks: list[Chunk] = []
    n = len(units)
    i = 0
    while i < n:
        length = 0
        j = i
        while j < n:
            addition = len(units[j][1]) + (1 if j > i else 0)
            if j > i and length + addition > chunk_size:
                break
            length += addition
            j += 1

        current_units = units[i:j]
        content = current_units[0][1]
        for (_, previous, _), (_, sentence, _) in zip(current_units, current_units[1:]):
            content += ("\n" if sentence.startswith(_BLOCK) or previous.startswith(_BLOCK) else " ") + sentence
        chunks.append(Chunk(content, current_units[0][0], current_units[-1][0], current_units[0][2]))

        if j >= n:
            break

        if len(current_units) == 1:
            i = j
            continue

        # Step back far enough to re-include ~overlap chars of trailing
        # units, so the next chunk repeats recent context.
        back_chars = 0
        step_back = 0
        for idx in range(j - 1, i, -1):
            if units[idx][1].startswith(_BLOCK):
                break
            back_chars += len(units[idx][1]) + 1
            step_back += 1
            if back_chars >= overlap:
                break
        i = max(j - step_back, i + 1)  # always make forward progress
    return chunks


class Link(NamedTuple):
    from_index: int
    to_index: int
    kind: str   # figure, table, ref (another part of the paper) or citation (the reference list)
    label: str  # the text of the reference, e.g. "Fig. 3" or "Brody et al., 2022"


# A mention of a figure or a table: "Fig. 3", "Figure 3", "Figs. 3", "Table 2".
_MENTION_RE = re.compile(r"(Extended Data )?\b(Fig(?:ure)?s?\.?|Tables?)\s*(\d+)\b")
# Its caption: after a figure link, at the start of a line, or after "**",
# with ":", "|" or "." after the number ("**Fig. 1 | Overview", "Table 2: Accuracy").
_CAPTION_RE = re.compile(r"(?:^|\n|\*\*|\)\n)\s*(?:<span[^>]*></span>)?\**\s*"
                         r"(Extended Data )?(Fig(?:ure)?\.?|Table)\s*(\d+)\s*\**\s*[.:|]")
# marker's internal links and their targets, when the PDF has hyperlinks.
_LINK_RE = re.compile(r"\[([^\]]*)\]\(#(page-\d+-\d+)\)")
_ANCHOR_RE = re.compile(r'<span id="(page-\d+-\d+)">')
_REFERENCES_RE = re.compile(r"\b(references|bibliography)\b", re.IGNORECASE)


def _figure_key(extended: Optional[str], word: str, number: str) -> tuple[str, str]:
    kind = "table" if word.lower().startswith("table") else "figure"
    return ("extended " if extended else "") + kind, number


def chunk_links(chunks: list[Chunk]) -> list[Link]:
    """The links between the chunks of one paper, at most one for each pair:
    a mention of a figure or a table to the chunk with its caption, and
    marker's internal links to the chunk with their target (a citation if
    the target is in the reference list). The order of the chunks is no
    link: the neighbours of chunk i are i - 1 and i + 1."""
    captions: dict[tuple[str, str], int] = {}
    anchors: dict[str, int] = {}
    for i, chunk in enumerate(chunks):
        for m in _CAPTION_RE.finditer(chunk.content):
            captions.setdefault(_figure_key(*m.groups()), i)
        for anchor in _ANCHOR_RE.findall(chunk.content):
            anchors.setdefault(anchor, i)
    links: dict[tuple[int, int], Link] = {}
    for i, chunk in enumerate(chunks):
        for m in _MENTION_RE.finditer(chunk.content):
            key = _figure_key(*m.groups())
            j = captions.get(key)
            if j is not None and j != i:
                links.setdefault((i, j), Link(i, j, key[0].split()[-1], m.group(0)))
        for text, anchor in _LINK_RE.findall(chunk.content):
            j = anchors.get(anchor)
            if j is not None and j != i:
                kind = "citation" if _REFERENCES_RE.search(chunks[j].section) else "ref"
                label = re.sub(r"[\\()\[\]]", "", _clean_heading(text)).strip(";, –-")[:80]
                links.setdefault((i, j), Link(i, j, kind, label))
    return list(links.values())


async def save_record(record: dict[str, Any], concept_embedding: Optional[list[float]] = None,
                      origin: Optional[str] = None) -> int:
    """Stores a source record (sources.search or sources.lookup) as an
    abstract-only paper, or updates the stored paper. Embeds the abstract,
    unless the caller gives its embedding. origin: see db.save_paper.
    Returns the paper_id."""
    abstract = record["abstract_text"] or None
    if abstract and concept_embedding is None:
        [concept_embedding] = await asyncio.to_thread(embed, [abstract])
    return await save_paper(
        record["src_db"], record["source_id"], record["title"], record["authors"],
        abstract, concept_embedding if abstract else None,
        published_date=(record["published_date"] or "")[:10] or None,
        venue=record["venue"], doi=record["doi"], url=record["hyperlink"],
        pdf_url=record["pdf_url"], keywords=record["categories"], origin=origin,
    )


async def ingest_full_text(
        paper_id: int,
        filename: str,
        content: bytes,
        extract_metadata: bool = False,
        model_name: Optional[str] = None,
        run_models: bool = True,
) -> list[str]:
    """Makes a full-text paper of a stored paper: parses the PDF into
    PAPERS_DIR/<paper_id>/, replaces its chunks (with pages and CPU
    embeddings), then makes the methods and results facets.

    run_models=False leaves the model steps pending for `phdcode summarize`
    (phdcode ingest: the parses of a queue use the GPU while llama-server
    is stopped, and the summaries start it after them).

    extract_metadata=True is for a local PDF, which has no source record:
    the model also extracts the title, the authors, the keywords and the
    abstract, and the abstract becomes the concept facet.

    The chunks are stored before any model call, so the paper is searchable
    even if a model step fails. A model step can fail for any reason: the
    server is stopped, no model is set, the reply is not valid. Then the
    paper stays full-text and its summaries are pending (ADR-0017). Returns
    one line for each failed step, for the caller to show; [] = all done.
    """
    pdf_path = PAPERS_DIR / str(paper_id) / Path(filename).name
    # One paper, one PDF: a new ingest replaces all files of the paper, so
    # that no figure or table of an earlier PDF stays (the caller already
    # holds the content).
    shutil.rmtree(pdf_path.parent, ignore_errors=True)
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(content)
    log.info("paper %s: parsing %s", paper_id, pdf_path)
    text_path = await _parse_pdf(pdf_path)
    text = await index_text(paper_id, PARSE_MODE, pdf_path, text_path)
    if not run_models:
        return []
    return await run_model_steps(paper_id, text, extract_metadata, model_name)


async def index_text(paper_id: int, parse_mode: str, pdf_path: Path, text_path: Path) -> str:
    """Chunks the parsed text of a paper, embeds the chunks (CPU), finds
    their links, and replaces the stored chunks. Returns the text."""
    text = Path(text_path).read_text()
    chunks = chunk_text(text)
    embeddings = await asyncio.to_thread(embed, [c.content for c in chunks]) if chunks else []
    links = chunk_links(chunks)
    await set_full_text(paper_id, parse_mode, str(pdf_path), str(text_path), chunks, embeddings, links)
    log.info("paper %s: indexed %d chunks, %d links", paper_id, len(chunks), len(links))
    return text


async def rechunk(paper_ids: Optional[list[int]] = None) -> list[str]:
    """`phdcode rechunk`: indexes the parsed text of each full-text paper
    again (all, or paper_ids), with no new parse and no model step: after a
    change to the chunker or to the links. Returns one line for each paper."""
    report = []
    for paper in await full_text_papers(paper_ids):
        try:
            await index_text(paper["id"], paper["parse_mode"], Path(paper["pdf_path"]), Path(paper["text_path"]))
            report.append(f"paper {paper['id']}: indexed again")
        except Exception as e:  # a missing text file stops only this paper
            log.exception("paper %s: rechunk failed", paper["id"])
            report.append(f"paper {paper['id']}: failed: {type(e).__name__}: {e}")
    return report


def download_pdf(url: str) -> bytes:
    response = requests.get(url, timeout=60, headers={"User-Agent": "phdcode"})
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        raise ValueError(
            f"{url} did not return a PDF. Download the PDF and call ingest_pdf with its local path."
        )
    return response.content


_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")
_ARXIV_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})")


async def paper_for_pdf(path: Path) -> tuple[int, str]:
    """The paper of a local PDF, and how it was found, in this order:
    0. a paper already has this file in its PDF queue (a second
       `phdcode queue` of the file);
    1. a DOI on its first two pages is stored (an arXiv ID counts as the DOI
       10.48550/arxiv.<id>, which OpenAlex gives to arXiv papers);
    2. the title of a stored paper is on its first page;
    3. the DOI that OpenAlex gives for that DOI is stored (OpenAlex leads a
       preprint DOI to the journal version);
    4. OpenAlex knows the DOI: a new abstract-only paper;
    5. else a new empty paper: `phdcode summarize` (or ingest_pdf) reads its
       title, authors and abstract from the parsed text.
    Steps 0 to 2 need no network. Tested on the 31 parsed PDFs of
    2026-10-12: each one went to its own paper. The journal PDFs show a DOI;
    the arXiv PDFs show only the arXiv ID."""
    if (paper_id := await find_paper_by_pdf_source(str(path))) is not None:
        return paper_id, "this file is in its queue"
    pages = await asyncio.to_thread(first_pages, path)
    text = " ".join(pages)
    doi_match, arxiv_match = _DOI_RE.search(text), _ARXIV_RE.search(text)
    doi = (doi_match.group(0).rstrip(".,;)]") if doi_match
           else f"10.48550/arxiv.{arxiv_match.group(1)}" if arxiv_match else None)
    if doi and (paper_id := await find_paper_by_doi(doi)) is not None:
        return paper_id, f"stored paper, DOI {doi}"
    if pages and (paper_id := await find_paper_in_text(pages[0])) is not None:
        return paper_id, "stored paper, its title is on page 1"
    record = await asyncio.to_thread(lookup, f"openalex:doi:{doi}") if doi else None
    if record and record["doi"] and (paper_id := await find_paper_by_doi(record["doi"])) is not None:
        return paper_id, f"stored paper, DOI {record['doi']} (OpenAlex, for {doi})"
    if record:
        return await save_record(record), f"new paper from {record['ref']}"
    return await save_paper(None, None, None, [], None, None), "new paper, metadata from the text at summarize"


async def queue_local_pdfs(paths: list[Path], paper_id: Optional[int] = None) -> list[str]:
    """`phdcode queue`: each local PDF into the queue of its paper
    (paper_for_pdf, or paper_id for one PDF). A paper that has full text is
    skipped. The file must stay in place until `phdcode ingest` copies it.
    Returns one line for each PDF."""
    if paper_id is not None and len(paths) != 1:
        raise ValueError("--paper takes one PDF.")
    report = []
    for path in paths:
        path = path.expanduser().resolve()
        if not path.is_file() or not path.read_bytes().startswith(b"%PDF"):
            report.append(f"{path}: skipped, not a PDF")
            continue
        target, how = (paper_id, "given") if paper_id is not None else await paper_for_pdf(path)
        paper = await get_paper(target)
        if paper is None:
            report.append(f"{path}: skipped, no paper {target}")
        elif paper["parse_mode"]:
            report.append(f"{path}: skipped, paper {target} already has full text ({paper['title']})")
        else:
            await queue_pdf(target, str(path))
            report.append(f"{path}: queued for paper {target} ({how}): {paper['title'] or 'no title yet'}")
    return report


async def ingest_queue(limit: Optional[int] = None) -> list[str]:
    """`phdcode ingest`: makes a full-text paper of each paper in the PDF
    queue (db.queued_pdfs), one at a time, with no model steps. A failure
    (a download that is not a PDF, a parse that fails) is stored in
    pdf_error, and the queue goes on. limit: at most this many papers.
    Returns one line for each paper."""
    report = []
    for paper in (await queued_pdfs())[:limit]:
        source = paper["pdf_source"]
        try:
            if source.startswith(("http://", "https://")):
                content = await asyncio.to_thread(download_pdf, source)
                filename = f"{(paper['source_id'] or paper['id'])}.pdf".replace("/", "_")
            else:
                content, filename = Path(source).read_bytes(), Path(source).name
                if not content.startswith(b"%PDF"):
                    raise ValueError(f"{source} is not a PDF.")
            await ingest_full_text(paper["id"], filename, content, run_models=False)
            report.append(f"paper {paper['id']}: full text")
        except Exception as e:  # the next paper still runs; the reason is in pdf_error
            log.exception("paper %s: the PDF failed", paper["id"])
            error = f"{type(e).__name__}: {e}"[:500]
            await set_pdf_error(paper["id"], error)
            report.append(f"paper {paper['id']}: failed: {error}")
    return report


async def run_model_steps(
        paper_id: int, text: str, extract_metadata: bool, model_name: Optional[str] = None,
) -> list[str]:
    """The model steps of a full-text paper: the metadata (a local PDF
    only), then the summaries. Returns one line for each failed step."""
    steps = [("metadata", extract_paper_metadata)] if extract_metadata else []
    steps.append(("summaries", summarize_full_text))
    failures = []
    for name, step in steps:
        try:
            await step(paper_id, text, model_name)
        except Exception as e:  # any failure leaves the step pending; the chunks stay
            log.exception("paper %s: %s pending", paper_id, name)
            failures.append(f"{name} pending: {type(e).__name__}: {e}")
    return failures


async def run_pending_steps(model_name: Optional[str] = None) -> list[str]:
    """Runs the model steps of each full-text paper that has a pending
    step. Returns one line for each paper."""
    report = []
    for paper in await pending_papers():
        text = Path(paper["text_path"]).read_text()
        failures = await run_model_steps(paper["id"], text, paper["needs_metadata"], model_name)
        report.append(f"paper {paper['id']}: " + ("; ".join(failures) if failures else "done"))
    return report


async def _parse_pdf(pdf_path: Path) -> Path:
    """Runs documents.py in a subprocess (see its docstring), on the GPU if
    that is faster (gpu.short_job), else on the CPU. A parse that fails on
    the GPU (for example, out of memory) runs again on the CPU. Its output
    goes to parse.log next to the PDF. Returns the path of the markdown file."""
    log_path = pdf_path.parent / "parse.log"
    with log_path.open("wb") as log_file:

        async def run(device: str) -> int:
            log.info("parse of %s on %s", pdf_path.name, device)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "llm_engine.documents", str(pdf_path), device,
                stdin=asyncio.subprocess.DEVNULL, stdout=log_file, stderr=log_file,
            )
            return await process.wait()

        gpu_seconds, cpu_seconds = await asyncio.to_thread(parse_seconds, pdf_path)
        job = short_job(PARSE_VRAM_MIB, gpu_seconds, cpu_seconds, f"parse of {pdf_path.name}")
        device = await asyncio.to_thread(job.__enter__)  # it can wait for the lock
        try:
            code = await run(device)
        finally:
            job.__exit__(None, None, None)
        if code != 0 and device == "cuda":
            log.warning("the parse on the GPU failed (exit code %s); it runs again on the CPU", code)
            code = await run("cpu")
    if code != 0:
        raise RuntimeError(f"The PDF parse failed (exit code {code}); see {log_path}.")
    return text_path_for(pdf_path)


async def extract_paper_metadata(paper_id: int, text: str, model_name: Optional[str] = None) -> None:
    """Writes the title, the authors, the keywords and the concept facet (the
    abstract) of a local PDF. Uses the first map chunk only: these are on the
    first page, and a digest could change the exact text."""
    first = chunk_text(text, chunk_size=MAP_CHUNK_SIZE, overlap=MAP_CHUNK_OVERLAP)[:1]
    if not first:
        return
    metadata = await asyncio.to_thread(
        generate, document_metadata_prompt(first[0].content), model_name, PAPER_METADATA_SCHEMA,
    )
    await update_paper_metadata(paper_id, metadata["title"], metadata["authors"])
    await add_paper_keywords(paper_id, metadata["keywords"])
    if metadata["abstract"].strip():
        [abstract_embedding] = await asyncio.to_thread(embed, [metadata["abstract"]])
        await set_facet(paper_id, "concept", metadata["abstract"], abstract_embedding)


async def summarize_full_text(paper_id: int, text: str, model_name: Optional[str] = None) -> None:
    """Writes the methods and results facets of a full-text paper. The drain
    calls this for the pending summaries. Map-reduce, not one call over the
    raw text (see MAP_CHUNK_SIZE)."""
    map_chunks = chunk_text(text, chunk_size=MAP_CHUNK_SIZE, overlap=MAP_CHUNK_OVERLAP)
    if not map_chunks:
        return

    digests = [
        await asyncio.to_thread(generate, chunk_digest_prompt(chunk.content), model_name)
        for chunk in map_chunks
    ]
    digest_text = "\n\n".join(
        f"--- Excerpt {i + 1} digest ---\n{digest}" for i, digest in enumerate(digests)
    )
    log.info("paper %s: digested %d sections", paper_id, len(map_chunks))

    methods = await asyncio.to_thread(generate, methods_summary_prompt(digest_text), model_name)
    results = await asyncio.to_thread(generate, results_summary_prompt(digest_text), model_name)
    methods_embedding, results_embedding = await asyncio.to_thread(embed, [methods, results])
    # results first: a paper with no methods summary counts as pending.
    await set_facet(paper_id, "results", results, results_embedding)
    await set_facet(paper_id, "methods", methods, methods_embedding)
    log.info("paper %s: summaries done", paper_id)


if __name__ == "__main__":
    # python -m llm_engine.memory.document_index
    separator = "-" * 48
    text = f"Title line.\n\n{{0}}{separator}\n\nOne. Two.\n\n{{1}}{separator}\n\nThree.\n"
    assert chunk_text(text, chunk_size=1000) == [Chunk("Title line. One. Two. Three.", 1, 2)]
    pages = [(c.page_start, c.page_end) for c in chunk_text(text, chunk_size=5, overlap=0)]
    assert pages == [(1, 1), (1, 1), (1, 1), (2, 2)], pages
    assert chunk_text("  \n\n") == []
    table = "Before. Text.\n| A | B |\n|---|---|\n| 1 | 2 |\nAfter."
    assert [c.content for c in chunk_text(table, chunk_size=1000)] == [
        "Before. Text.\n| A | B |\n|---|---|\n| 1 | 2 |\nAfter."]
    # A table larger than a chunk is a chunk of its own; no overlap repeats it.
    parts = [c.content for c in chunk_text(table, chunk_size=20, overlap=50)]
    assert parts.count("| A | B |\n|---|---|\n| 1 | 2 |") == 1, parts
    figure = "Text. More.\n\n![](_page_1_Figure_1.jpeg)\n\n*Fig. 1. A caption. Two.*\n\nAfter."
    assert [c.content for c in chunk_text(figure, chunk_size=1000)] == [
        "Text. More.\n![](_page_1_Figure_1.jpeg)\n*Fig. 1. A caption. Two.*\nAfter."]
    parts = [c.content for c in chunk_text(figure, chunk_size=10, overlap=50)]
    assert "![](_page_1_Figure_1.jpeg)\n*Fig. 1. A caption. Two.*" in parts, parts
    print("chunk_text: ok")

    paper = (
        "# 1 Introduction\n\nAs Fig. 1 shows, see Table 2 and [Smith, 2020](#page-3-0). More text here.\n\n"
        "![](_page_1_Figure_1.jpeg)\n\n**Fig. 1 | Overview.** A caption.\n\n"
        "Table 2: Accuracy of the models.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
        "# References\n\n<span id=\"page-3-0\"></span>Smith, J. 2020. A paper.\n"
    )
    pieces = chunk_text(paper, chunk_size=60, overlap=0)
    assert pieces[0].section == "1 Introduction" and pieces[-1].section == "References", pieces
    found = {(link.kind, link.label) for link in chunk_links(pieces)}
    assert found == {("figure", "Fig. 1"), ("table", "Table 2"), ("citation", "Smith, 2020")}, found
    print("chunk_links: ok")

    page = "Br J Cancer (2021) 124:686; https://doi.org/10.1038/s41416-020-01122-x. arXiv:2004.09666v2 [cs.CV]"
    assert _DOI_RE.search(page).group(0).rstrip(".,;)]") == "10.1038/s41416-020-01122-x"
    assert _ARXIV_RE.search(page).group(1) == "2004.09666"
    print("pdf ids: ok")
