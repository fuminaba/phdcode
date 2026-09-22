import asyncio
import os
import re
from typing import NamedTuple, Optional

from dotenv import load_dotenv

try:
    from ..documents import UPLOADS_DIR, ingest_document
    from ..embeddings import embed
    from ..frontend.base import ChannelFeedback
    from ..prompts import (
        chunk_digest_prompt,
        document_metadata_prompt,
        document_note,
        document_summary_prompt,
        methods_summary_prompt,
        results_summary_prompt,
    )
    from ..schemas import DOCUMENT_METADATA_SCHEMA
    from ..tgi import GenerativeService
except ImportError:
    from documents import UPLOADS_DIR, ingest_document
    from embeddings import embed
    from frontend.base import ChannelFeedback
    from prompts import (
        chunk_digest_prompt,
        document_metadata_prompt,
        document_note,
        document_summary_prompt,
        methods_summary_prompt,
        results_summary_prompt,
    )
    from schemas import DOCUMENT_METADATA_SCHEMA
    from tgi import GenerativeService

from .db import (
    add_document_keywords,
    create_document,
    save_document_chunks,
    set_document_paths,
    update_document_metadata,
    update_document_methods_summary,
    update_document_results_summary,
    update_document_summary,
)

load_dotenv()

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
# for long documents -- using the existing document/methods/results summary
# prompts unchanged.
MAP_CHUNK_SIZE = int(os.getenv("DOCUMENT_MAP_CHUNK_SIZE", "8000"))
MAP_CHUNK_OVERLAP = 300

_DEFAULT_MODEL_NAME = "qwen3:14b"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


class ChunkResult(NamedTuple):
    content: str
    start_line: int  # 1-indexed, inclusive
    end_line: int     # 1-indexed, inclusive


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[ChunkResult]:
    """Splits text into overlapping chunks that only ever break at sentence
    or line boundaries — never mid-sentence — and reports each chunk's
    source start_line/end_line (1-indexed, inclusive) in the original text.

    Text is first split into lines, then each line into sentences, giving a
    flat ordered list of (line_number, sentence) units. Units are greedily
    accumulated into a chunk until adding the next one would exceed
    chunk_size; overlap re-includes enough trailing units from the previous
    chunk (by approximate character length, not raw slicing) that the next
    chunk starts with recent context instead of cold. Returns [] for empty/
    whitespace-only text.
    """
    if not text.strip():
        return []

    units: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(stripped):
            sentence = sentence.strip()
            if sentence:
                units.append((line_number, sentence))

    if not units:
        return []

    chunks: list[ChunkResult] = []
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
        if j == i:
            j = i + 1  # a single unit longer than chunk_size: take it alone

        current_units = units[i:j]
        content = " ".join(sentence for _, sentence in current_units)
        chunks.append(ChunkResult(content, current_units[0][0], current_units[-1][0]))

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


async def ingest_and_index_document(
        user_id: str,
        conversation_id: Optional[int],
        filename: str,
        content: bytes,
        model_name: Optional[str] = None,
        feedback: Optional[ChannelFeedback] = None,
) -> tuple[int, str]:
    """The full RAG-oriented document pipeline: register the DB row, extract
    (marker-pdf, off-thread) into a per-upload folder that embeds this row's
    own id, chunk + embed the extracted text for RAG retrieval, then
    generate a general summary (which also covers conceptual/theoretical
    framing), title/authors/keywords, and two more summary "flavors"
    (methodological, results), each with its own embedding. Everything is
    persisted onto the documents/document_chunks/keywords tables. Returns
    (document_id, note) — note is prompts.document_note's text, meant to be
    folded into the agent's prompt by the caller (see
    pipeline.handle_incoming_message).

    feedback (see frontend/base.py) is called at each pipeline stage
    boundary with a label + fraction complete in [0, 1] -- purely a
    progress signal, channel-agnostic by design (this module knows nothing
    about Telegram/Django/etc; see frontend/telegram.py for the Telegram
    implementation that turns this into an edited chat message). Stage
    weights (extraction 35%, RAG chunk embedding 10%, map digests 35% split
    evenly across chunks, the 4 reduce/metadata calls 5% each) are rough
    estimates from one real document's timing, not precisely measured per
    stage -- expect drift for very different document shapes.

    Summary/methods/results generation is map-reduce, not one call over the
    raw text: Ollama's default context window (4096 tokens on this
    deployment) silently truncates a whole-document prompt, and raising
    num_ctx per-call was tried and rejected -- it triggers a catastrophic
    decode-speed cliff at real document lengths on this hardware (see
    GenerativeService.generate_text's num_ctx docstring). Instead, the text
    is split into larger map-reduce-sized chunks (MAP_CHUNK_SIZE, distinct
    from the smaller RAG-retrieval CHUNK_SIZE), each digested individually
    at the cheap default context, and the summary/methods/results prompts
    run unchanged against the concatenation of those digests -- short
    enough to stay within the default context even for a long document.
    Metadata is the exception: scoped to just the first map-chunk (title/
    authors/keywords are effectively always on an academic paper's first
    page), since a narrative digest could mangle exact title/author text
    that needs to survive verbatim.

    Deliberately sequential, not concurrent: whether the LLM backend
    actually executes concurrent requests in parallel or just queues them
    depends on server-side config this project doesn't control or know, so
    there's no proven latency win to justify the added complexity. A real
    need for parallel throughput is a job for a proper task queue
    (Redis/Celery), not ad-hoc asyncio concurrency against an opaque server.

    Schema modeled on (a simplified version of) a prior project's
    ResearchArticle/ArticleChunk Django models — kept the reusable
    document+chunks shape, dropped the literature-library-specific fields
    (DOIs, citation graph) this project doesn't need.
    """
    model_name = model_name or _DEFAULT_MODEL_NAME
    feedback = feedback or ChannelFeedback()
    await feedback.start(f"Processing {filename}")
    try:
        result = await _run_ingestion(
            user_id, conversation_id, filename, content, model_name, feedback,
        )
    except Exception:
        await feedback.finish(f"❌ Failed to process {filename}")
        raise
    await feedback.finish(f"✅ Finished processing {filename}")
    return result


async def _run_ingestion(
        user_id: str,
        conversation_id: Optional[int],
        filename: str,
        content: bytes,
        model_name: str,
        feedback: ChannelFeedback,
) -> tuple[int, str]:
    document_id = await create_document(user_id, conversation_id, filename)
    conversation_dir = str(conversation_id) if conversation_id is not None else "standalone"
    dir_path = UPLOADS_DIR / user_id / conversation_dir / str(document_id)

    # marker-pdf and a resident Ollama model can OOM competing for the same
    # GPU's VRAM (confirmed: loading marker's own weights failed outright
    # with Ollama holding ~12GB of a 16GB card) -- unload before extraction
    # rather than tuning batch sizes, which only affects activation memory
    # during inference, not the cost of loading model weights in the first
    # place. Ollama reloads naturally on this pipeline's own next
    # generate_text() call below, or the agent's next turn.
    await asyncio.to_thread(GenerativeService.unload_all_models)
    extracted = await asyncio.to_thread(ingest_document, dir_path, filename, content)
    await set_document_paths(document_id, str(dir_path), str(extracted.text_path))
    await feedback.progress("Extracted text", 0.35)

    text = extracted.text_path.read_text()

    chunks = chunk_text(text)
    if chunks:
        chunk_embeddings = [await asyncio.to_thread(embed, chunk.content) for chunk in chunks]
        await save_document_chunks(document_id, chunks, chunk_embeddings)
    await feedback.progress("Indexed for search", 0.45)

    # Map: digest each (larger, map-reduce-sized) chunk individually at the
    # cheap default context -- fast, and never at risk of truncation since
    # each chunk is small. Metadata is scoped to just the first chunk: title/
    # authors/keywords are effectively always on an academic paper's first
    # page, so there's no need to map-reduce metadata across the whole
    # document (and doing so would risk a narrative digest mangling exact
    # title/author text, which needs to survive verbatim).
    map_chunks = chunk_text(text, chunk_size=MAP_CHUNK_SIZE, overlap=MAP_CHUNK_OVERLAP)
    digests = []
    for i, chunk in enumerate(map_chunks):
        digest_result = await asyncio.to_thread(
            GenerativeService.generate_text, chunk_digest_prompt(chunk.content), model_name=model_name,
        )
        digests.append(digest_result["output"])
        await feedback.progress(
            f"Digesting section {i + 1}/{len(map_chunks)}", 0.45 + 0.35 * (i + 1) / len(map_chunks),
        )
    digest_text = "\n\n".join(
        f"--- Excerpt {i + 1} digest ---\n{digest}" for i, digest in enumerate(digests)
    )
    metadata_source = map_chunks[0].content if map_chunks else text

    # Reduce: the existing summary/methods/results prompts, unchanged, now
    # fed the (much shorter) concatenated digests instead of the raw text --
    # comfortably within the default context even for a long document.
    summary_result = await asyncio.to_thread(
        GenerativeService.generate_text, document_summary_prompt(digest_text), model_name=model_name,
    )
    summary = summary_result["output"]
    summary_embedding = await asyncio.to_thread(embed, summary)
    await update_document_summary(document_id, summary, summary_embedding)
    await feedback.progress("Generated summary", 0.85)

    metadata_result = await asyncio.to_thread(
        GenerativeService.generate_text, document_metadata_prompt(metadata_source),
        response_schema=DOCUMENT_METADATA_SCHEMA, as_json=True, model_name=model_name,
    )
    metadata = metadata_result["output"]
    await update_document_metadata(document_id, metadata.get("title"), metadata.get("authors") or [])
    await add_document_keywords(document_id, metadata.get("keywords") or [])
    await feedback.progress("Extracted metadata", 0.90)

    methods_result = await asyncio.to_thread(
        GenerativeService.generate_text, methods_summary_prompt(digest_text), model_name=model_name,
    )
    methods_summary = methods_result["output"]
    methods_embedding = await asyncio.to_thread(embed, methods_summary)
    await update_document_methods_summary(document_id, methods_summary, methods_embedding)
    await feedback.progress("Summarized methodology", 0.95)

    results_result = await asyncio.to_thread(
        GenerativeService.generate_text, results_summary_prompt(digest_text), model_name=model_name,
    )
    results_summary = results_result["output"]
    results_embedding = await asyncio.to_thread(embed, results_summary)
    await update_document_results_summary(document_id, results_summary, results_embedding)
    await feedback.progress("Summarized results", 1.0)

    return document_id, document_note(filename, str(extracted.text_path))
