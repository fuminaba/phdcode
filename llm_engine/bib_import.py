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
import re
from pathlib import Path
from typing import Any, Optional

from . import sources
from .memory import (
    add_paper_keywords,
    find_paper_by_doi,
    get_paper,
    queue_pdf,
    save_record,
)

log = logging.getLogger(__name__)

# A small BibTeX reader (a Zotero or Better BibTeX export): entries with
# braced, quoted or bare field values. No @string or @comment support;
# Zotero does not write them.
_ENTRY = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,")
_FIELD = re.compile(r"\s*([\w-]+)\s*=\s*")
_LATEX = [(r"\&", "&"), (r"\%", "%"), (r"\_", "_"), (r"\$", "$"), ("~", " "), ("--", "–")]


def _value(text: str, i: int) -> tuple[str, int]:
    """The value that starts at text[i] and the index after it."""
    if text[i] in "{\"":
        close = "}" if text[i] == "{" else "\""
        depth, j = 0, i
        while j < len(text):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}" and close == "}":
                depth -= 1
                if depth == 0:
                    return text[i + 1:j], j + 1
            elif c == "\"" and close == "\"" and j > i and depth == 0:
                return text[i + 1:j], j + 1
            elif c == "}":
                depth -= 1
            j += 1
        raise ValueError(f"An unclosed value at character {i}.")
    m = re.match(r"[^,}\s]+", text[i:])
    return m.group(0), i + m.end()


def clean(value: str) -> str:
    """Removes the case-protection braces and the common LaTeX escapes."""
    value = value.replace("{", "").replace("}", "")
    for latex, plain in _LATEX:
        value = value.replace(latex, plain)
    return " ".join(value.split())


def parse_bibtex(text: str) -> list[dict[str, str]]:
    """Each entry as a dict: "type", "key", and each field (lower-case name),
    cleaned."""
    entries = []
    for m in _ENTRY.finditer(text):
        entry = {"type": m.group(1).lower(), "key": m.group(2)}
        i = m.end()
        while True:
            field = _FIELD.match(text, i)
            if not field:
                break
            value, i = _value(text, field.end())
            entry[field.group(1).lower()] = clean(value)
            i = re.match(r"\s*,?", text[i:]).end() + i
        entries.append(entry)
    return entries



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
    for entry in parse_bibtex(path.read_text()):
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
    sample = """
@article{agosti2024,
  title = {Histopathological Evaluation: {{Current}} Issues},
  author = {Agosti, Vittorio and Munari, Enrico},
  year = 2024,
  month = nov,
  doi = {10.4103/aja202440},
  abstract = {A {nested {brace}} and 50\\% of \\& cases},
  pmid = {39254403}
}
@misc{leopard, title = "The {LEOPARD} Challenge", note = {x}}
"""
    a, b = parse_bibtex(sample)
    assert a["type"] == "article" and a["key"] == "agosti2024"
    assert a["title"] == "Histopathological Evaluation: Current Issues", a["title"]
    assert a["year"] == "2024" and a["month"] == "nov" and a["pmid"] == "39254403"
    assert a["abstract"] == "A nested brace and 50% of & cases", a["abstract"]
    assert b == {"type": "misc", "key": "leopard", "title": "The LEOPARD Challenge", "note": "x"}, b
    print("bib_import: ok")
