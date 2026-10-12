"""The phdcode command: the work that runs outside a harness session.

    phdcode sweep        runs all saved searches and writes the digest
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="phdcode")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sweep", help="run all saved searches and write the digest")
    commands.add_parser("summarize", help="make the pending summaries through SUMMARY_MODEL")
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
    elif args.command == "summarize":
        print("\n".join(asyncio.run(_summarize())) or "No pending papers.")
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


async def _summarize() -> list[str]:
    from .memory import close_pool, init_schema, run_pending_steps
    await init_schema()
    try:
        return await run_pending_steps()
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
