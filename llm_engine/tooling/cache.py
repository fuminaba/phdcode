"""Shared caching helpers for any cacheable tool -- see schema.sql's
comment on tool_result_cache for the design. A cacheable tool calls
cache_tool_result(...) once, itself, after computing its result; the
generic recall_tool_results tool (cache_tools.py) calls
lookup_cached_results(...) for any tool_name. Adding a new cacheable tool
later is exactly one call to cache_tool_result -- no schema change, no new
recall tool, nothing else to wire.

Both functions are sync (tools are called synchronously from within
_run_tool_loop, itself running in a worker thread) and bridge to the
actual async DB/embedding work via run_on_main_loop, matching every other
tool in this package that touches the database.
"""
import logging
from typing import Any

try:
    from ..context import current_user_id, run_on_main_loop
except ImportError:
    from context import current_user_id, run_on_main_loop

log = logging.getLogger(__name__)

# Below this cosine similarity, a match isn't actually related to the
# query -- calibrated against real measurements, not guessed: a genuine
# match scored ~0.89, a deliberately unrelated query against the same
# cache entry scored ~0.40. Confirmed the cost of skipping this filter
# via a real production trace: nearest_neighbor search has no concept of
# "nothing good enough", so a "weather" query against a cache holding
# only a gold-price entry still returned that gold-price entry (0.3674)
# as if it were usable -- the model then has to notice it's irrelevant
# itself, which it did successfully once and, on an identical retry,
# didn't (returned an empty digest instead). Filtering here means the
# model sees a clean "nothing cached" instead of a misleading low-quality
# match it has to reason its way out of.
_MIN_RELEVANCE = 0.5


def cache_tool_result(tool_name: str, query: str, result: Any) -> None:
    """Embeds query and persists (tool_name, query, result) to
    tool_result_cache, tagged with the current user_id for provenance
    (not a lookup filter -- see schema.sql). Best-effort: a caching
    failure is logged and swallowed, never raised -- it must not affect
    the tool call it's riding along with. The whole point is to keep a
    cacheable tool's own call site to this one line -- no imports, no
    user_id plumbing, at the caller."""
    try:
        try:
            from ..embeddings import embed
            from ..memory.db import save_tool_result_cache
        except ImportError:
            from embeddings import embed
            from memory.db import save_tool_result_cache

        query_embedding = embed(query)
        user_id = current_user_id.get() or "unknown"
        run_on_main_loop(
            save_tool_result_cache(tool_name, user_id, query, query_embedding, result)
        )
    except Exception as e:
        log.warning(f"Couldn't cache result for tool={tool_name!r}: {e}")


def lookup_cached_results(tool_name: str, query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Embeds query and returns the closest past cached calls to this same
    tool_name, each with its own age in hours -- see
    cache_tools.recall_tool_results for how this is exposed to the model."""
    try:
        from ..embeddings import embed
        from ..memory.db import search_tool_result_cache
    except ImportError:
        from embeddings import embed
        from memory.db import search_tool_result_cache

    from datetime import datetime, timezone

    query_embedding = embed(query)
    matches = run_on_main_loop(search_tool_result_cache(tool_name, query, query_embedding, max_results or 5))
    now = datetime.now(timezone.utc)
    return [
        {
            "cached_query": m["query"],
            "result": m["result"],
            "cached_at": m["created_at"].isoformat(),
            "age_hours": round((now - m["created_at"]).total_seconds() / 3600, 1),
            "relevance": round(m["relevance"], 4),
        }
        for m in matches
        if m["relevance"] >= _MIN_RELEVANCE
    ]
