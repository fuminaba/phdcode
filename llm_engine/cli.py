"""The phdcode command: the work that runs outside a harness session.

    phdcode sweep        runs all saved searches and writes the digest
    phdcode import FILE  a reference list (BibTeX) into the library, with its
                         PDFs in the queue
    phdcode queue FILES  local PDFs into the queue, each to its stored paper
                         (by its DOI or its title) or to a new paper
    phdcode ingest       parses each PDF in the queue (no summaries)
    phdcode rechunk      indexes the parsed text of the full-text papers again
                         (no new parse): after a change to the chunker
    phdcode backup       dumps the phdcode tables, and copies the paper
                         folders and the dumps to BACKUP_REMOTE
    phdcode summarize    makes the pending summaries (and the metadata of a
                         local PDF) through SUMMARY_MODEL
    phdcode gpu clear    before a training run: stops llama-server, and no
                         phdcode GPU user starts until `phdcode gpu restore`
    phdcode gpu restore  after the training run
    phdcode gpu check N  exit code 0 if N MiB of VRAM is free (for the
                         llama-server unit)
"""
import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from typing import Awaitable

log = logging.getLogger("phdcode")


def main() -> None:
    parser = argparse.ArgumentParser(prog="phdcode")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sweep", help="run all saved searches and write the digest")
    importer = commands.add_parser("import", help="a reference list (BibTeX) into the library")
    importer.add_argument("file", type=Path)
    importer.add_argument("--zotero-storage", type=Path,
                          help="a copy of Zotero's storage folder: its PDFs go first into the queue")
    queue = commands.add_parser("queue", help="local PDFs into the queue, each to its paper; then phdcode ingest")
    queue.add_argument("files", type=Path, nargs="+")
    queue.add_argument("--paper", type=int, help="the paper_id of the PDF, if phdcode finds the wrong paper (one PDF)")
    fetch = commands.add_parser("fetch", help="an open-access PDF for each abstract-only paper into the queue (ADR-0036)")
    fetch.add_argument("--limit", type=int, help="at most this many papers, newest first (default: all)")
    fetch.add_argument("--dry-run", action="store_true", help="print the link of each paper; change nothing")
    commands.add_parser("pdfs", help="fetch, then parse the PDF queue on a VMQ GPU VM and import it (ADR-0036)")
    ingest = commands.add_parser("ingest", help="parse each PDF in the queue; then run phdcode summarize")
    ingest.add_argument("--limit", type=int, help="parse at most this many PDFs")
    summarize = commands.add_parser("summarize", help="make the pending summaries through SUMMARY_MODEL")
    summarize.add_argument("--vm", action="store_true",
                           help="from 20 pending papers on: Bonsai 2 on a VMQ VM, 8 papers at a time (ADR-0037)")
    commands.add_parser("headscore", help="the shadow score of the System One head for each judged hit (ADR-0028)")
    rechunk = commands.add_parser("rechunk", help="index the parsed text of the full-text papers again (no parse)")
    rechunk.add_argument("paper_ids", type=int, nargs="*", help="only these papers")
    commands.add_parser("backup", help="dump the tables; copy the papers and the dumps to BACKUP_REMOTE")
    gpu_parser = commands.add_parser("gpu", help="the GPU around a training run (ADR-0017)")
    gpu_commands = gpu_parser.add_subparsers(dest="gpu_command", required=True)
    gpu_commands.add_parser("clear", help="before training: stop llama-server; no phdcode GPU user starts")
    gpu_commands.add_parser("restore", help="after training: phdcode may use the GPU again")
    check = gpu_commands.add_parser("check", help="exit code 0 if MIB of VRAM is free")
    check.add_argument("mib", type=int)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.command == "sweep":
        from .sweep import sweep
        path, failed = asyncio.run(sweep())
        print(path)
        if failed:
            raise SystemExit(f"{failed} saved searches failed; their lines are in {path}")
    elif args.command == "import":
        from .bib_import import import_bib
        print("\n".join(asyncio.run(_with_db(import_bib(args.file, args.zotero_storage)))))
    elif args.command == "queue":
        from .memory import queue_local_pdfs
        print("\n".join(asyncio.run(_with_db(queue_local_pdfs(args.files, args.paper)))))
    elif args.command == "fetch":
        from .fetch import fetch_pdfs
        report, failed = asyncio.run(_with_db(fetch_pdfs(args.limit or None, args.dry_run)))
        print("\n".join(report))
        if failed:
            raise SystemExit(f"{failed} lookups failed; the next fetch tries these papers again")
    elif args.command == "pdfs":
        from .vmparse import pdfs
        report, problems = asyncio.run(_with_db(pdfs()))
        print("\n".join(report))
        if problems:
            raise SystemExit(f"{problems} failed lookups or VMQ jobs; see the lines above")
    elif args.command == "ingest":
        from . import gpu
        from .memory import ingest_queue
        if gpu.llm_active():
            log.warning("llama-server holds the GPU, so each parse runs on the CPU (about 4 minutes "
                        "for each paper). To parse on the GPU, stop it first: "
                        "systemctl --user stop phdcode-llm-proxy.service")
        print("\n".join(asyncio.run(_with_db(ingest_queue(args.limit)))) or "The PDF queue is empty.")
    elif args.command == "summarize":
        from .memory import pending_papers, run_pending_steps
        model, parallel = None, 1  # SUMMARY_MODEL, one paper at a time
        if args.vm:
            from .vmparse import BONSAI_MODEL, BONSAI_SLOTS, VM_MIN_PAPERS, bonsai_vm
            pending = len(asyncio.run(_with_db(pending_papers())))
            if pending >= VM_MIN_PAPERS:
                model, parallel = BONSAI_MODEL, BONSAI_SLOTS
        try:
            with (bonsai_vm() if model else contextlib.nullcontext()):
                report = asyncio.run(_with_db(run_pending_steps(model, parallel)))
        except RuntimeError as e:  # the VM failed: the papers stay pending for the next run
            raise SystemExit(f"No summaries: {e}")
        print("\n".join(report) or "No pending papers.")
    elif args.command == "rechunk":
        from .memory import rechunk
        print("\n".join(asyncio.run(_with_db(rechunk(args.paper_ids or None)))) or "No full-text papers.")
    elif args.command == "headscore":
        from .headscore import headscore
        print(asyncio.run(headscore()))
    elif args.command == "backup":
        from .backup import backup
        print("\n".join(backup()))
    elif args.command == "gpu":
        from . import gpu
        if args.gpu_command == "clear":
            gpu.clear()
            print(f"The GPU is cleared: {gpu.free_vram_mib()} MiB free. Run `phdcode gpu restore` after training.")
        elif args.gpu_command == "restore":
            gpu.restore()
            print("The GPU is restored: llama-server starts at the next request.")
        else:
            free = gpu.free_vram_mib()
            print(f"{free} MiB free, {args.mib} MiB needed.", file=sys.stderr)
            sys.exit(0 if free >= args.mib else 1)


async def _with_db[T](work: Awaitable[T]) -> T:
    from .memory import close_pool, init_schema
    await init_schema()
    try:
        return await work
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
