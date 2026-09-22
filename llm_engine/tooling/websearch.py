import os, json, sys
import logging
import concurrent.futures
from typing import Literal, Optional
from dotenv import load_dotenv
from tavily import TavilyClient

try:
    from .registry import tool
    from .cache import cache_tool_result
    from ..prompts import websearch_result_digest_prompt
except ImportError:
    from registry import tool
    from cache import cache_tool_result
    from prompts import websearch_result_digest_prompt

load_dotenv()

log = logging.getLogger(__name__)

# Raw Tavily content below this length is already compact enough to just
# pass through -- not worth a whole extra LLM round trip to shrink a
# snippet that's already small.
_DIGEST_THRESHOLD_CHARS = 600
_DEFAULT_MODEL_NAME = "qwen3:14b"
# Boosts results from this country -- Tavily expects the full country
# name, lowercase (confirmed against their docs: "canada", not "CA" or
# "Canada"), and only takes effect when topic="general", which is
# already Tavily's own default (this project never sets topic
# explicitly). Empty string disables it -- passed as None to Tavily,
# same as just not setting the param at all.
WEB_SEARCH_COUNTRY = os.getenv("WEB_SEARCH_COUNTRY", "canada")

# Tavily's own `timeout` kwarg bounds requests' per-read gaps, not total
# call duration -- a slow, trickling response can still run far longer
# than that (confirmed directly: a real call once took ~629s despite the
# SDK's own 60s/120s-capped timeout). A persistent executor (never shut
# down) lets us abandon a stuck call outright on our own hard deadline --
# its thread finishes in the background and its result is just discarded,
# rather than the whole turn blocking on it.
_SEARCH_TIMEOUT_SECONDS = 30
_search_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="tavily-search"
)


def _digest_result(query: str, content: Optional[str]) -> Optional[str]:
    """Condenses one result's raw page content via the same "chat" Celery
    queue every other model call in this project goes through -- a direct
    (non-Celery) call here would let this run concurrently with whatever
    else is using the GPU, exactly the race the whole Celery migration
    exists to close. Pass-through, unchanged, for content already short
    enough not to be worth the round trip."""
    if not content or len(content) <= _DIGEST_THRESHOLD_CHARS:
        return content
    try:
        from ..tasks.chat_tasks import call_model
    except ImportError:
        from tasks.chat_tasks import call_model

    messages = [{"role": "user", "content": websearch_result_digest_prompt(query, content)}]
    try:
        response = call_model.apply_async(
            args=[_DEFAULT_MODEL_NAME, messages, []], queue="chat",
        ).get(timeout=600)
        return response["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning(f"web_search result digest failed, falling back to raw content: {e}")
        return content


@tool
def web_search(
        query: str,
        max_results: Optional[int] = 5,
        search_depth: Literal["basic", "advanced"] = "basic",
) -> list[dict]:
    """
    Search the web for a given query and return the results. Each result's
    content is condensed to a short digest if it's long (see
    _DIGEST_THRESHOLD_CHARS) -- an overview, not the full page; there's no
    fetch-the-rest tool today, so search again with a more specific query
    if a digest doesn't have what you need.
    """
    # Fetch API key from environment variables
    api_key = os.environ.get("TAVILY_TOKEN")
    if not api_key:
        raise ValueError("TAVILY_TOKEN environment variable not set.")

    # Initialize the Tavily Client
    tavily_client = TavilyClient(api_key=api_key)

    print(f"\nSearching for: '{query}'...\n")

    search_results = []
    try:
        future = _search_executor.submit(
            tavily_client.search,
            query=query, search_depth=search_depth, max_results=max_results or 5,
            country=WEB_SEARCH_COUNTRY or None,
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
        try:
            response = future.result(timeout=_SEARCH_TIMEOUT_SECONDS + 10)
        except concurrent.futures.TimeoutError:
            log.warning(
                f"Tavily search exceeded its {_SEARCH_TIMEOUT_SECONDS}s hard "
                f"deadline for query={query!r}; abandoning and returning no "
                "results rather than blocking the turn."
            )
            return []
        results = response.get("results", [])

        if not results:
            print("No results found.")
            return []

        # Sequential, not concurrent, by the same reasoning as everywhere
        # else in this project: the Celery worker processes one task at a
        # time regardless, so there's no real parallelism to gain, and it
        # keeps this simple.
        for result in results:
            search_results.append({
                "title": result.get("title"),
                "url": result.get("url"),
                "content": _digest_result(query, result.get("content")),
            })

        # One line is the entire integration cost for a cacheable tool --
        # see tooling/cache.py. recall_tool_results can surface this to a
        # future call instead of hitting Tavily again.
        cache_tool_result("web_search", query, search_results)

    except Exception as e:
        print(f"An error occurred: {e}")

    return search_results

if __name__ == "__main__":
    # Allow passing the query via terminal arguments or interactive prompt
    if len(sys.argv) > 1:
        user_query = " ".join(sys.argv[1:])
    else:
        user_query = input("Enter your search query: ")
    
    if user_query.strip():
        res = web_search(user_query)
        res = json.dumps(res, indent=2)
        print(res)
