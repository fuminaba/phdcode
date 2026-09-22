from pathlib import Path
from typing import Any, Optional

try:
    from .registry import tool
except ImportError:
    from registry import tool

try:
    from ..context import current_user_id, run_on_main_loop
except ImportError:
    from context import current_user_id, run_on_main_loop

try:
    from ..documents import UPLOADS_DIR
except ImportError:
    from documents import UPLOADS_DIR

_MAX_LINES_PER_CALL = 400
_MAX_CHARS_PER_CALL = 8000  # defensive cap in case a "line" is unexpectedly huge


@tool
def search_documents(
        query: str,
        max_results: Optional[int] = 5
) -> list[dict[str, Any]]:
    """
    Searches the current user's uploaded documents for ones relevant to
    `query` — a hybrid search combining semantic similarity (embedding of
    `query` vs. each document's summary) with keyword/full-text matching,
    fused into one ranking. Returns each match's title, authors, keywords,
    filename, when it was uploaded, its extracted text_path, and all three
    summary flavors (general — which covers conceptual/theoretical framing
    too — methods, and results) — enough for a multi-angle overview without
    a separate read_document call. If that still isn't enough, call
    read_document with the returned text_path for the full text.
    """
    user_id = current_user_id.get()
    if not user_id:
        return []

    # Lazy, on-call imports: same circular-import reason as memory_tools.py
    # (embeddings.py and memory/db.py both transitively import tgi.py).
    try:
        from ..embeddings import embed
        from ..memory.db import search_documents as db_search_documents
    except ImportError:
        from embeddings import embed
        from memory.db import search_documents as db_search_documents

    query_embedding = embed(query)
    matches = (
        run_on_main_loop(
            db_search_documents(
                user_id,
                query,
                query_embedding,
                max_results or 5
            )
        )
    )
    return [
        {
            "document_id": m["id"],
            "filename": m["filename"],
            "title": m["title"],
            "authors": m["authors"],
            "keywords": m["keywords"],
            "summary": m["summary"],
            "methods_summary": m["methods_summary"],
            "results_summary": m["results_summary"],
            "text_path": m["text_path"],
            "created_at": m["created_at"].isoformat(),
            "relevance": round(m["score"], 4),  # fused rank score, higher = more relevant
        }
        for m in matches
    ]


@tool
def search_document_chunks(
        document_id: int,
        query: str,
        max_results: Optional[int] = 5
) -> list[dict[str, Any]]:
    """
    Searches within one specific document (its document_id, from an
    earlier search_documents call) for the passages most relevant to
    `query` — a hybrid search combining semantic similarity (per-chunk
    embedding) with keyword/full-text matching, fused into one ranking. Use
    this to drill into a document for a specific fact, figure, or detail
    that the whole-document summary from search_documents doesn't cover.
    Returns each match's content and its start_line/end_line in the
    document's extracted text, so you can call read_document with that
    range if you need more surrounding context than the chunk alone
    provides.
    """
    user_id = current_user_id.get()
    if not user_id:
        return []

    try:
        from ..embeddings import embed
        from ..memory.db import search_document_chunks as db_search_document_chunks
    except ImportError:
        from embeddings import embed
        from memory.db import search_document_chunks as db_search_document_chunks

    query_embedding = embed(query)
    matches = run_on_main_loop(
        db_search_document_chunks(user_id, document_id, query, query_embedding, max_results or 5)
    )
    return [
        {
            "chunk_index": m["chunk_index"],
            "content": m["content"],
            "start_line": m["start_line"],
            "end_line": m["end_line"],
            "relevance": round(m["score"], 4),  # fused rank score, higher = more relevant
        }
        for m in matches
    ]


@tool
def read_document(
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None
) -> str:
    """
    Reads the extracted markdown text of a document the current user
    uploaded, given the file_path from an upload note or search_documents.
    Reads from the beginning by default. For documents longer than one
    call can return, use start_line/end_line (1-indexed, inclusive) to page
    through the rest — the response always states the total line count and,
    if there's more, exactly what start_line to use next.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."

    # Defense in depth: even if the model hallucinates or is steered toward
    # a path outside its own user, never read outside that user's directory.
    try:
        resolved = Path(file_path).resolve()
        allowed_root = (UPLOADS_DIR / str(user_id)).resolve()
        resolved.relative_to(allowed_root)
    except (ValueError, OSError):
        return "Access denied: that file is not one of your uploaded documents."

    if not resolved.exists():
        return "That document no longer exists."

    lines = resolved.read_text().splitlines()
    total_lines = len(lines)

    start = max(1, start_line or 1)
    end = min(total_lines, end_line or total_lines)
    if start > total_lines:
        return (
            f"start_line ({start}) is past the end of the document "
            f"({total_lines} lines total)."
        )
    if end < start:
        return (
            f"Invalid range: start_line ({start}) is after end_line ({end})."
        )

    # Cap how much a single call can return, regardless of what was requested.
    end = min(end, start + _MAX_LINES_PER_CALL - 1)

    snippet = "\n".join(lines[start - 1:end])
    if len(snippet) > _MAX_CHARS_PER_CALL:
        snippet = (
            snippet[:_MAX_CHARS_PER_CALL]
            + "\n...(cut short — an unusually long line)"
        )

    note = f"\n\n[lines {start}-{end} of {total_lines} total]"
    if end < total_lines:
        note += f" Call again with start_line={end + 1} to continue reading."
    return snippet + note
