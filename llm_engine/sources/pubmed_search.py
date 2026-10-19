"""
pubmed_search.py

PubMed (NCBI E-utilities) search with date filtering and rate limiting. The
query is in PubMed's own syntax (search tags, AND, OR, NOT). Returns records in the
standardized cross-database schema shared with arxiv_search.py:

{
    "db_id": str,             # PMID
    "src_db": "pubmed",
    "title": str,
    "authors": List[str],
    "published_date": str,
    "updated_date": str,      # PubMed has no true revision date; falls back to
                               # epubdate, then published_date, since articles
                               # aren't versioned the way arXiv preprints are
    "venue": Optional[str],   # journal name
    "categories": List[str],  # publication types, e.g. ["Journal Article", "Review"]
                               # (MeSH terms aren't available via esummary)
    "hyperlink": str,         # PubMed landing page
    "abstract_text": Optional[str],  # only populated if include_abstract=True
    "doi": Optional[str],
    "pdf_url": Optional[str], # PMC full-text link if open access, else None
    "is_open_access": bool,   # True if a PMC ID exists
    "matched_query": str,
    "raw": Dict[str, Any],    # untouched esummary record + articleids
}
"""

import time
from typing import Any, Dict, List, Optional

import requests

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

# Real accepted values for ESearch's `sort` param on the pubmed db.
SORT_MAP = {
    "relevance": "relevance",
    "pub_date": "pub date",
    "first_author": "first author",
    "last_author": "last author",
    "title": "title",
    "journal": "journal",
}

VALID_DATETYPES = {"pdat", "edat", "mdat"}


def search_pubmed(
    raw_query: str,
    max_results: int = 10,
    sort_by: str = "relevance",
    email: Optional[str] = None,
    api_key: Optional[str] = None,
    datetype: str = "pdat",
    mindate: Optional[str] = None,
    maxdate: Optional[str] = None,
    include_abstract: bool = False,
    timeout: int = 15,
) -> List[Dict[str, Any]]:
    """
    Query PubMed and return a list of standardized paper dicts
    (see module docstring for the schema).
    """
    if sort_by not in SORT_MAP:
        raise ValueError(f"sort_by must be one of {list(SORT_MAP)}, got {sort_by!r}")
    if datetype not in VALID_DATETYPES:
        raise ValueError(f"datetype must be one of {VALID_DATETYPES}")
    if bool(mindate) != bool(maxdate):
        raise ValueError("mindate and maxdate must be provided together")

    common = {"db": "pubmed", "retmode": "json"}
    if email:
        common["email"] = email
    if api_key:
        common["api_key"] = api_key

    search_params = {
        **common,
        "term": raw_query,
        "retmax": max_results,
        "sort": SORT_MAP[sort_by],
    }
    if mindate and maxdate:
        search_params.update(datetype=datetype, mindate=mindate, maxdate=maxdate)

    try:
        search_resp = requests.get(f"{EUTILS_BASE}esearch.fcgi", params=search_params, timeout=timeout)
        search_resp.raise_for_status()
        pmids = search_resp.json().get("esearchresult", {}).get("idlist", [])
    except requests.RequestException as e:
        raise RuntimeError(f"PubMed esearch request failed: {e}") from e

    if not pmids:
        return []

    time.sleep(0.34 if not api_key else 0.11)

    try:
        summary_resp = requests.get(
            f"{EUTILS_BASE}esummary.fcgi",
            params={**common, "id": ",".join(pmids)},
            timeout=timeout,
        )
        summary_resp.raise_for_status()
        summary_data = summary_resp.json().get("result", {})
    except requests.RequestException as e:
        raise RuntimeError(f"PubMed esummary request failed: {e}") from e

    abstracts: Dict[str, str] = {}
    if include_abstract:
        time.sleep(0.34 if not api_key else 0.11)
        abstracts = _fetch_abstracts(pmids, common, timeout)

    articles = []
    for pmid in pmids:
        str_pmid = str(pmid)
        if str_pmid not in summary_data:
            continue

        paper = summary_data[str_pmid]
        authors = [a["name"] for a in paper.get("authors", []) if "name" in a]

        doi = None
        pmcid = None
        for article_id in paper.get("articleids", []):
            if article_id.get("idtype") == "doi":
                doi = article_id.get("value")
            elif article_id.get("idtype") == "pmc":
                pmcid = article_id.get("value")

        is_open_access = pmcid is not None
        pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/" if pmcid else None

        # sortpubdate ("2026/10/05 00:00") is ISO-like; pubdate ("2026 Oct 5")
        # is free text.
        published_date = paper.get("sortpubdate", "")[:10].replace("/", "-") or paper.get("pubdate", "")
        updated_date = paper.get("epubdate") or published_date

        articles.append(
            {
                "db_id": str_pmid,
                "src_db": "pubmed",
                "title": paper.get("title", ""),
                "authors": authors,
                "published_date": published_date,
                "updated_date": updated_date,
                "venue": paper.get("fulljournalname") or paper.get("source"),
                "categories": paper.get("pubtype", []),
                "hyperlink": f"https://pubmed.ncbi.nlm.nih.gov/{str_pmid}/",
                "abstract_text": abstracts.get(str_pmid),
                "doi": doi,
                "pdf_url": pdf_url,
                "is_open_access": is_open_access,
                "matched_query": raw_query,
                "raw": paper,
            }
        )

    return articles


def _fetch_abstracts(pmids: List[str], common_params: Dict[str, str], timeout: int) -> Dict[str, str]:
    """Fetch abstract text via efetch (esummary doesn't include it)."""
    import xml.etree.ElementTree as ET

    try:
        resp = requests.get(
            f"{EUTILS_BASE}efetch.fcgi",
            params={**common_params, "id": ",".join(pmids), "rettype": "abstract", "retmode": "xml"},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return {}

    abstracts: Dict[str, str] = {}
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return {}

    for article in root.findall(".//PubmedArticle"):
        pmid_el = article.find(".//PMID")
        if pmid_el is None or not pmid_el.text:
            continue
        pieces = [el.text for el in article.findall(".//AbstractText") if el.text]
        if pieces:
            abstracts[pmid_el.text] = " ".join(pieces)

    return abstracts

