"""phdcode pdfs on phdcode_test (ADR-0036), with no VM.

1. The job folder: the manifest (a URL; an upload with its path), job.yaml
   that `vmq.py check` accepts.
2. The import of a fake results folder: a parsed paper becomes full text
   ("balanced", chunks on its pages, figures); a marker failure gets
   pdf_error; a failed download goes back to the fetch as a dated miss; a
   lost URL paper goes back to the fetch; a lost upload waits in the queue
   again; the copies of the PDFs are deleted; an imported job is not
   imported again.
3. A held paper (a job not imported yet) is not in queued_pdfs, so phdcode
   ingest does not parse it on the server.
4. pdfs() as one piece, with VMQ stubbed: while another run holds the lock,
   pdfs() does nothing; a refused job sends its URL
   papers back to the fetch; an accepted job is held, waited for (queued,
   running, done) and imported.
5. run_pending_steps(parallel=3) runs 3 papers at once and keeps their order
   in the report (fake model steps).
6. bonsai_vm() with VMQ stubbed and a local stand-in for the SSH tunnel: it
   submits the service job, waits until serving, opens the tunnel, reuses a
   serving job, and raises for a failed job.

    export DATABASE_URL="${DATABASE_URL%/*}/phdcode_test"   # before Python starts
    export LOG_DIR=<an absolute folder under the home folder>   # VMQ copies only code under home
    python tests/test_vmparse.py
"""
import asyncio
import fcntl
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

assert os.environ.get("DATABASE_URL", "").endswith("/phdcode_test"), "Set DATABASE_URL to phdcode_test first."
assert os.environ.get("LOG_DIR", "").startswith("/"), "Set LOG_DIR to an absolute folder first."

from llm_engine import fetch, vmparse  # noqa: E402  (after the checks)
from llm_engine.memory import db, document_index  # noqa: E402
from llm_engine.routes import resolve  # noqa: E402

SEP = "-" * 48
TEXT = (f"{{0}}{SEP}\n\n# Methods\n\nWe parse each PDF on a GPU VM with marker. It runs once a week.\n\n"
        f"![](_page_0_Figure_1.jpeg)\n\n{{1}}{SEP}\n\n# Results\n\nThe batch makes full-text papers.\n")


def fake_result(out: Path, paper_id: int) -> None:
    """The output of parse.sh for one paper: out/<id>/<id>/<id>.md, a figure, out/<id>/<id>.pdf."""
    (out / str(paper_id) / str(paper_id)).mkdir(parents=True)
    (out / str(paper_id) / str(paper_id) / f"{paper_id}.md").write_text(TEXT)
    (out / str(paper_id) / str(paper_id) / "_page_0_Figure_1.jpeg").write_bytes(b"jpeg")
    (out / str(paper_id) / f"{paper_id}.pdf").write_bytes(b"%PDF-1.4 " + str(paper_id).encode())


async def column(pool, name: str, paper_id: int):
    return await pool.fetchval(f"SELECT {name} FROM papers WHERE id = $1", paper_id)


async def main() -> None:
    log_dir = Path(os.environ["LOG_DIR"])
    assert vmparse.CODE_DIR == log_dir / "vmq", vmparse.CODE_DIR
    await db.init_schema()
    pool = await db.get_pool()
    await pool.execute("DELETE FROM papers WHERE source_id LIKE 'test-vm-%'")
    names = ("good", "bad", "dl", "lost", "upload", "held", "a", "b")
    ids = dict(zip(names, [await db.save_paper("openalex", f"test-vm-{n}", f"vm test {n}", [], None, None)
                           for n in names]))
    upload = log_dir / "upload.pdf"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_bytes(b"%PDF-1.4 upload")
    for name in ("good", "bad", "dl", "lost", "upload", "held"):
        await db.queue_pdf(ids[name], str(upload) if name == "upload" else f"https://example.org/{name}.pdf")
    try:
        # 1. The job folder.
        papers = [p for p in await db.queued_pdfs() if p["id"] in ids.values()]
        folder = vmparse.write_job(papers, "phdcode_test")
        manifest = {line.split("\t")[0]: line.split("\t")[1:]
                    for line in (folder / "manifest.tsv").read_text().splitlines()}
        assert manifest[str(ids["upload"])] == ["local", str(upload)], manifest
        assert manifest[str(ids["good"])] == ["https://example.org/good.pdf"]
        assert (folder / "pdfs" / f"{ids['upload']}.pdf").read_bytes() == b"%PDF-1.4 upload"
        job = json.loads((folder / "job.yaml").read_text())
        assert job["profile"] == "surya" and job["run"] == "bash parse.sh" and job["max_hours"] == 1, job
        check = subprocess.run([sys.executable, str(vmparse.VMQ), "check", str(folder / "job.yaml")],
                               capture_output=True, text=True)
        assert check.returncode == 0, check.stdout + check.stderr

        # 2. The import.
        results = log_dir / "results"
        fake_result(results / "out", ids["good"])
        (results / "out" / "failed.tsv").write_text(
            f"{ids['bad']}\tmarker failed: boom\n{ids['dl']}\tdownload failed\n")
        await vmparse.import_results(folder, results)
        assert await column(pool, "parse_mode", ids["good"]) == "balanced"
        pdf_path = Path(await column(pool, "pdf_path", ids["good"]))
        assert pdf_path.read_bytes() == b"%PDF-1.4 " + str(ids["good"]).encode()
        assert Path(await column(pool, "text_path", ids["good"])).read_text() == TEXT
        assert (pdf_path.parent / "images" / "_page_0_Figure_1.jpeg").exists()
        pages = await pool.fetchrow("SELECT min(page_start) AS first, max(page_end) AS last FROM chunks "
                                    "WHERE paper_id = $1", ids["good"])
        assert (pages["first"], pages["last"]) == (1, 2), "the page marks of marker give pages 1 and 2"
        assert await column(pool, "pdf_error", ids["bad"]) == "VM parse: marker failed: boom"
        assert await column(pool, "pdf_source", ids["dl"]) is None
        assert await column(pool, "pdf_error", ids["dl"]) == f"{fetch.MISS} {date.today()}"
        assert await column(pool, "pdf_source", ids["lost"]) is None
        assert await column(pool, "pdf_error", ids["lost"]) is None
        assert await column(pool, "pdf_source", ids["upload"]) == str(upload), "a lost upload waits again"
        assert not (results / "out").exists() and not (folder / "pdfs").exists(), "the copies are deleted"
        (folder / "job_id").write_text("20990101-000000\n")
        assert await vmparse._finish_open_jobs() == ([], 0), "an imported job is not imported again"

        # 3. A held paper is not in the queue of phdcode ingest.
        await db.hold_pdfs([ids["held"]], "some-job")
        assert ids["held"] not in [p["id"] for p in await db.queued_pdfs()]
        assert await column(pool, "pdf_source", ids["held"]) == "vmq:some-job"

        # 4. pdfs() with VMQ stubbed.
        for name in ("upload", "held"):  # out of the way of part 4
            await db.unqueue_pdf(ids[name])

        async def no_fetch(limit=None, dry_run=False):
            return ["database phdcode_test"], 0
        vmparse.fetch_pdfs = no_fetch
        vmparse.WAIT_S = 0
        mine = {ids["a"], ids["b"]}

        def no_submit(*args):
            raise AssertionError(f"a second run called vmq {args}")
        vmparse._vmq = no_submit
        with open(vmparse.CODE_DIR / "lock", "w") as lock:  # another run holds the lock
            fcntl.flock(lock, fcntl.LOCK_EX)
            assert await vmparse.pdfs() == (["another phdcode pdfs runs for phdcode_test"], 0)

        def refuse(*args):
            if args[0] == "submit":
                raise subprocess.CalledProcessError(1, "vmq", output="", stderr="vmq: no flavor fits")
            return ""
        for name in ("a", "b"):
            await db.queue_pdf(ids[name], f"https://example.org/{name}.pdf")
        vmparse._vmq = refuse
        report, problems = await vmparse.pdfs()
        assert problems == 1 and "VMQ refused the job" in report[-1], report
        assert all([await column(pool, "pdf_source", i) is None for i in mine]), "back to the fetch"

        states = iter(["queued", "running", "done"])
        results2 = log_dir / "results2"

        def vmq(*args):
            if args[0] == "submit":
                job_folder = Path(json.loads(Path(args[1]).read_text())["code"])
                for line in (job_folder / "manifest.tsv").read_text().splitlines():
                    if int(line.split("\t")[0]) in mine:
                        fake_result(results2 / "out", int(line.split("\t")[0]))
                return "  n3-RTX-A4000x1 in NORWAY-1: ok\n20990102-000000 /results/\n"
            state = next(states)
            return f"20990102-000000 phdcode {state} {results2 if state == 'done' else ''}\n"
        for name in ("a", "b"):
            await db.queue_pdf(ids[name], f"https://example.org/{name}.pdf")
        vmparse._vmq = vmq
        report, problems = await vmparse.pdfs()
        assert problems == 0, report
        assert any(line.startswith("VMQ job 20990102-000000") for line in report), report
        for i in mine:
            assert await column(pool, "parse_mode", i) == "balanced"
            assert (await column(pool, "pdf_source", i)).startswith("vmq:phdcode_test-"), "held until the import"

        # 5. The summaries of several papers at once, in the order of the papers (fake model steps).
        running, most = 0, 0

        async def fake_steps(paper_id, text, needs_metadata, model_name):
            nonlocal running, most
            running += 1
            most = max(most, running)
            await asyncio.sleep(0.05 * (6 - paper_id))  # the later papers end first
            running -= 1
            return [] if paper_id != 3 else ["methods: the model failed"]
        text = log_dir / "text.md"
        text.write_text("text")
        document_index.pending_papers = lambda: asyncio.sleep(0, [{"id": n, "text_path": str(text), "needs_metadata": False}
                                                                 for n in range(1, 6)])
        document_index.run_model_steps = fake_steps
        report = await document_index.run_pending_steps("bonsai2-27b-vm", parallel=3)
        assert report == [f"paper {n}: done" if n != 3 else "paper 3: methods: the model failed" for n in range(1, 6)], report
        assert most == 3, most

        # 6. The Bonsai VM: submit, wait until serving, open the tunnel, reuse a serving job.
        port = urlsplit(resolve(vmparse.BONSAI_MODEL).endpoint).port
        vmparse.BONSAI_DIR = log_dir / "bonsai"
        states = iter(["queued", "running", "serving"])
        submitted = []

        def vmq2(*args):
            if args[0] == "submit":
                submitted.append(json.loads(Path(args[1]).read_text()))
                return "  n3-L40x1 in CANADA-1: ok\n20990103-000000 /results/\n"
            state = next(states, "serving")
            return f"20990103-000000 phdcode-bonsai {state} {'10.0.0.9' if state == 'serving' else ''}\n"

        class Health(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/health" else 404)
                self.end_headers()

            def log_message(self, *args):
                pass

        class FakeTunnel:  # stands for ssh -L: a local server that answers /health
            def __init__(self, ip, tunnel_port):
                assert (ip, tunnel_port) == ("10.0.0.9", port), (ip, tunnel_port)
                self.server = http.server.HTTPServer(("127.0.0.1", port), Health)
                threading.Thread(target=self.server.serve_forever, daemon=True).start()
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                self.server.shutdown()
                self.server.server_close()

            def wait(self):
                pass
        vmparse._vmq, vmparse._tunnel = vmq2, FakeTunnel
        for _ in range(2):  # the second use finds the serving job: no second submit
            with vmparse.bonsai_vm():
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).close()
        assert len(submitted) == 1 and submitted[0]["profile"] == "bonsai" and "service" in submitted[0], submitted
        vmparse._vmq = lambda *args: ("  ok\n20990104-000000 /results/\n" if args[0] == "submit"
                                      else "20990104-000000 phdcode-bonsai failed /results/\n")
        try:
            with vmparse.bonsai_vm():
                raise AssertionError("a failed job must not serve")
        except RuntimeError as e:
            assert "job 20990104-000000) is failed" in str(e), e
        vmparse._vmq = refuse  # a refused submit is a RuntimeError too: the papers stay pending
        try:
            with vmparse.bonsai_vm():
                raise AssertionError("a refused job must not serve")
        except RuntimeError as e:
            assert "VMQ refused the Bonsai VM: vmq: no flavor fits" in str(e), e
    finally:
        await pool.execute("DELETE FROM chunks WHERE paper_id IN (SELECT id FROM papers WHERE source_id LIKE 'test-vm-%')")
        await pool.execute("DELETE FROM papers WHERE source_id LIKE 'test-vm-%'")
        await db.close_pool()
        shutil.rmtree(log_dir)
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
