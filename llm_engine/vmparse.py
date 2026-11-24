"""phdcode pdfs (ADR-0036): the weekly PDF batch. marker 2 parses the PDF
queue on a new GPU VM of VMQ (~/Desktop/dev/VMQ, profile surya), and the
results become full-text papers (parse_mode "balanced") of this library.

1. Wait for and import each earlier job of this library that is not imported.
2. phdcode fetch, all papers.
3. One VMQ job with each PDF of the queue (a URL, or a local upload).
4. Wait for the job, then import its results.

The VM makes no summaries: phdcode summarize makes them after this command,
for a large batch on a second VM, the Bonsai 2 service (bonsai_vm, ADR-0037).
The state of each job is in its folder, CODE_DIR/<library>-<time>/:
manifest.tsv, parse.sh, job.yaml, then job_id (submitted), then imported.
"""
import asyncio
import fcntl
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from . import CLONE
from .documents import PAPERS_DIR, text_path_for
from .fetch import MISS, RETRY_DAYS, fetch_pdfs
from .memory import db
from .memory.document_index import index_text
from .routes import resolve

log = logging.getLogger(__name__)

VMQ = Path(os.getenv("VMQ_DIR", str(Path.home() / "Desktop/dev/VMQ"))) / "vmq.py"
CODE_DIR = PAPERS_DIR.parent / "vmq"
PARSE_MODE = "balanced"
WAIT_S = 300
QUEUED_H = 24
_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# On the VM, in ~/job, after `. ~/.vmq-env` (VMQ profile surya: Surya on vLLM, ~/marker).
# stdin of curl and marker is /dev/null: the loop reads the manifest on stdin.
PARSE_SH = r"""#!/usr/bin/env bash
# One paper at a time; a failed paper goes into out/failed.tsv and the loop goes on.
set -u
mkdir -p in out
cp manifest.tsv out/
while IFS=$'\t' read -r id src _; do
  if [ "$src" = local ]; then
    cp "pdfs/$id.pdf" "in/$id.pdf"
  else
    case "$src" in *arxiv.org*) sleep 3;; esac  # arXiv asks for 3 s between requests
    if ! curl -fsSL --retry 2 --max-time 180 -A phdcode -o "in/$id.pdf" "$src" < /dev/null; then
      printf '%s\tdownload failed\n' "$id" >> out/failed.tsv; continue
    fi
  fi
  if [ "$(head -c 4 "in/$id.pdf")" != "%PDF" ]; then
    printf '%s\tnot a PDF\n' "$id" >> out/failed.tsv; continue
  fi
  if ! ~/marker/bin/marker_single "in/$id.pdf" --mode balanced --paginate_output --output_format markdown \
       --disable_tqdm --output_dir "out/$id" < /dev/null > "out/$id.log" 2>&1; then
    printf '%s\tmarker failed: %s\n' "$id" "$(tail -n 1 "out/$id.log" | tr '\t' ' ')" >> out/failed.tsv; continue
  fi
  cp "in/$id.pdf" "out/$id/"
done < manifest.tsv
"""


def _vmq(*args: str) -> str:
    return subprocess.run([sys.executable, str(VMQ), *args], capture_output=True, text=True, check=True).stdout


def _jobs() -> dict[str, tuple[str, str]]:
    """job id -> (state, results folder) of each VMQ job."""
    jobs = {}
    for line in _vmq("jobs").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            jobs[parts[0]] = (parts[2], parts[3] if len(parts) > 3 else "")
    return jobs


def write_job(papers: list[dict[str, Any]], library: str) -> Path:
    """The folder of a new job: manifest.tsv (paper_id, then the URL, or
    "local" and the path of the upload), the
    local PDFs in pdfs/, parse.sh and job.yaml (JSON is YAML)."""
    folder = CODE_DIR / f"{library}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    (folder / "pdfs").mkdir(parents=True)
    lines = []
    for p in papers:
        source = p["pdf_source"]
        if source.startswith(("http://", "https://")):
            lines.append(f"{p['id']}\t{source}\n")
        else:  # an upload: its path stays in the manifest, for a job with no result
            shutil.copy2(source, folder / "pdfs" / f"{p['id']}.pdf")
            lines.append(f"{p['id']}\tlocal\t{source}\n")
    (folder / "manifest.tsv").write_text("".join(lines))
    (folder / "parse.sh").write_text(PARSE_SH)
    job = {"name": CLONE.name, "profile": "surya", "code": str(folder), "run": "bash parse.sh",
           "outputs": ["out"], "max_hours": min(24, math.ceil(0.5 + len(papers) / 60))}
    (folder / "job.yaml").write_text(json.dumps(job, indent=2) + "\n")
    return folder


def submit(folder: Path) -> str:
    """Queues the job of folder in VMQ; returns the job id."""
    job_id = _vmq("submit", str(folder / "job.yaml")).split()[-2]
    (folder / "job_id").write_text(job_id + "\n")
    return job_id


def _wait(job_id: str, hours: float, ends: tuple[str, ...]) -> tuple[str, str]:
    """Waits until the VMQ job is in a state of ends: (state, the last column
    of vmq.py jobs: the results folder, or the IP of a serving job). The
    clock starts when the job leaves the queue (VMQ runs one job at a time,
    and a GPU can be out of stock): QUEUED_H in the queue, then hours. At
    the deadline: (the state, "")."""
    deadline = time.time() + QUEUED_H * 3600
    started = False
    while True:
        state, last = _jobs().get(job_id, ("missing", ""))
        if state in ends:
            return state, last
        if state != "queued" and not started:
            started, deadline = True, time.time() + hours * 3600
        if time.time() > deadline:
            return state, ""
        time.sleep(WAIT_S)


def wait(folder: Path) -> tuple[str, Optional[Path]]:
    """Waits until VMQ ends the job of folder: (state, results folder, or
    None when VMQ has no such job). A job still queued after QUEUED_H, or
    still running after max_hours + 1 h, returns its state, and the next run
    waits for it again."""
    hours = json.loads((folder / "job.yaml").read_text())["max_hours"]
    state, results = _wait((folder / "job_id").read_text().strip(), hours + 1, ("done", "failed", "missing"))
    return state, Path(results) if results else None


# The summaries of a large batch (ADR-0037): Bonsai 2 on a VMQ service VM
# (profile bonsai), which both clones share; its folder holds job.yaml, job_id
# and the lock. The port of the tunnel is the one of the route BONSAI_MODEL in
# LLM_ROUTING, a different port in each clone.
BONSAI_DIR = Path.home() / ".local/share/phdcode/bonsai"
BONSAI_MODEL, BONSAI_SLOTS, VM_MIN_PAPERS = "bonsai2-27b-vm", 8, 20
BONSAI_SETUP_H = 0.5
SSH_KEY = Path.home() / ".ssh/hyperstack-ifumiyi-key.pem"  # the key of VMQ
BONSAI_JOB = {"name": "phdcode-bonsai", "profile": "bonsai", "run": "true",
              "service": {"idle_hours": 0.5,
                          "busy": "curl -s 127.0.0.1:8081/slots | grep -q '\"is_processing\":true'"}}


def _tunnel(ip: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(["ssh", "-N", "-i", str(SSH_KEY), "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                             "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
                             "-L", f"{port}:127.0.0.1:8081", f"ubuntu@{ip}"], stdin=subprocess.DEVNULL)


@contextmanager
def bonsai_vm() -> Iterator[None]:
    """The Bonsai 2 service of VMQ through an SSH tunnel to the port of the
    route BONSAI_MODEL. A queued, running or serving job is reused, else a new
    one is submitted. The VM stops by itself after 30 min with no request
    (the idle stop of VMQ), so the other clone can reuse it. Raises
    RuntimeError when the job fails, or does not serve in time."""
    port = urlsplit(resolve(BONSAI_MODEL).endpoint).port
    BONSAI_DIR.mkdir(parents=True, exist_ok=True)
    with open(BONSAI_DIR / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # the other clone may submit at the same time
        job_file = BONSAI_DIR / "job_id"
        job_id = job_file.read_text().strip() if job_file.exists() else ""
        if _jobs().get(job_id, ("missing", ""))[0] not in ("queued", "running", "serving"):
            (BONSAI_DIR / "job.yaml").write_text(json.dumps(BONSAI_JOB, indent=2) + "\n")
            try:
                job_id = submit(BONSAI_DIR)
            except subprocess.CalledProcessError as e:  # for example, the credit is below the worst case
                raise RuntimeError(f"VMQ refused the Bonsai VM: {(e.stderr or e.stdout).strip()[-300:]}") from e
    state, ip = _wait(job_id, BONSAI_SETUP_H, ("serving", "done", "failed", "missing"))
    if state != "serving" or not ip:
        raise RuntimeError(f"the Bonsai VM (VMQ job {job_id}) is {state}, not serving; see vmq.py jobs")
    tunnel = _tunnel(ip, port)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).close()
                break
            except OSError:
                if tunnel.poll() is not None:
                    raise RuntimeError(f"the SSH tunnel to the Bonsai VM {ip} closed (exit code {tunnel.returncode})")
                time.sleep(2)
        else:
            raise RuntimeError(f"the Bonsai VM {ip} does not answer on the tunnel")
        yield
    finally:
        tunnel.terminate()
        tunnel.wait()


async def import_results(folder: Path, results: Optional[Path]) -> list[str]:
    """Makes a full-text paper of each paper of folder with a result. A
    paper that marker failed on gets pdf_error. A paper that the VM could
    not download goes back to the fetch as a miss (it tries the paper again
    after RETRY_DAYS); a paper with no result (the job stopped) goes back to
    the next fetch. Then deletes the copies of the PDFs (results/out, the
    uploads in folder/pdfs)."""
    failed, out_root = {}, (results / "out") if results else None
    if out_root and (out_root / "failed.tsv").exists():
        for line in (out_root / "failed.tsv").read_text().splitlines():
            paper_id, _, reason = line.partition("\t")
            failed[paper_id] = reason
    report = []
    for line in (folder / "manifest.tsv").read_text().splitlines():
        paper_id, source, *upload = line.split("\t")
        out = out_root / paper_id if out_root else None
        texts = sorted(out.rglob("*.md")) if out and out.is_dir() else []
        reason = failed.get(paper_id, "")
        paper = await db.get_paper(int(paper_id))
        if paper is None or paper["parse_mode"]:
            report.append(f"paper {paper_id}: skipped, deleted or full text already")
        elif texts and (out / f"{paper_id}.pdf").is_file():
            target = PAPERS_DIR / paper_id
            shutil.rmtree(target, ignore_errors=True)  # one paper, one PDF (as ingest_full_text)
            target.mkdir(parents=True)
            pdf_path = shutil.copy2(out / f"{paper_id}.pdf", target)
            text_path = text_path_for(Path(pdf_path))
            shutil.copy2(texts[0], text_path)
            for image in (f for f in texts[0].parent.iterdir() if f.suffix.lower() in _IMAGES):
                (target / "images").mkdir(exist_ok=True)
                shutil.copy2(image, target / "images")
            await index_text(int(paper_id), PARSE_MODE, Path(pdf_path), text_path)
            report.append(f"paper {paper_id}: full text")
        elif reason.startswith("marker failed"):
            await db.set_pdf_error(int(paper_id), f"VM parse: {reason}"[:500])
            report.append(f"paper {paper_id}: failed on the VM: {reason}")
        elif reason:  # download failed, not a PDF
            await db.unqueue_pdf(int(paper_id), f"{MISS} {date.today()}")
            report.append(f"paper {paper_id}: {reason} on the VM; the fetch tries again in {RETRY_DAYS} days")
        elif source == "local":
            await db.queue_pdf(int(paper_id), upload[0])
            report.append(f"paper {paper_id}: no result; the upload waits in the queue again")
        else:
            await db.unqueue_pdf(int(paper_id))
            report.append(f"paper {paper_id}: no result; back to the next fetch")
    if out_root:
        shutil.rmtree(out_root, ignore_errors=True)
    shutil.rmtree(folder / "pdfs", ignore_errors=True)
    (folder / "imported").write_text(f"{results}\n")
    return report


async def _finish_open_jobs() -> tuple[list[str], int]:
    """Waits for and imports each submitted job of this clone that is not
    imported. A job that is still queued or running stays open."""
    report, problems = [], 0
    for folder in sorted(CODE_DIR.glob("*/job_id")):
        folder = folder.parent
        if (folder / "imported").exists():
            continue
        state, results = await asyncio.to_thread(wait, folder)
        report.append(f"VMQ job {folder.name}: {state}")
        if state in ("done", "failed", "missing"):
            report += await import_results(folder, results)
        problems += state != "done"
    return report, problems


async def pdfs() -> tuple[list[str], int]:
    """`phdcode pdfs`. Returns one line for each step and paper, and the
    number of problems (failed lookups of the fetch, VMQ jobs that failed,
    were refused or did not end)."""
    library = urlsplit(db.DATABASE_URL or "").path.lstrip("/")
    # One run at a time: a run of the button waits hours for its job, and the
    # Monday run must not import that job a second time.
    CODE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CODE_DIR / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return [f"another phdcode pdfs runs for {library}"], 0
        return await _pdfs(library)


async def _pdfs(library: str) -> tuple[list[str], int]:
    report, problems = await _finish_open_jobs()
    fetched, failed = await fetch_pdfs(None)
    report = [fetched[0], *report, *fetched[1:]]
    papers = await db.queued_pdfs()
    if papers:
        folder = write_job(papers, library)
        try:
            job_id = submit(folder)
        except subprocess.CalledProcessError as e:  # refused: no flavor fits the limit or the credit now
            for p in papers:
                if p["pdf_source"].startswith(("http://", "https://")):
                    await db.unqueue_pdf(p["id"])
            shutil.rmtree(folder)
            return [*report, f"VMQ refused the job: {(e.stderr or e.stdout).strip()[-300:]}"], failed + problems + 1
        # Held: phdcode ingest (an upload on the interface) does not parse them on the server.
        await db.hold_pdfs([p["id"] for p in papers], folder.name)
        report.append(f"VMQ job {job_id} ({folder.name}): {len(papers)} PDFs")
        more, problems_now = await _finish_open_jobs()
        report += more
        problems += problems_now
    return report, failed + problems
