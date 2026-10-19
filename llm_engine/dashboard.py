"""The phdcode interface: a web page of what phdcode holds and runs.

    uvicorn llm_engine.dashboard:app --host 100.110.224.47 --port 8620

phdcode-dashboard.service runs it on the Tailscale address only, so it has
no login. It reads Postgres, the paper folders, the digests, the systemd
units and the GPU state. It changes the importance of a paper (ADR-0019),
its notes, its read mark and the bin (ADR-0026),
the daily question with its answer (ADR-0020), the paper of the day
(ADR-0024) and the writing coach (ADR-0025); the last three call a model.
It also keeps the user's label of each judged hit (ADR-0027).
"""
import asyncio
import fcntl
import math
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from . import daily_paper, gpu, questions, writing
from .documents import PAPERS_DIR
from .embeddings import EMBEDDING_MODEL
from .generation import SUMMARY_MODEL
from .memory import close_pool, get_paper, get_pool, pending_papers
from .memory.document_index import CHUNK_OVERLAP, CHUNK_SIZE, MAP_CHUNK_SIZE
from .sources import OPENALEX_API_KEY, PUBMED_API_KEY
from .sweep import DIGEST_DIR, LOOKBACK_DAYS, MAX_HITS, parse_digest
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
# The choices of the bin page; a paper stays in the bin this many days (ADR-0026).
BIN_DAYS = (7, 14, 30, 60, 90, 180, 365)
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
_CITE = re.compile(r"\[P(\d+)\]")
# A tutor reply: Markdown without raw HTML, with [P12] linked to paper 12.
templates.env.filters["md_cites"] = lambda text: Markup(_CITE.sub(r'<a href="/papers/\1">[P\1]</a>',
                                                                  _markdown.render(text or "")))
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
    "       p.parse_mode, p.importance, p.read_at, p.created_at, p.pdf_source, p.pdf_error, "
    "       (p.methods_summary IS NOT NULL AND p.results_summary IS NOT NULL) AS summarized, "
    "       (SELECT count(*) FROM chunks c WHERE c.paper_id = p.id) AS chunks "
    "FROM papers p WHERE p.deleted_at IS NULL ORDER BY p.id DESC"
)


@app.get("/")
async def overview(request: Request):
    counts, papers, pending, system = await asyncio.gather(
        _fetch(
            "SELECT (SELECT count(*) FROM papers WHERE deleted_at IS NULL) AS papers, "
            "       (SELECT count(*) FROM papers WHERE parse_mode IS NOT NULL AND deleted_at IS NULL) AS full_text, "
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
    days = await _bin_days()
    await _empty_bin(days)
    rows = await _fetch(_PAPERS_SQL)
    for p in rows:
        p["state"] = _paper_state(p)
    return _page(request, "papers.html", papers=rows, days=days)


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


async def _update_paper(paper_id: int, assignment: str, value: Any) -> None:
    """Sets one column of a paper ($2 in the assignment), or answers 404."""
    if not await _fetch(f"UPDATE papers SET {assignment}, updated_at = now() WHERE id = $1 RETURNING id",
                        paper_id, value):
        raise HTTPException(404, f"No paper has id {paper_id}.")


@app.post("/papers/{paper_id}/notes")
async def set_notes(paper_id: int, notes: str = Body(embed=True)):
    await _update_paper(paper_id, "notes = $2", notes.strip() or None)


@app.post("/papers/{paper_id}/read")
async def set_read(paper_id: int, read: bool):
    await _update_paper(paper_id, "read_at = CASE WHEN $2 THEN coalesce(read_at, now()) END", read)


@app.post("/papers/{paper_id}/bin")
async def move_to_bin(paper_id: int, restore: bool = False):
    """Moves a paper to the bin, or restores it from the bin."""
    await _update_paper(paper_id, "deleted_at = CASE WHEN $2 THEN NULL ELSE coalesce(deleted_at, now()) END", restore)


async def _bin_days() -> int:
    rows = await _fetch("SELECT value FROM settings WHERE key = 'bin_days'")
    return int(rows[0]["value"]) if rows else 30


async def _empty_bin(days: int) -> None:
    """Deletes for good each paper that is in the bin longer than the bin
    days, with its folder. The tables of the paper cascade (schema.sql)."""
    # ponytail: runs when the papers or the bin page opens; a timer if a paper must go on time.
    for row in await _fetch("DELETE FROM papers WHERE deleted_at < now() - make_interval(days => $1) RETURNING id", days):
        shutil.rmtree(_paper_dir(row["id"]), ignore_errors=True)


@app.get("/bin")
async def bin_page(request: Request):
    days = await _bin_days()
    await _empty_bin(days)
    rows = await _fetch("SELECT id, title, authors, deleted_at, deleted_at + make_interval(days => $1) AS purge_at "
                        "FROM papers WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC", days)
    return _page(request, "bin.html", papers=rows, days=days, choices=sorted(set(BIN_DAYS) | {days}))


@app.post("/bin/days")
async def set_bin_days(days: int):
    if not 1 <= days <= 3650:
        raise HTTPException(422, "The bin days must be from 1 to 3650.")
    await _fetch("INSERT INTO settings (key, value) VALUES ('bin_days', $1) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", str(days))


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


def _search_name(interest: str) -> str:
    """A short name of a saved search: its interest statement up to the
    first example or parenthesis."""
    name = re.split(r" \(|, for example|, such as|, especially|, including", interest or "", maxsplit=1)[0]
    return name.rstrip(".") or "(no interest)"


@app.get("/digests")
async def digests(request: Request, day: str = ""):
    """Each digest file as a card of its day; day opens that card (default: the newest)."""
    files = sorted(DIGEST_DIR.glob("*.md"), reverse=True)
    if day and (not _DIGEST_NAME.fullmatch(day) or day not in {f.name for f in files}):
        raise HTTPException(404, f"No digest {day}.")
    days = []
    for f in files:
        sweeps = parse_digest(f.read_text())
        searches = [s for sweep in sweeps for s in sweep["searches"]]
        for s in searches:
            s["name"] = _search_name(s.get("interest", ""))
        days.append({"name": f.name, "date": date.fromisoformat(f.stem), "sweeps": sweeps,
                     "relevant": sum(len(s["hits"]) for s in searches),
                     "hits": sum((s["counts"] or {}).get("hits", 0) for s in searches),
                     "searches": len(searches), "notes": sum(len(s["notes"]) for s in searches)})
    refs = list({h["ref"] for d in days for sweep in d["sweeps"] for s in sweep["searches"] for h in s["hits"]})
    papers = {f"{r['source']}:{r['source_id']}": r for r in await _fetch(
        "SELECT id, source, source_id, importance FROM papers WHERE source || ':' || source_id = ANY($1)", refs)}
    return _page(request, "digests.html", days=days, papers=papers, open_day=day or (files[0].name if files else ""),
                 source_names={"arxiv": "arXiv", "pubmed": "PubMed", "openalex": "OpenAlex"})


def _auc(scored: list[tuple[float, bool]]) -> float | None:
    """The AUC of the probabilities against the labels (a tie counts half);
    None without both kinds of label."""
    pos = [p for p, y in scored if y]
    neg = [p for p, y in scored if not y]
    if not pos or not neg:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


@app.get("/labels")
async def labels(request: Request):
    """Every judged hit with its probability and the user's label (ADR-0027),
    newest first; the unlabelled hits in their own table."""
    hits = await _fetch(
        "SELECT t.saved_search_id, t.source_id, t.probability, t.triaged_at, t.label, t.labelled_at, "
        "       coalesce(t.title, p.title) AS title, coalesce(t.abstract, p.abstract) AS abstract, t.url, t.head_probability, "
        "       s.source, s.threshold, s.interest, p.id AS paper_id "
        "FROM triaged_hits t JOIN saved_searches s ON s.id = t.saved_search_id "
        "LEFT JOIN papers p ON p.source = s.source AND p.source_id = t.source_id "
        "ORDER BY t.triaged_at DESC, t.probability DESC")
    for h in hits:
        h["name"] = _search_name(h["interest"])
        h["guess"] = h["probability"] >= h["threshold"]
    labelled = [h for h in hits if h["label"] is not None]
    return _page(request, "labels.html", todo=[h for h in hits if h["label"] is None], labelled=labelled,
                 relevant=sum(h["label"] for h in labelled),
                 agree=sum(h["guess"] == h["label"] for h in labelled),
                 auc=_auc([(h["probability"], h["label"]) for h in labelled]),
                 head_auc=_auc([(h["head_probability"], h["label"]) for h in labelled if h["head_probability"] is not None]),
                 head_n=sum(h["head_probability"] is not None for h in labelled))


@app.post("/labels/{saved_search_id}/{source_id:path}")
async def set_label(saved_search_id: int, source_id: str, label: bool | None = None):
    """Sets the user's label of a judged hit; no label clears it."""
    if not await _fetch("UPDATE triaged_hits SET label = $3::boolean, "
                        "labelled_at = CASE WHEN $3::boolean IS NULL THEN NULL ELSE now() END "
                        "WHERE saved_search_id = $1 AND source_id = $2 RETURNING source_id",
                        saved_search_id, source_id, label):
        raise HTTPException(404, f"Saved search {saved_search_id} has no judged hit {source_id}.")
    return {"saved_search_id": saved_search_id, "source_id": source_id, "label": label}


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
async def questions_page(request: Request, q: int = 0):
    """q: the question to show and answer again (ADR-0022); else today's latest."""
    rows = await _fetch("SELECT * FROM questions ORDER BY asked_on DESC, id DESC")
    attempts = await _fetch("SELECT * FROM attempts ORDER BY graded_at")
    ids = {i for r in rows for i in (r["paper_ids"] or []) + (r["cited"] or [])}
    titles = {r["id"]: r["title"] for r in await _fetch("SELECT id, title FROM papers WHERE id = ANY($1)", list(ids))}
    current = next((r for r in rows if r["id"] == q), None) if q else (
        rows[0] if rows and rows[0]["asked_on"] == date.today() else None)
    if q and current is None:
        raise HTTPException(404, f"No question has id {q}.")
    by_question: dict[int, list[dict[str, Any]]] = {}
    for a in attempts:
        by_question.setdefault(a["question_id"], []).append(a)
    stats = questions.stats(rows, attempts)
    chat = await _fetch("SELECT role, content, model FROM question_chat WHERE question_id = $1 ORDER BY id",
                        current["id"]) if current else []
    return _page(request, "questions.html", current=current, history=[r for r in rows if r is not current],
                 attempts=by_question, titles=titles, stats=stats, today=date.today(), chat=chat,
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


@app.post("/questions/{question_id}/chat")
async def chat_question(question_id: int, request: Request):
    """{"message": "...", "model": "..."}: the tutor's reply (ADR-0023), as
    HTML, so that the page adds it without a reload (an answer draft stays)."""
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(422, "The message is empty.")
    if body.get("model") is not None and body["model"] not in questions.models():
        raise HTTPException(422, f"Unknown model {body['model']!r}.")
    try:
        reply = await questions.chat(question_id, message, body.get("model"))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # the model server is stopped
        raise HTTPException(503, f"No reply: {e}")
    return {"html": str(templates.env.filters["md_cites"](reply))}


async def _model_body(request: Request, *fields: str) -> dict[str, Any]:
    """The JSON body, with each model field checked against questions.models()."""
    body = await request.json()
    for field in fields:
        if body.get(field) is not None and body[field] not in questions.models():
            raise HTTPException(422, f"Unknown model {body[field]!r}.")
    return body


@app.get("/paper-of-the-day")
async def paper_of_the_day(request: Request):
    brief = await daily_paper.today()
    paper = await get_paper(brief["paper_id"]) if brief else None
    earlier = await _fetch("SELECT d.*, p.title FROM daily_papers d JOIN papers p ON p.id = d.paper_id "
                           "WHERE d.day < $1 ORDER BY d.day DESC", date.today())
    return _page(request, "paper_of_the_day.html", brief=brief, p=paper, earlier=earlier,
                 models=questions.models(), default_model=questions.QUESTION_MODEL)


@app.post("/paper-of-the-day/new")
async def new_paper_of_the_day(request: Request):
    """{"model"}: a new brief for today, of the next paper (ADR-0024)."""
    body = await _model_body(request, "model")
    try:
        return {"paper_id": (await daily_paper.write(body.get("model")))["paper_id"]}
    except Exception as e:  # the model server is stopped, the library is empty, or the reply is not valid
        raise HTTPException(503, f"No paper of the day: {e}")


@app.get("/writing")
async def writing_page(request: Request, p: int = 0):
    """p: the writing task to show and revise; else today's latest (ADR-0025)."""
    prompts = await _fetch("SELECT w.*, p.title FROM writing_prompts w LEFT JOIN papers p ON p.id = w.paper_id "
                           "ORDER BY w.asked_on DESC, w.id DESC")
    attempts = await _fetch("SELECT a.*, w.kind FROM writing_attempts a JOIN writing_prompts w ON w.id = a.prompt_id "
                            "ORDER BY a.id")
    current = next((w for w in prompts if w["id"] == p), None) if p else (
        prompts[0] if prompts and prompts[0]["asked_on"] == date.today() else None)
    if p and current is None:
        raise HTTPException(404, f"No writing task has id {p}.")
    by_prompt: dict[int, list[dict[str, Any]]] = {}
    for a in attempts:
        a["overall"] = writing.overall(a["scores"])
        by_prompt.setdefault(a["prompt_id"], []).append(a)
    return _page(request, "writing.html", current=current, history=[w for w in prompts if w is not current],
                 attempts=by_prompt, stats=writing.stats(attempts), kinds=writing.KINDS, criteria=writing.CRITERIA,
                 recent=writing.RECENT, today=date.today(), models=questions.models(),
                 prompt_model=questions.QUESTION_MODEL, review_model=questions.GRADE_MODEL)


@app.post("/writing/new")
async def new_writing(request: Request):
    """{"kind", "model"}, each optional: a new writing task; a missing kind is
    the least practised."""
    body = await _model_body(request, "model")
    if body.get("kind") is not None and body["kind"] not in writing.KINDS:
        raise HTTPException(422, f"Unknown kind {body['kind']!r}.")
    try:
        return {"id": (await writing.ask(body.get("kind"), body.get("model")))["id"]}
    except Exception as e:  # the model server is stopped, the library is empty, or the reply is not valid
        raise HTTPException(503, f"No writing task: {e}")


@app.post("/writing/{prompt_id}/review")
async def review_writing(prompt_id: int, request: Request):
    """{"text": "...", "model": "..."}: the review of text as an attempt; the
    page keeps the text if the review fails."""
    body = await _model_body(request, "model")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(422, "The answer is empty.")
    try:
        return {"id": (await writing.review(prompt_id, text, body.get("model")))["id"]}
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # the model server is stopped, or the reply is not valid
        raise HTTPException(503, f"No review: {e}")


@app.get("/how")
async def how(request: Request):
    return _page(request, "how.html", html=_markdown.render(HOW_PAGE.read_text()))
