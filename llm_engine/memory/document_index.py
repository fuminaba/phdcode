import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple, Optional

from dotenv import load_dotenv

try:
    from ..documents import PAPERS_DIR, text_path_for
    from ..embeddings import embed
    from ..generation import generate
    from ..prompts import (
        chunk_digest_prompt,
        document_metadata_prompt,
        methods_summary_prompt,
        results_summary_prompt,
    )
    from ..schemas import PAPER_METADATA_SCHEMA
except ImportError:
    from documents import PAPERS_DIR, text_path_for
    from embeddings import embed
    from generation import generate
    from prompts import (
        chunk_digest_prompt,
        document_metadata_prompt,
        methods_summary_prompt,
        results_summary_prompt,
    )
    from schemas import PAPER_METADATA_SCHEMA

from .db import add_paper_keywords, set_facet, set_full_text, update_paper_metadata

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

# documents.py parses with no OCR, on the CPU (ADR-0017). The drain upgrades
# a 'text' paper to 'balanced'.
PARSE_MODE = "text"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# marker's paginate_output line before each page: "{0}" + 48 dashes.
_PAGE_SEPARATOR_RE = re.compile(r"^\{(\d+)\}-{3,}$")


class Chunk(NamedTuple):
    content: str
    page_start: int  # 1-indexed
    page_end: int    # 1-indexed, inclusive


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
    """
    units: list[tuple[int, str]] = []
    page = 1
    for line in text.splitlines():
        stripped = line.strip()
        separator = _PAGE_SEPARATOR_RE.match(stripped)
        if separator:
            page = int(separator.group(1)) + 1
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(stripped):
            sentence = sentence.strip()
            if sentence:
                units.append((page, sentence))

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
        content = " ".join(sentence for _, sentence in current_units)
        chunks.append(Chunk(content, current_units[0][0], current_units[-1][0]))

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
            back_chars += len(units[idx][1]) + 1
            step_back += 1
            if back_chars >= overlap:
                break
        i = max(j - step_back, i + 1)  # always make forward progress
    return chunks


async def ingest_full_text(
        paper_id: int,
        filename: str,
        content: bytes,
        extract_metadata: bool = False,
        model_name: Optional[str] = None,
) -> list[str]:
    """Makes a full-text paper of a stored paper: parses the PDF into
    PAPERS_DIR/<paper_id>/, replaces its chunks (with pages and CPU
    embeddings), then makes the methods and results facets.

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
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(content)
    log.info("paper %s: parsing %s", paper_id, pdf_path)
    text_path = await _parse_pdf(pdf_path)
    text = text_path.read_text()

    chunks = chunk_text(text)
    embeddings = await asyncio.to_thread(embed, [c.content for c in chunks]) if chunks else []
    await set_full_text(paper_id, PARSE_MODE, str(pdf_path), str(text_path), chunks, embeddings)
    log.info("paper %s: indexed %d chunks", paper_id, len(chunks))

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


async def _parse_pdf(pdf_path: Path) -> Path:
    """Runs documents.py in a subprocess (see its docstring). Its output goes
    to parse.log next to the PDF. Returns the path of the markdown file."""
    log_path = pdf_path.parent / "parse.log"
    with log_path.open("wb") as log_file:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "llm_engine.documents", str(pdf_path),
            stdin=asyncio.subprocess.DEVNULL, stdout=log_file, stderr=log_file,
        )
        code = await process.wait()
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
    print("chunk_text: ok")
