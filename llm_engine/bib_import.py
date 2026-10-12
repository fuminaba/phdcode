"""`phdcode import FILE.bib`: a reference list (a Zotero BibTeX export) into
the library, as abstract-only papers, with their PDFs in the queue.

Each entry with an ID becomes an abstract-only paper. The lookup order:
a DOI at OpenAlex (free: a lookup by ID; it also gives the open-access PDF
link), else a PubMed ID at PubMed, else an arXiv ID at arXiv. If the record
has no abstract, the abstract of the entry fills it, else PubMed's (with a
PubMed ID). The keywords of the entry are added to the paper.

Skipped and listed: an entry with no ID (grey literature: reports, web
pages, patents), and an entry whose DOI is already stored.

The PDF goes into the queue (`phdcode ingest` parses it): the Zotero copy
first, if zotero_storage is the folder that holds a copy of Zotero's
storage folder, else the open-access link. PubMed's link is the PMC page,
not a PDF, so it is not queued.
"""
import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from . import bibtex, sources
from .memory import (
    add_paper_keywords,
    find_paper_by_doi,
    get_paper,
    queue_pdf,
    save_record,
)

log = logging.getLogger(__name__)


def _refs(entry: dict[str, str]) -> list[str]:
    refs = []
    if entry.get("doi"):
        refs.append(f"openalex:doi:{entry['doi']}")
    if entry.get("pmid"):
        refs.append(f"pubmed:{entry['pmid']}")
    if entry.get("eprint") and entry.get("archiveprefix", "arxiv").lower() == "arxiv":
        refs.append(f"arxiv:{entry['eprint']}")
    return refs


def _lookup(entry: dict[str, str]) -> Optional[dict[str, Any]]:
    """The first source record that the IDs of the entry find, with the
    abstract filled from the entry or PubMed if the record has none."""
    record = next((r for r in map(sources.lookup, _refs(entry)) if r), None)
    if record is None or record["abstract_text"]:
        return record
    abstract = entry.get("abstract", "")
    if not abstract and entry.get("pmid") and record["src_db"] != "pubmed":
        pubmed = sources.lookup(f"pubmed:{entry['pmid']}")
        abstract = pubmed["abstract_text"] if pubmed else ""
    return {**record, "abstract_text": abstract}


def zotero_pdf(file_field: str, zotero_storage: Optional[Path]) -> Optional[str]:
    """The server path of the first PDF in a Zotero `file` field
    ("/Users/x/Zotero/storage/KEY/name.pdf;..."), if it exists there."""
    if not file_field or zotero_storage is None:
        return None
    for path in file_field.split(";"):
        _, sep, rest = path.strip().partition("/storage/")
        if sep and rest.lower().endswith(".pdf") and (zotero_storage / rest).is_file():
            return str(zotero_storage / rest)
    return None


async def import_bib(path: Path, zotero_storage: Optional[Path] = None) -> list[str]:
    """Imports each entry of the file (see the module docstring). Returns
    one line for each entry, then the counts."""
    report: list[str] = []
    counts = {"saved": 0, "queued": 0, "stored before": 0, "no ID": 0, "not found": 0}
    for entry in bibtex.parse(path.read_text()):
        name = f"{entry['key']} ({entry.get('title', '')[:60]})"
        if not _refs(entry):
            counts["no ID"] += 1
            report.append(f"skipped, no DOI, PubMed or arXiv ID: {name}")
            continue
        if entry.get("doi") and (stored := await find_paper_by_doi(entry["doi"])):
            counts["stored before"] += 1
            report.append(f"stored before as paper {stored}: {name}")
            continue
        record = await asyncio.to_thread(_lookup, entry)
        if record is None:
            counts["not found"] += 1
            report.append(f"not found at the sources: {name}")
            continue
        paper_id = await save_record(record)
        if entry.get("keywords"):
            await add_paper_keywords(paper_id, [k.strip() for k in entry["keywords"].split(",") if k.strip()])
        counts["saved"] += 1
        pdf = zotero_pdf(entry.get("file", ""), zotero_storage) or (
            record["pdf_url"] if record["src_db"] != "pubmed" else None)
        line = f"paper {paper_id} ({record['ref']}): {record['title'][:70]}"
        if pdf and not (await get_paper(paper_id))["parse_mode"]:
            await queue_pdf(paper_id, pdf)
            counts["queued"] += 1
            line += " [PDF queued]"
        if not record["abstract_text"]:
            line += " [no abstract]"
        report.append(line)
    report.append("; ".join(f"{k}: {v}" for k, v in counts.items()))
    return report


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        (storage / "KEY1").mkdir()
        (storage / "KEY1" / "a.pdf").write_bytes(b"%PDF")
        field = "/Users/x/Zotero/storage/KEY0/snap.html;/Users/x/Zotero/storage/KEY1/a.pdf"
        assert zotero_pdf(field, storage) == str(storage / "KEY1" / "a.pdf")
        assert zotero_pdf("/Users/x/Zotero/storage/KEY2/b.pdf", storage) is None
        assert zotero_pdf(field, None) is None
    assert _refs({"doi": "10.1/x", "pmid": "1", "eprint": "1804.03999"}) == [
        "openalex:doi:10.1/x", "pubmed:1", "arxiv:1804.03999"]
    assert _refs({"title": "LEOPARD"}) == []
    print("bib_import: ok")
