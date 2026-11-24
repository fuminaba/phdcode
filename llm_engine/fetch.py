"""phdcode fetch (ADR-0036): finds an open-access PDF for the abstract-only
papers, newest first, and puts its link into the PDF queue (pdf_source);
phdcode ingest then parses it.

The links of a paper, in this order; the first that returns a PDF wins:

1. the stored pdf_url (arXiv; the best open-access location of OpenAlex),
   but not PubMed's link, which is the PMC article page;
2. the PDF of the PMC open-access data set on S3, by the PMC id of the
   PubMed link, else by the DOI through the NCBI ID converter;
3. the PDF locations of OpenAlex by DOI;
4. citation_pdf_url of the DOI landing page (the PDF of the publisher).

A paper with no PDF gets pdf_error "no open-access PDF found on <date>"; the
fetch tries it again after RETRY_DAYS (a PMC deposit can come months after
the publication). A failure of a lookup service (OpenAlex, the ID converter,
S3) leaves the paper as it is, for the next fetch. A failed link of a
publisher is a miss of that link only.
"""
import asyncio
import json
import logging
import re
import urllib.request
from collections.abc import Iterator
from datetime import date
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

import requests

from .memory import db
from .sources import OPENALEX_API_KEY
from .sources.openalex_search import BASE as OPENALEX
from .sources.openalex_search import _get as openalex_get

log = logging.getLogger(__name__)

MISS = "no open-access PDF found on"
RETRY_DAYS = 30
FAILED_IN_A_ROW = 5
_HEADERS = {"User-Agent": "phdcode"}
_PMCID = re.compile(r"PMC\d+")
_S3 = "https://pmc-oa-opendata.s3.amazonaws.com"
_IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
_META = re.compile(r"<meta[^>]*citation_pdf_url[^>]*>", re.I)
_CONTENT = re.compile(r"""content=["']([^"']+)""", re.I)


class LookupFailed(Exception):
    """A lookup service did not answer: try the paper again at the next fetch."""


def _service(url: str, params: Optional[dict[str, Any]] = None) -> bytes:
    """The body of a lookup service. urllib, not requests: the NCBI ID
    converter answers 403 to each requests client (also with the headers of
    curl) and 200 to urllib and curl (2026-11-16)."""
    try:
        request = urllib.request.Request(f"{url}?{urlencode(params)}" if params else url, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except (OSError, ValueError) as e:  # URLError and HTTPError are OSErrors
        raise LookupFailed(f"{urlsplit(url).netloc}: {e}") from e


def is_pdf(url: str) -> bool:
    """True if url returns a PDF. Reads only the first bytes: phdcode ingest
    downloads the file."""
    try:
        with requests.get(url, headers=_HEADERS, timeout=60, stream=True) as response:
            return response.ok and next(response.iter_content(4), b"") == b"%PDF"
    except requests.RequestException:
        return False


def _pmcid(paper: dict[str, Any]) -> Optional[str]:
    found = _PMCID.search(paper["pdf_url"] or "")
    if found:
        return found.group()
    if not paper["doi"]:
        return None
    records = json.loads(_service(_IDCONV, {"ids": paper["doi"], "format": "json", "tool": "phdcode"}))["records"]
    return records[0].get("pmcid") if records else None


def _pmc_pdf(pmcid: str) -> Optional[str]:
    """The PDF of the newest version of pmcid in the open-access data set, or None."""
    listing = _service(_S3, {"list-type": "2", "prefix": f"metadata/{pmcid}."}).decode()
    keys = re.findall(rf"<Key>(metadata/{pmcid}\.(\d+)\.json)</Key>", listing)
    if not keys:
        return None
    key, _ = max(keys, key=lambda k: int(k[1]))
    pdf = json.loads(_service(f"{_S3}/{key}")).get("pdf_url")  # s3://pmc-oa-opendata/<path>?md5=...
    return pdf and f"{_S3}/{pdf.removeprefix('s3://pmc-oa-opendata/').split('?')[0]}"


def _openalex_pdfs(doi: str) -> list[str]:
    params = {"select": "locations", **({"api_key": OPENALEX_API_KEY} if OPENALEX_API_KEY else {})}
    try:
        work = openalex_get(f"{OPENALEX}/doi:{doi}", params, 30)
    except (requests.RequestException, ValueError) as e:  # ValueError: a 429 or a spent budget
        raise LookupFailed(f"OpenAlex: {e}") from e
    return [loc["pdf_url"] for loc in (work or {}).get("locations") or [] if loc.get("pdf_url")]


def _publisher_pdf(doi: str) -> Optional[str]:
    try:
        response = requests.get(f"https://doi.org/{doi}", headers=_HEADERS, timeout=30)
    except requests.RequestException:
        return None
    tag = _META.search(response.text) if response.ok else None
    content = tag and _CONTENT.search(tag.group())
    return content.group(1) if content else None


def _links(paper: dict[str, Any]) -> Iterator[str]:
    """The links of paper in the order of the module docstring. A generator,
    so that a hit stops the lookups after it."""
    if paper["pdf_url"] and not _PMCID.search(paper["pdf_url"]):
        yield paper["pdf_url"]
    pmcid = _pmcid(paper)
    if pmcid and (pdf := _pmc_pdf(pmcid)):
        yield pdf
    if paper["doi"]:
        yield from _openalex_pdfs(paper["doi"])
        if pdf := _publisher_pdf(paper["doi"]):
            yield pdf


def find_pdf(paper: dict[str, Any]) -> Optional[str]:
    """The first link of paper that returns a PDF, or None. Raises
    LookupFailed when a lookup service does not answer."""
    tried = set()
    for url in _links(paper):
        if url not in tried:
            tried.add(url)
            # An arXiv link is not checked: arXiv asks for 3 s between requests, and
            # the VM checks each download (parse.sh).
            if urlsplit(url).netloc.endswith("arxiv.org") or is_pdf(url):
                return url
    return None


async def fetch_pdfs(limit: Optional[int] = None, dry_run: bool = False) -> tuple[list[str], int]:
    """`phdcode fetch`: at most limit papers with a result, a link or a miss
    (None: all). A paper whose lookup failed does not count, so that it does
    not take the place of another paper each day; after FAILED_IN_A_ROW such
    papers the fetch stops (a service is down). Returns one line for each
    paper, after a line with the name of the database, and the number of
    failed lookups."""
    report = [f"database {urlsplit(db.DATABASE_URL or '').path.lstrip('/')}" + (", dry run" if dry_run else "")]
    done = failed = in_a_row = 0
    for paper in await db.papers_to_fetch(None, MISS, RETRY_DAYS):
        if done == limit or in_a_row == FAILED_IN_A_ROW:
            break
        try:
            url = await asyncio.to_thread(find_pdf, paper)
        except LookupFailed as e:
            failed, in_a_row = failed + 1, in_a_row + 1
            report.append(f"paper {paper['id']}: not now, the next fetch tries again ({e})")
            continue
        done, in_a_row = done + 1, 0
        if url and not dry_run:
            await db.queue_pdf(paper["id"], url)
        elif not url and not dry_run:
            await db.set_pdf_error(paper["id"], f"{MISS} {date.today()}")
        report.append(f"paper {paper['id']}: " + (f"queued {url}" if url else "no open-access PDF"))
    return report, failed
