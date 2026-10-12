"""
arxiv_search.py

arXiv search with boolean (AND/OR/NOT) keyword support. Returns records in
the standardized cross-database schema shared with pubmed_search.py:

{
    "db_id": str,             # arXiv ID, e.g. "2401.12345v2"
    "src_db": "arxiv",
    "title": str,
    "authors": List[str],
    "published_date": str,    # ISO datetime, original submission
    "updated_date": str,      # ISO datetime, latest revision (arXiv allows re-submission)
    "venue": Optional[str],   # journal_ref if the authors supplied one, else None
    "categories": List[str],  # arXiv subject tags, e.g. ["cs.CV", "q-bio.QM"]
    "hyperlink": str,         # abstract landing page
    "abstract_text": str,
    "doi": Optional[str],
    "pdf_url": str,
    "is_open_access": bool,   # always True for arXiv
    "matched_query": str,     # the search_query string that produced this hit
    "raw": Dict[str, Any],    # extra source-specific fields (comment, primary_category, links)
}
"""

from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import requests

ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"
NAMESPACES = {"atom": ATOM_NS, "arxiv": ARXIV_NS}

ARXIV_API_URL = "https://export.arxiv.org/api/query"

VALID_FIELDS = {"all", "ti", "abs", "au", "cat", "co", "jr", "id"}
VALID_SORT_BY = {"relevance", "lastUpdatedDate", "submittedDate"}
VALID_SORT_ORDER = {"ascending", "descending"}

# Translates the canonical field vocabulary (shared across db_handlers, see
# docs/adr/0002) into arXiv's own VALID_FIELDS keys. Canonical values with
# no precise arXiv equivalent fall back to "all" via canonical_to_arxiv_field
# below - e.g. arXiv has no combined title+abstract tag, unlike PubMed.
CANONICAL_FIELD_MAP = {
    "all": "all",
    "title": "ti",
    "abstract": "abs",
    "author": "au",
    "journal": "jr",
}


def canonical_to_arxiv_field(canonical_field: str) -> str:
    """Translate a canonical field into arXiv's VALID_FIELDS vocabulary.

    Falls back to "all" (unrestricted) for any canonical value arXiv has
    no reasonable equivalent for.
    """
    return CANONICAL_FIELD_MAP.get(canonical_field, "all")


def _quote_if_phrase(term: str) -> str:
    term = term.strip()
    return f'"{term}"' if " " in term else term


def build_arxiv_query(
    all_of: Optional[List[str]] = None,
    any_of: Optional[List[str]] = None,
    none_of: Optional[List[str]] = None,
    field: str = "all",
    mindate: Optional[str] = None,
    maxdate: Optional[str] = None,
) -> str:
    """
    Build an arXiv boolean search_query string.

    - all_of: terms/phrases that must ALL appear (AND'd together).
    - any_of: terms/phrases where AT LEAST ONE must appear (OR'd together,
              then AND'd against the rest of the query as one group).
    - none_of: terms/phrases that must NOT appear (AND NOT'd).
    - field: which arXiv field to search in for every term above
             ("all", "ti", "abs", "au", "cat", ...).
    - mindate/maxdate: optional inclusive date bounds on submission date,
      each formatted "YYYYMMDD" (e.g. "20240101"). Must be provided
      together. Always filters on submittedDate regardless of `field` -
      a recency window is independent of which field the keyword terms
      themselves are scoped to.
    """
    if field not in VALID_FIELDS:
        raise ValueError(f"field must be one of {VALID_FIELDS}, got {field!r}")
    if bool(mindate) != bool(maxdate):
        raise ValueError("mindate and maxdate must be provided together")

    clauses: List[str] = []

    if all_of:
        and_terms = [f"{field}:{_quote_if_phrase(t)}" for t in all_of if t.strip()]
        clauses.extend(f"({t})" for t in and_terms)

    if any_of:
        or_terms = [f"{field}:{_quote_if_phrase(t)}" for t in any_of if t.strip()]
        if or_terms:
            clauses.append("(" + " OR ".join(or_terms) + ")")

    if not clauses:
        raise ValueError("At least one of all_of/any_of must be provided.")

    query = " AND ".join(clauses)

    if none_of:
        not_terms = [f"{field}:{_quote_if_phrase(t)}" for t in none_of if t.strip()]
        if not_terms:
            query += " ANDNOT (" + " OR ".join(not_terms) + ")"

    if mindate and maxdate:
        query += f" AND submittedDate:[{mindate}0000 TO {maxdate}2359]"

    return query


def search_arxiv(
    all_of: Optional[List[str]] = None,
    any_of: Optional[List[str]] = None,
    none_of: Optional[List[str]] = None,
    field: str = "all",
    max_results: int = 10,
    start: int = 0,
    sort_by: str = "relevance",
    sort_order: str = "descending",
    raw_query: Optional[str] = None,
    mindate: Optional[str] = None,
    maxdate: Optional[str] = None,
    timeout: int = 15,
) -> List[Dict[str, Any]]:
    """
    Query the arXiv API and return a list of standardized paper dicts
    (see module docstring for the schema).

    mindate/maxdate (each "YYYYMMDD") only apply when building the query
    from all_of/any_of/none_of - a raw_query bypasses them, same as it
    already bypasses `field`.
    """
    if sort_by not in VALID_SORT_BY:
        raise ValueError(f"sort_by must be one of {VALID_SORT_BY}")
    if sort_order not in VALID_SORT_ORDER:
        raise ValueError(f"sort_order must be one of {VALID_SORT_ORDER}")

    search_query = (
        raw_query or
        build_arxiv_query(all_of, any_of, none_of, field, mindate, maxdate))

    params = {
        "search_query": search_query,
        "start": start,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }

    try:
        response = requests.get(ARXIV_API_URL, params=params, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"arXiv API request failed: {e}") from e

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as e:
        raise RuntimeError(f"Failed to parse arXiv response as XML: {e}") from e

    articles: List[Dict[str, Any]] = []

    for entry in root.findall("atom:entry", NAMESPACES):
        title_el = entry.find("atom:title", NAMESPACES)
        title = " ".join(title_el.text.split()) if title_el is not None and title_el.text else ""

        summary_el = entry.find("atom:summary", NAMESPACES)
        abstract_text = (
            " ".join(summary_el.text.split()) if summary_el is not None and summary_el.text else ""
        )

        published_el = entry.find("atom:published", NAMESPACES)
        published_date = published_el.text if published_el is not None else ""

        updated_el = entry.find("atom:updated", NAMESPACES)
        updated_date = updated_el.text if updated_el is not None else ""

        authors = [
            name_el.text.strip()
            for auth in entry.findall("atom:author", NAMESPACES)
            if (name_el := auth.find("atom:name", NAMESPACES)) is not None and name_el.text
        ]

        id_el = entry.find("atom:id", NAMESPACES)
        abs_url = id_el.text if id_el is not None else ""
        db_id = abs_url.rsplit("/", 1)[-1] if abs_url else ""

        categories = [
            cat.attrib["term"] for cat in entry.findall("atom:category", NAMESPACES) if "term" in cat.attrib
        ]

        primary_cat_el = entry.find("arxiv:primary_category", NAMESPACES)
        primary_category = primary_cat_el.attrib.get("term", "") if primary_cat_el is not None else ""

        journal_ref_el = entry.find("arxiv:journal_ref", NAMESPACES)
        venue = journal_ref_el.text.strip() if journal_ref_el is not None and journal_ref_el.text else None

        doi_el = entry.find("arxiv:doi", NAMESPACES)
        doi = doi_el.text.strip() if doi_el is not None and doi_el.text else None

        comment_el = entry.find("arxiv:comment", NAMESPACES)
        comment = comment_el.text.strip() if comment_el is not None and comment_el.text else None

        pdf_url = ""
        for link in entry.findall("atom:link", NAMESPACES):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
                break

        articles.append(
            {
                "db_id": db_id,
                "src_db": "arxiv",
                "title": title,
                "authors": authors,
                "published_date": published_date,
                "updated_date": updated_date,
                "venue": venue,
                "categories": categories,
                "hyperlink": abs_url,
                "abstract_text": abstract_text,
                "doi": doi,
                "pdf_url": pdf_url,
                "is_open_access": True,
                "matched_query": search_query,
                "raw": {
                    "primary_category": primary_category,
                    "comment": comment,
                },
            }
        )

    return articles


if __name__ == "__main__":
    results = search_arxiv(
        all_of=["graph neural networks"],
        any_of=["oncology", "pathology"],
        none_of=["survey"],
        max_results=5,
    )

    print(f"Total papers found: {len(results)}\n")
    for paper in results:
        print(f"[{paper['src_db']}] {paper['db_id']}  {paper['title']}")
        print(f"  Authors: {', '.join(paper['authors'])}")
        print(f"  Categories: {paper['categories']}  |  Updated: {paper['updated_date'][:10]}")
        print(f"  {paper['hyperlink']}\n")