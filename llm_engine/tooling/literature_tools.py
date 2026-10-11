import os
from typing import Any, Optional

from dotenv import load_dotenv

try:
    from .registry import tool
except ImportError:
    from registry import tool

try:
    from ..context import run_on_main_loop
except ImportError:
    from context import run_on_main_loop

try:
    from ._tooling_deps.arxiv_search import canonical_to_arxiv_field, search_arxiv as _search_arxiv
    from ._tooling_deps.pubmed_search import canonical_to_pubmed_field, search_pubmed as _search_pubmed
except ImportError:
    from tooling._tooling_deps.arxiv_search import canonical_to_arxiv_field, search_arxiv as _search_arxiv
    from tooling._tooling_deps.pubmed_search import canonical_to_pubmed_field, search_pubmed as _search_pubmed

load_dotenv()

PUBMED_EMAIL = os.getenv("PUBMED_EMAIL")
PUBMED_API_KEY = os.getenv("PUBMED_API_KEY")

# search_arxiv/search_pubmed return only this much per hit -- measured
# directly, not guessed: a single default (then 10-result) arXiv call
# serialized to ~3900 tokens, ~95% of this deployment's 4096-token context
# budget, by itself, before the prompt/system prompt/tool schemas. A real
# agent turn using the tool was immediately truncated to an empty response
# as a result. Full detail (abstract_text, hyperlink, doi, categories,
# pdf_url, is_open_access) is one read_paper call away, per result the
# model actually wants to look at -- incremental disclosure instead of
# cramming every hit's full record into context up front.
_OVERVIEW_FIELDS = ["db_id", "src_db", "title", "authors", "published_date", "venue"]

# read_paper's full-detail view. Drops "raw" (untouched source dict) and
# matched_query (meaningless for a single by-id lookup) from the
# underlying modules' own schema.
_DETAIL_FIELDS = [
    "db_id", "src_db", "title", "authors", "published_date", "updated_date",
    "venue", "categories", "hyperlink", "abstract_text", "doi", "pdf_url",
    "is_open_access",
]


def _project(records: list[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    return [{k: r.get(k) for k in fields} for r in records]


@tool
def search_arxiv(
        all_of: Optional[list[str]] = None,
        any_of: Optional[list[str]] = None,
        none_of: Optional[list[str]] = None,
        field: str = "all",
        max_results: Optional[int] = 10,
) -> list[dict[str, Any]] | str:
    """
    Searches arXiv (preprints, mostly CS/physics/math/quant-bio) for papers
    matching a boolean keyword query. Returns a short overview per hit --
    db_id (arXiv id), src_db, title, authors, published_date, venue -- not
    the full record. Call read_paper(src_db, db_id) on whichever hits look
    promising to get the rest (abstract_text, hyperlink, doi, categories,
    pdf_url, is_open_access) before deciding whether to save_finding.

    Args:
        all_of: terms/phrases that must ALL appear (AND'd together).
        any_of: terms/phrases where at least one must appear (OR'd
            together, then AND'd against the rest).
        none_of: terms/phrases that must NOT appear.
        field: which field to search in, for every term above — "all"
            (default), "title", "abstract", "author", or "journal".
        max_results: how many results to return (default 10).

    At least one of all_of/any_of is required.
    """
    try:
        records = _search_arxiv(
            all_of=all_of, any_of=any_of, none_of=none_of,
            field=canonical_to_arxiv_field(field), max_results=max_results or 10,
        )
    except (ValueError, RuntimeError) as e:
        return f"Search failed: {e}"
    return _project(records, _OVERVIEW_FIELDS)


@tool
def search_pubmed(
        all_of: Optional[list[str]] = None,
        any_of: Optional[list[str]] = None,
        none_of: Optional[list[str]] = None,
        field: str = "all",
        max_results: Optional[int] = 10,
) -> list[dict[str, Any]] | str:
    """
    Searches PubMed (biomedical/life-sciences literature) for papers
    matching a boolean keyword query. Returns a short overview per hit --
    db_id (PMID), src_db, title, authors, published_date, venue -- not the
    full record. Call read_paper(src_db, db_id) on whichever hits look
    promising to get the rest (abstract_text, hyperlink, doi, categories,
    pdf_url, is_open_access) before deciding whether to save_finding.

    Args:
        all_of: terms/phrases that must ALL appear (AND'd together).
        any_of: terms/phrases where at least one must appear (OR'd
            together, then AND'd against the rest).
        none_of: terms/phrases that must NOT appear.
        field: which field to search in, for every term above — "all"
            (default), "title", "abstract", "author", or "journal".
        max_results: how many results to return (default 10).

    At least one of all_of/any_of is required.
    """
    try:
        records = _search_pubmed(
            all_of=all_of, any_of=any_of, none_of=none_of,
            field=canonical_to_pubmed_field(field), max_results=max_results or 10,
            email=PUBMED_EMAIL, api_key=PUBMED_API_KEY, include_abstract=True,
        )
    except (ValueError, RuntimeError) as e:
        return f"Search failed: {e}"
    return _project(records, _OVERVIEW_FIELDS)


@tool
def read_paper(src_db: str, db_id: str) -> dict[str, Any] | str:
    """
    Looks up one paper's full record by id -- db_id/src_db from a
    search_arxiv/search_pubmed result. Returns title, authors,
    published_date, updated_date, venue, categories, hyperlink,
    abstract_text, doi, pdf_url, is_open_access. Re-queries the source API
    for just this one paper rather than caching search results, so it's
    always current and never adds to context until you actually ask for a
    specific paper's detail.
    """
    src_db = src_db.lower().strip()
    try:
        if src_db == "arxiv":
            records = _search_arxiv(raw_query=f"id:{db_id}", max_results=1)
        elif src_db == "pubmed":
            records = _search_pubmed(
                raw_query=f"{db_id}[uid]", max_results=1, include_abstract=True,
                email=PUBMED_EMAIL, api_key=PUBMED_API_KEY,
            )
        else:
            return f"Unknown src_db {src_db!r} -- expected 'arxiv' or 'pubmed'."
    except (ValueError, RuntimeError) as e:
        return f"Lookup failed: {e}"

    if not records:
        return f"No {src_db} paper found for db_id={db_id!r}."
    return _project(records, _DETAIL_FIELDS)[0]


@tool
def save_finding(
        db_id: str,
        src_db: str,
        title: str,
        authors: list[str],
        abstract_text: str,
        hyperlink: str,
        doi: Optional[str] = None,
        categories: Optional[list[str]] = None,
) -> str:
    """
    Saves a paper as abstract-only -- title, authors, and abstract
    (embedded as the concept facet), findable later with search_papers.
    Does NOT download or parse the PDF, so the paper has no chunks. Use
    this for papers worth remembering, not every search hit.

    Pass the field values from a read_paper call on this paper (db_id,
    src_db, title, authors, abstract_text, hyperlink, doi, categories) --
    search_arxiv/search_pubmed's own results don't include abstract_text,
    so look the paper up with read_paper first if you haven't already.
    Saving the same paper (same db_id + src_db) again updates the existing
    paper instead of creating a duplicate.
    """
    if not abstract_text:
        return "Can't save a finding with no abstract_text -- call read_paper first to get it."

    try:
        from ..embeddings import embed
        from ..memory.db import save_paper
    except ImportError:
        from embeddings import embed
        from memory.db import save_paper

    [embedding] = embed([abstract_text])
    paper_id = run_on_main_loop(
        save_paper(
            src_db.lower().strip(), db_id, title, authors, abstract_text, embedding,
            doi=doi, url=hyperlink, keywords=categories or [],
        )
    )
    return f"Saved as paper_id={paper_id}. Findable later via search_papers."
