"""The phdcode interface: a web page of what phdcode holds and runs.

    uvicorn llm_engine.dashboard:app --host 100.110.224.47 --port 8620

phdcode-dashboard.service runs it on the Tailscale address only, so it has
no login. It reads Postgres, the paper folders, the digests, the systemd
units and the GPU state. It changes two things: the importance of a paper
(ADR-0019) and the daily question with its answer (ADR-0020); the daily
question calls the summary model.
"""
import asyncio
import fcntl
import math
import os
import re
import subprocess
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from . import gpu, questions
from .documents import PAPERS_DIR
from .embeddings import EMBEDDING_MODEL
from .generation import SUMMARY_MODEL
from .memory import close_pool, get_paper, get_pool, pending_papers
from .memory.document_index import CHUNK_OVERLAP, CHUNK_SIZE, MAP_CHUNK_SIZE
from .sources import OPENALEX_API_KEY, PUBMED_API_KEY
from .sweep import DIGEST_DIR, LOOKBACK_DAYS, MAX_HITS
from .triage import TRIAGE_MODEL

SOURCE_URL = {
    "arxiv": "https://arxiv.org/abs/{}",
    "pubmed": "https://pubmed.ncbi.nlm.nih.gov/{}/",
    "openalex": "https://openalex.org/{}",
}
# The same page as the documentation site (mkdocs.yml), so that the two
# cannot differ.
HOW_PAGE = Path(__file__).resolve().parents[1] / "docs" / "how-it-works.md"
# Weakest first, as in the CHECK of papers.importance (schema.sql).
IMPORTANCE = ("interesting", "recommended", "crucial")
_DIGEST_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.md")
# No raw HTML: the paper text comes from a PDF.
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(title="phdcode", lifespan=_lifespan, docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _when(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else (value or "")


templates.env.filters["when"] = _when
# [P12] in a model answer links to paper 12.
templates.env.filters["cite_links"] = lambda text: Markup(
    re.sub(r"\[P(\d+)\]", r'<a href="/papers/\1">[P\1]</a>', str(escape(text or ""))))
templates.env.globals["importance_levels"] = IMPORTANCE
templates.env.globals["zip"] = zip
templates.env.globals["source_url"] = lambda source, source_id: (
    SOURCE_URL[source].format(source_id) if source in SOURCE_URL else None)


def _page(request: Request, template: str, **context: Any):
    return templates.TemplateResponse(request, template, context)


def _paper_state(p: dict[str, Any]) -> str:
    if p["parse_mode"]:
        return "full text" if p["summarized"] else "full text, summaries pending"
    if p["pdf_error"]:
        return "PDF failed"
    if p["pdf_source"]:
        return "PDF queued"
    # PubMed's pdf_url is the PMC page, which ingest_pdf refuses.
    if p["pdf_url"] and p["source"] != "pubmed":
        return "abstract only, PDF link known"
    return "abstract only, no PDF link"


def _units() -> dict[str, dict[str, str]]:
    props = ["LoadState", "ActiveState", "ActiveEnterTimestamp", "NextElapseUSecRealtime",
             "LastTriggerUSec", "Result", "ExecMainExitTimestamp"]
    units = {}
    for name in (*gpu.LLM_UNITS, "phdcode-sweep.timer", "phdcode-sweep.service"):
        out = subprocess.run(["systemctl", "--user", "show", name, *(f"-p{p}" for p in props)],
                             capture_output=True, text=True, check=False).stdout
        units[name] = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return units


def _lock_holder() -> str:
    """Who holds /tmp/gpu.lock now: a shared lock fails while a tenant holds
    it. A short job that tries at the same moment waits 0.5 s and tries again."""
    try:
        fd = os.open(gpu.LOCK_PATH, os.O_RDONLY)
    except FileNotFoundError:
        return ""
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return ""
    except OSError:
        if gpu.llm_active():
            return "llama-server"
        with open(gpu.LOCK_PATH) as f:
            return f.read().strip() or "unknown"
    finally:
        os.close(fd)  # also releases the shared lock


def _system() -> dict[str, Any]:
    return {
        "units": _units(),
        "cleared": os.path.exists(gpu.GPU_CLEARED),
        "lock_holder": _lock_holder(),
        "free_mib": gpu.free_vram_mib(),
    }


def _config() -> list[tuple[str, Any]]:
    return [
        ("Summary model (SUMMARY_MODEL)", SUMMARY_MODEL or "not set: summaries stay pending"),
        ("Triage model (TRIAGE_MODEL): does the student with this library want the paper?",
         TRIAGE_MODEL or f"{SUMMARY_MODEL} (the summary model)"),
        ("Embedding model (CPU, Ollama)", EMBEDDING_MODEL),
        ("Chunk size / overlap (characters)", f"{CHUNK_SIZE} / {CHUNK_OVERLAP}"),
        ("Summary map chunk (characters)", MAP_CHUNK_SIZE),
        ("Sweep window / hits per search", f"{LOOKBACK_DAYS} days / {MAX_HITS}"),
        ("Paper folders", PAPERS_DIR),
        ("Digest folder", DIGEST_DIR),
        ("OpenAlex key", "set" if OPENALEX_API_KEY else "not set: $0.10 a day of searches"),
        ("PubMed key", "set" if PUBMED_API_KEY else "not set"),
    ]


async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch(query, *args)]


_PAPERS_SQL = (
    "SELECT p.id, p.source, p.source_id, p.title, p.authors, p.published_date, p.venue, p.pdf_url, "
    "       p.parse_mode, p.importance, p.created_at, p.pdf_source, p.pdf_error, "
    "       (p.methods_summary IS NOT NULL AND p.results_summary IS NOT NULL) AS summarized, "
    "       (SELECT count(*) FROM chunks c WHERE c.paper_id = p.id) AS chunks "
    "FROM papers p ORDER BY p.id DESC"
)


@app.get("/")
async def overview(request: Request):
    counts, papers, pending, system = await asyncio.gather(
        _fetch(
            "SELECT (SELECT count(*) FROM papers) AS papers, "
            "       (SELECT count(*) FROM papers WHERE parse_mode IS NOT NULL) AS full_text, "
            "       (SELECT count(*) FROM chunks) AS chunks, "
            "       (SELECT count(*) FROM keywords) AS keywords, "
            "       (SELECT count(*) FROM saved_searches WHERE cancelled_at IS NULL) AS searches, "
            "       (SELECT count(*) FROM triaged_hits) AS triaged"
        ),
        _fetch(_PAPERS_SQL),
        pending_papers(),
        asyncio.to_thread(_system),
    )
    states: dict[str, int] = {}
    for p in papers:
        states[_paper_state(p)] = states.get(_paper_state(p), 0) + 1
    digests = sorted(DIGEST_DIR.glob("*.md"), reverse=True)
    return _page(request, "overview.html", counts=counts[0], states=states, pending=pending,
                 system=system, config=_config(), last_digest=digests[0].name if digests else None)


@app.get("/papers")
async def papers(request: Request):
    rows = await _fetch(_PAPERS_SQL)
    for p in rows:
        p["state"] = _paper_state(p)
    return _page(request, "papers.html", papers=rows)


def _paper_dir(paper_id: int) -> Path:
    return (PAPERS_DIR / str(paper_id)).resolve()


@app.get("/papers/{paper_id}")
async def paper(request: Request, paper_id: int):
    stored = await get_paper(paper_id)
    if stored is None:
        raise HTTPException(404, f"No paper has id {paper_id}.")
    queue, chunks, hits = await asyncio.gather(
        _fetch("SELECT pdf_source, pdf_error FROM papers WHERE id = $1", paper_id),
        _fetch("SELECT c.chunk_index, c.page_start, c.page_end, c.section, c.content, "
               "       (SELECT array_agg(l.kind || ' ' || l.label || ' → chunk ' || l.to_index ORDER BY l.to_index) "
               "        FROM chunk_links l WHERE l.paper_id = c.paper_id AND l.from_index = c.chunk_index) AS links "
               "FROM chunks c WHERE c.paper_id = $1 ORDER BY c.chunk_index", paper_id),
        _fetch("SELECT t.saved_search_id, t.probability, t.triaged_at, s.threshold FROM triaged_hits t "
               "JOIN saved_searches s ON s.id = t.saved_search_id "
               "WHERE s.source = $1 AND t.source_id = $2", stored["source"], stored["source_id"]),
    )
    folder = _paper_dir(paper_id)
    files = sorted(f.relative_to(folder) for f in folder.rglob("*") if f.is_file()) if folder.is_dir() else []
    return _page(request, "paper.html", p=stored, queue=queue[0], chunks=chunks, hits=hits,
                 files=[(str(f), (folder / f).stat().st_size) for f in files],
                 images=[str(f) for f in files if f.parts[0] == "images"],
                 text_file=next((str(f) for f in files if f.name.endswith(".pdf.md")), None))


@app.post("/papers/{paper_id}/importance")
async def set_importance(paper_id: int, level: str = ""):
    if level not in IMPORTANCE + ("",):
        raise HTTPException(422, f"The importance must be one of {IMPORTANCE} or empty.")
    rows = await _fetch("UPDATE papers SET importance = $2, updated_at = now() WHERE id = $1 RETURNING id",
                        paper_id, level or None)
    if not rows:
        raise HTTPException(404, f"No paper has id {paper_id}.")
    return {"id": paper_id, "importance": level or None}


@app.get("/papers/{paper_id}/files/{path:path}")
async def paper_file(paper_id: int, path: str):
    folder = _paper_dir(paper_id)
    target = (folder / path).resolve()
    if not target.is_relative_to(folder) or not target.is_file():
        raise HTTPException(404, "No such file.")
    return FileResponse(target)


@app.get("/papers/{paper_id}/text")
async def paper_text(request: Request, paper_id: int):
    folder = _paper_dir(paper_id)
    text_file = next(folder.glob("*.pdf.md"), None) if folder.is_dir() else None
    if text_file is None:
        raise HTTPException(404, "This paper has no parsed text.")
    stored = await get_paper(paper_id)
    # marker links each figure by its bare name; the files are in images/.
    html = re.sub(r'src="(_page_[^"/]+)"', r'src="files/images/\1"', _markdown.render(text_file.read_text()))
    return _page(request, "markdown.html", title=(stored or {}).get("title") or text_file.name,
                 back=f"/papers/{paper_id}", html=html)


@app.get("/interests")
async def interests(request: Request):
    searches, hits = await asyncio.gather(
        _fetch("SELECT s.*, count(t.source_id) AS hits, "
               "       count(t.source_id) FILTER (WHERE t.probability >= s.threshold) AS relevant "
               "FROM saved_searches s LEFT JOIN triaged_hits t ON t.saved_search_id = s.id "
               "GROUP BY s.id ORDER BY s.cancelled_at IS NOT NULL, s.id"),
        _fetch("SELECT t.saved_search_id, t.source_id, t.probability, t.triaged_at, p.id AS paper_id, p.title "
               "FROM triaged_hits t JOIN saved_searches s ON s.id = t.saved_search_id "
               "LEFT JOIN papers p ON p.source = s.source AND p.source_id = t.source_id "
               "ORDER BY t.probability DESC"),
    )
    by_search: dict[int, list[dict[str, Any]]] = {}
    for h in hits:
        by_search.setdefault(h["saved_search_id"], []).append(h)
    return _page(request, "interests.html", searches=searches, hits=by_search)


@app.get("/digests")
async def digests(request: Request, day: str = ""):
    names = [p.name for p in sorted(DIGEST_DIR.glob("*.md"), reverse=True)]
    name = day or (names[0] if names else "")
    if name and (not _DIGEST_NAME.fullmatch(name) or name not in names):
        raise HTTPException(404, f"No digest {name}.")
    html = _markdown.render((DIGEST_DIR / name).read_text()) if name else ""
    return _page(request, "digests.html", names=names, name=name, html=html)


def _radar(values: list[float], width: int = 700, height: int = 300, rings: int = 4) -> dict[str, Any]:
    """The geometry of a radar chart in an SVG of width x height: for each
    axis its end, its value point and its label anchor; one polygon for each
    ring. The scale is 0 to the largest value."""
    cx, cy, r, n = width / 2, height / 2, height / 2 - 30, len(values)
    top = max(values) or 1
    unit = [(math.sin(2 * math.pi * i / n), -math.cos(2 * math.pi * i / n)) for i in range(n)]
    return {
        "width": width, "height": height, "cx": cx, "cy": cy, "top": top,
        "rings": [" ".join(f"{cx + x * r * k / rings:.1f},{cy + y * r * k / rings:.1f}" for x, y in unit)
                  for k in range(1, rings + 1)],
        "axes": [{"x": cx + x * r, "y": cy + y * r, "px": cx + x * r * v / top, "py": cy + y * r * v / top,
                  "lx": cx + x * (r + 12), "ly": cy + y * (r + 12) + 4,
                  "anchor": "middle" if abs(x) < 0.3 else "start" if x > 0 else "end"}
                 for (x, y), v in zip(unit, values)],
    }


@app.get("/questions")
async def questions_page(request: Request):
    rows = await _fetch("SELECT * FROM questions ORDER BY asked_on DESC, id DESC")
    ids = {i for q in rows for i in (q["paper_ids"] or []) + (q["cited"] or [])}
    titles = {r["id"]: r["title"] for r in await _fetch("SELECT id, title FROM papers WHERE id = ANY($1)", list(ids))}
    current = rows[0] if rows and rows[0]["asked_on"] == date.today() else None
    stats = questions.stats(rows)
    return _page(request, "questions.html", current=current, history=[q for q in rows if q is not current],
                 titles=titles, stats=stats,
                 radars={name: _radar([n for n, _ in axes.values()]) for name, axes in stats["axes"].items()},
                 axis_sets=questions.AXIS_SETS, question_types=questions.QUESTION_TYPES,
                 models=questions.models(), question_model=questions.QUESTION_MODEL,
                 grade_model=questions.GRADE_MODEL)


@app.post("/questions/new")
async def new_question(request: Request):
    """{"model", "axis_set", "axis", "question_type"}, each optional: a new
    question by that model on that axis and type; a missing axis or type is
    the weakest of axis_set (ADR-0021)."""
    body = await request.json()
    choices = {"model": questions.models(), "axis_set": questions.AXIS_SETS,
               "axis": questions.AXIS_SETS.get(body.get("axis_set") or next(iter(questions.AXIS_SETS)), {}),
               "question_type": questions.QUESTION_TYPES}
    for field, allowed in choices.items():
        if body.get(field) is not None and body[field] not in allowed:
            raise HTTPException(422, f"Unknown {field} {body[field]!r}.")
    try:
        return {"id": (await questions.ask(**{field: body.get(field) for field in choices}))["id"]}
    except Exception as e:  # the model server is stopped, the library is empty, or the reply is not valid
        raise HTTPException(503, f"No question: {e}")


@app.post("/questions/{question_id}/answer")
async def answer_question(question_id: int, request: Request):
    """{"answer": "...", "model": "..."} saves and grades the answer; {"answer": null}
    grades the saved one again. model is the grader (default GRADE_MODEL)."""
    body = await request.json()
    answer = body.get("answer")
    if answer is not None and not answer.strip():
        raise HTTPException(422, "The answer is empty.")
    if body.get("model") is not None and body["model"] not in questions.models():
        raise HTTPException(422, f"Unknown model {body['model']!r}.")
    try:
        return await questions.grade(question_id, answer, body.get("model"))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # the model server is stopped, or the reply is not valid
        raise HTTPException(503, f"The answer is saved but not graded: {e}")


@app.get("/how")
async def how(request: Request):
    return _page(request, "how.html", html=_markdown.render(HOW_PAGE.read_text()))
