"""The phdcode command: the work that runs outside a harness session.

    phdcode sweep        runs all saved searches and writes the digest
    phdcode import FILE  a reference list (BibTeX) into the library, with its
                         PDFs in the queue
    phdcode ingest       parses each PDF in the queue (no summaries)
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
    ingest = commands.add_parser("ingest", help="parse each PDF in the queue; then run phdcode summarize")
    ingest.add_argument("--limit", type=int, help="parse at most this many PDFs")
    commands.add_parser("summarize", help="make the pending summaries through SUMMARY_MODEL")
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
        print(asyncio.run(sweep()))
    elif args.command == "import":
        from .bib_import import import_bib
        print("\n".join(asyncio.run(_with_db(import_bib(args.file, args.zotero_storage)))))
    elif args.command == "ingest":
        from . import gpu
        from .memory import ingest_queue
        if gpu.llm_active():
            log.warning("llama-server holds the GPU, so each parse runs on the CPU (about 4 minutes "
                        "for each paper). To parse on the GPU, stop it first: "
                        "systemctl --user stop phdcode-llm-proxy.service")
        print("\n".join(asyncio.run(_with_db(ingest_queue(args.limit)))) or "The PDF queue is empty.")
    elif args.command == "summarize":
        from .memory import run_pending_steps
        print("\n".join(asyncio.run(_with_db(run_pending_steps()))) or "No pending papers.")
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


async def _with_db(work: Awaitable[list[str]]) -> list[str]:
    from .memory import close_pool, init_schema
    await init_schema()
    try:
        return await work
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
