from typing import Any, Optional

try:
    from .registry import tool
    from .cache import lookup_cached_results
except ImportError:
    from registry import tool
    from cache import lookup_cached_results


@tool
def recall_tool_results(tool_name: str, query: str, max_results: Optional[int] = 5) -> list[dict[str, Any]]:
    """
    Checks for a past call to tool_name with a similar query, across any
    conversation -- call this BEFORE an expensive external lookup to avoid
    redoing it. Currently cacheable: "web_search" (search_arxiv/
    search_pubmed aren't wired yet -- expect no results if you pass those).

    An empty result means nothing cached is topically close enough to be
    useful (low-relevance matches are already filtered out server-side) --
    just call the real tool as normal in that case, don't retry this
    first. Each match that IS returned includes age_hours, how long ago
    it was cached. Judge freshness yourself: a stable fact can be reused
    regardless of age, but something time-sensitive (weather, prices,
    current events, scores) usually isn't worth reusing past a few hours
    old.
    """
    return lookup_cached_results(tool_name, query, max_results or 5)
