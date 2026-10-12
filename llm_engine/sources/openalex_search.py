"""
openalex_search.py

OpenAlex works search and lookup. Returns records in the standardized
cross-database schema of arxiv_search.py:

{
    "db_id": str,             # OpenAlex work ID, e.g. "W7221365635"
    "src_db": "openalex",
    "title": str,
    "authors": List[str],
    "published_date": str,    # "YYYY-MM-DD"
    "venue": Optional[str],   # the source of the primary location
    "categories": List[str],  # OpenAlex keywords
    "hyperlink": str,         # the DOI link, else the landing page, else the work
    "abstract_text": str,     # rebuilt from the inverted index; "" if none
    "doi": Optional[str],     # without "https://doi.org/"
    "pdf_url": Optional[str], # the best open-access PDF, if any
    "is_open_access": bool,
    "matched_query": str,
    "raw": Dict[str, Any],    # type
}

The query is the value of the OpenAlex `filter` parameter, for example
'title_and_abstract.search:"nuclei segmentation",type:article'.

Pricing (checked 2026-10-11): with no API key, a search costs $0.001 from a
daily budget of $0.10; a lookup by ID costs nothing. from_created_date needs
a paid plan, so a date window uses from_publication_date.
"""
from typing import Any, Dict, List, Optional

import requests

BASE = "https://api.openalex.org/works"
_SELECT = (
    "id,doi,title,authorships,publication_date,primary_location,"
    "best_oa_location,open_access,abstract_inverted_index,keywords,type"
)


def _abstract(inverted: Optional[Dict[str, List[int]]]) -> str:
    """{"word": [positions]} -> the text."""
    if not inverted:
        return ""
    words = sorted((pos, word) for word, positions in inverted.items() for pos in positions)
    return " ".join(word for _, word in words)


def _record(work: Dict[str, Any], query: str) -> Dict[str, Any]:
    primary = work.get("primary_location") or {}
    best_oa = work.get("best_oa_location") or {}
    doi = (work.get("doi") or "").removeprefix("https://doi.org/") or None
    return {
        "db_id": work["id"].rsplit("/", 1)[-1],
        "src_db": "openalex",
        "title": work.get("title") or "",
        "authors": [a["author"]["display_name"] for a in work.get("authorships") or []],
        "published_date": work.get("publication_date") or "",
        "venue": (primary.get("source") or {}).get("display_name"),
        "categories": [k["display_name"] for k in work.get("keywords") or []],
        "hyperlink": work.get("doi") or primary.get("landing_page_url") or work["id"],
        "abstract_text": _abstract(work.get("abstract_inverted_index")),
        "doi": doi,
        "pdf_url": best_oa.get("pdf_url"),
        "is_open_access": bool((work.get("open_access") or {}).get("is_oa")),
        "matched_query": query,
        "raw": {"type": work.get("type")},
    }


def _get(url: str, params: Dict[str, Any], timeout: int) -> Optional[Dict[str, Any]]:
    """The JSON reply; None if OpenAlex has no such work."""
    response = requests.get(url, params=params, timeout=timeout)
    if response.status_code == 404:
        return None
    if response.status_code in (400, 403, 429):
        # OpenAlex explains a bad filter or a spent budget in the body.
        try:
            message = response.json().get("message") or response.text
        except ValueError:
            message = response.text
        raise ValueError(f"OpenAlex: {message[:300]}")
    response.raise_for_status()
    return response.json()


def search_openalex(
    query: str,
    max_results: int = 10,
    from_date: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 30,
) -> List[Dict[str, Any]]:
    """query is an OpenAlex filter. from_date ("YYYY-MM-DD") keeps works
    published since then, newest first; else the order is by relevance."""
    filters = query + (f",from_publication_date:{from_date}" if from_date else "")
    params = {"filter": filters, "per-page": max_results, "select": _SELECT}
    if from_date:
        params["sort"] = "publication_date:desc"
    if api_key:
        params["api_key"] = api_key
    return [_record(w, query) for w in _get(BASE, params, timeout)["results"]]


def get_openalex(work_id: str, api_key: Optional[str] = None, timeout: int = 30) -> Optional[Dict[str, Any]]:
    """One work by its ID ("W7221365635"), or None."""
    params = {"select": _SELECT, **({"api_key": api_key} if api_key else {})}
    work = _get(f"{BASE}/{work_id}", params, timeout)
    return _record(work, work_id) if work else None
