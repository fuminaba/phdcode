"""
arxiv_search.py

arXiv search. The query is in arXiv's own search_query syntax (fields,
AND, OR, ANDNOT). Returns records in
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

VALID_SORT_BY = {"relevance", "lastUpdatedDate", "submittedDate"}
VALID_SORT_ORDER = {"ascending", "descending"}

def search_arxiv(
    raw_query: str,
    max_results: int = 10,
    start: int = 0,
    sort_by: str = "relevance",
    sort_order: str = "descending",
    timeout: int = 15,
) -> List[Dict[str, Any]]:
    """
    Query the arXiv API and return a list of standardized paper dicts
    (see module docstring for the schema).
    """
    if sort_by not in VALID_SORT_BY:
        raise ValueError(f"sort_by must be one of {VALID_SORT_BY}")
    if sort_order not in VALID_SORT_ORDER:
        raise ValueError(f"sort_order must be one of {VALID_SORT_ORDER}")

    params = {
        "search_query": raw_query,
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
                "matched_query": raw_query,
                "raw": {
                    "primary_category": primary_category,
                    "comment": comment,
                },
            }
        )

    return articles

