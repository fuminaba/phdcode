"""The literature sources: one search and one lookup for each source.

A paper reference is "<source>:<source_id>", for example "arxiv:2401.12345",
"pubmed:33649564" or "openalex:W7221365635". An arXiv id has no version
suffix, so that a new version of a stored paper updates the same row.

The MCP tools (search_sources, get_paper, save_paper) and the sweep use these
functions. Each call blocks on HTTP: call it with asyncio.to_thread from async
code.
"""
import os
import re
from datetime import date
from typing import Any, Optional

from dotenv import load_dotenv

from .arxiv_search import search_arxiv
from .openalex_search import get_openalex, search_openalex
from .pubmed_search import search_pubmed

load_dotenv()

PUBMED_EMAIL = os.getenv("PUBMED_EMAIL")
PUBMED_API_KEY = os.getenv("PUBMED_API_KEY")
OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY")

SOURCES = ("arxiv", "pubmed", "openalex")

_ARXIV_VERSION_RE = re.compile(r"v\d+$")


# Each search takes since: None = by relevance; a date = the papers that the
# source added (arXiv, PubMed) or published (OpenAlex) since that day, newest
# first.

def _arxiv(raw_query: str, max_results: int, since: Optional[date]) -> list[dict[str, Any]]:
    if since is None:
        return search_arxiv(raw_query=raw_query, max_results=max_results)
    window = f"submittedDate:[{since:%Y%m%d}0000 TO {date.today():%Y%m%d}2359]"
    return search_arxiv(raw_query=f"({raw_query}) AND {window}", max_results=max_results,
                        sort_by="submittedDate")


def _pubmed(raw_query: str, max_results: int, since: Optional[date]) -> list[dict[str, Any]]:
    window = {} if since is None else {
        "datetype": "edat", "mindate": f"{since:%Y/%m/%d}", "maxdate": f"{date.today():%Y/%m/%d}",
        "sort_by": "pub_date",
    }
    return search_pubmed(
        raw_query=raw_query, max_results=max_results, include_abstract=True,
        email=PUBMED_EMAIL, api_key=PUBMED_API_KEY, **window,
    )


def _openalex(query: str, max_results: int, since: Optional[date]) -> list[dict[str, Any]]:
    return search_openalex(query, max_results, since and since.isoformat(), OPENALEX_API_KEY)


_SEARCH = {"arxiv": _arxiv, "pubmed": _pubmed, "openalex": _openalex}
_LOOKUP = {
    "arxiv": lambda source_id: _arxiv(f"id:{source_id}", 1, None),
    "pubmed": lambda source_id: _pubmed(f"{source_id}[uid]", 1, None),
    "openalex": lambda source_id: [r for r in [get_openalex(source_id, OPENALEX_API_KEY)] if r],
}


def check_source(source: str) -> str:
    source = source.lower().strip()
    if source not in SOURCES:
        raise ValueError(f"Unknown source {source!r}; expected one of {SOURCES}.")
    return source


def _with_ref(record: dict[str, Any]) -> dict[str, Any]:
    source_id = record["db_id"]
    if record["src_db"] == "arxiv":
        source_id = _ARXIV_VERSION_RE.sub("", source_id)
    return {**record, "source_id": source_id, "ref": f"{record['src_db']}:{source_id}"}


def parse_ref(ref: str) -> tuple[str, str]:
    """"arxiv:2401.12345" -> ("arxiv", "2401.12345")."""
    source, sep, source_id = ref.partition(":")
    if not sep or not source_id.strip():
        raise ValueError(f"Not a paper reference: {ref!r}; expected '<source>:<id>', e.g. 'arxiv:2401.12345'.")
    return check_source(source), source_id.strip()


def search(source: str, query: str, max_results: int = 10, since: Optional[date] = None) -> list[dict[str, Any]]:
    """Full records for a query in the native syntax of the source. Each
    record has the keys of arxiv_search.py, plus source_id and ref."""
    return [_with_ref(r) for r in _SEARCH[check_source(source)](query, max_results, since)]


def lookup(ref: str) -> Optional[dict[str, Any]]:
    """The full record of one paper, from the source (never a cache), or None."""
    source, source_id = parse_ref(ref)
    records = _LOOKUP[source](source_id)
    return _with_ref(records[0]) if records else None
