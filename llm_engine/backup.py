"""`phdcode backup`: a dump of the phdcode tables, and a one-way copy of the
paper folders and the dumps to BACKUP_REMOTE (an rclone remote path).

    BACKUP_REMOTE/papers/<paper_id>/   = PAPERS_DIR (rclone sync: the same files)
    BACKUP_REMOTE/database/            = the last DUMPS_KEPT dumps

The server is the only writer: a change made at the remote goes at the next
copy. With no BACKUP_REMOTE, only the dump is made. The summaries took
hours of GPU time, so the dump is the copy that matters most; the chunks
can be made again from the parsed text.

Postgres runs in Docker (docker-compose.yml), so pg_dump runs in its
container (POSTGRES_CONTAINER).
"""
import gzip
import os
import subprocess
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .documents import PAPERS_DIR

load_dotenv()

BACKUP_REMOTE = os.getenv("BACKUP_REMOTE", "").rstrip("/")
POSTGRES_CONTAINER = os.getenv("POSTGRES_CONTAINER", "alira2-postgres-1")
DUMP_DIR = PAPERS_DIR.parent / "backup"
DUMPS_KEPT = 7
TABLES = ("papers", "keywords", "paper_keywords", "chunks", "saved_searches", "triaged_hits")


def dump() -> Path:
    """DUMP_DIR/phdcode-YYYY-MM-DD.sql.gz (a dump of the same day is
    replaced); the oldest dumps go, so that DUMPS_KEPT stay."""
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    path = DUMP_DIR / f"phdcode-{date.today():%Y-%m-%d}.sql.gz"
    command = ["docker", "exec", POSTGRES_CONTAINER, "pg_dump", "--no-owner",
               "-U", os.environ["POSTGRES_USER"], "-d", os.environ["POSTGRES_DB"],
               *(arg for table in TABLES for arg in ("-t", table))]
    result = subprocess.run(command, capture_output=True, check=True)
    with gzip.open(path, "wb") as f:
        f.write(result.stdout)
    for old in sorted(DUMP_DIR.glob("phdcode-*.sql.gz"))[:-DUMPS_KEPT]:
        old.unlink()
    return path


def copy() -> None:
    for source, target in ((PAPERS_DIR, "papers"), (DUMP_DIR, "database")):
        subprocess.run(["rclone", "sync", str(source), f"{BACKUP_REMOTE}/{target}"], check=True)


def backup() -> list[str]:
    path = dump()
    report = [f"dump: {path} ({path.stat().st_size >> 10} KiB)"]
    if BACKUP_REMOTE:
        copy()
        report.append(f"copied to {BACKUP_REMOTE}: papers/ and database/")
    else:
        report.append("BACKUP_REMOTE is not set: no copy")
    return report
