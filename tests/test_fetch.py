"""phdcode fetch on phdcode_test (ADR-0036): a hit goes into the queue, a miss
gets its dated pdf_error and comes back after RETRY_DAYS, a failed lookup
changes nothing and takes no slot of the limit, and a failure of the ingest
stays out. The network part
(find_pdf) is a stub.

    export DATABASE_URL="${DATABASE_URL%/*}/phdcode_test"   # before Python starts
    python tests/test_fetch.py
"""
import asyncio
import os
from datetime import date, timedelta

assert os.environ.get("DATABASE_URL", "").endswith("/phdcode_test"), "Set DATABASE_URL to phdcode_test first."

from llm_engine import fetch  # noqa: E402  (after the check of the database)
from llm_engine.memory import db  # noqa: E402


async def main() -> None:
    await db.init_schema()
    pool = await db.get_pool()
    await pool.execute("DELETE FROM papers WHERE source_id LIKE 'test-fetch-%'")
    ids = [await db.save_paper("openalex", f"test-fetch-{i}", f"fetch test {i}", [], None, None, doi=f"10.0/t{i}")
           for i in range(4)]
    hit, miss, down, ingest_failed = ids
    await db.set_pdf_error(ingest_failed, "HTTPError: 403")

    def find_pdf(paper):
        if paper["id"] == hit:
            return "https://example.org/hit.pdf"
        if paper["id"] == miss:
            return None
        raise fetch.LookupFailed("down")  # also each paper of other tests: no change

    fetch.find_pdf = find_pdf
    selected = lambda: db.papers_to_fetch(None, fetch.MISS, fetch.RETRY_DAYS)  # noqa: E731
    try:
        assert [p["id"] for p in await selected()][:3] == [down, miss, hit], "newest first"
        # One slot: the failed lookup (down) does not take it; the miss does, so the hit waits.
        report, failed = await fetch.fetch_pdfs(1)
        assert report[0] == "database phdcode_test", report[0]
        assert failed == 1 and len(report) == 3, report
        assert await pool.fetchval("SELECT pdf_source FROM papers WHERE id = $1", hit) is None
        report, failed = await fetch.fetch_pdfs(None)
        assert failed >= 1 and failed <= fetch.FAILED_IN_A_ROW + 1, report
        rows = {r["id"]: r for r in await pool.fetch(
            "SELECT id, pdf_source, pdf_error FROM papers WHERE id = ANY($1)", ids)}
        assert rows[hit]["pdf_source"] == "https://example.org/hit.pdf"
        assert rows[miss]["pdf_error"] == f"{fetch.MISS} {date.today()}"
        assert rows[down]["pdf_source"] is None and rows[down]["pdf_error"] is None
        assert rows[ingest_failed]["pdf_error"] == "HTTPError: 403"

        order = [p["id"] for p in await selected()]
        assert down in order and hit not in order and miss not in order and ingest_failed not in order
        old = date.today() - timedelta(days=fetch.RETRY_DAYS)
        await db.set_pdf_error(miss, f"{fetch.MISS} {old}")
        order = [p["id"] for p in await selected()]
        assert order.index(down) < order.index(miss), "new papers before the retries"

        dry, _ = await fetch.fetch_pdfs(None, dry_run=True)
        assert dry[0] == "database phdcode_test, dry run"
        error = await pool.fetchval("SELECT pdf_error FROM papers WHERE id = $1", miss)
        assert error == f"{fetch.MISS} {old}", "a dry run writes nothing"
    finally:
        await pool.execute("DELETE FROM papers WHERE source_id LIKE 'test-fetch-%'")
        await db.close_pool()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
