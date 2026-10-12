"""The phdcode command: the work that runs outside a harness session.

    phdcode sweep        runs all saved searches and writes the digest
    phdcode summarize    makes the pending summaries (and the metadata of a
                         local PDF) through SUMMARY_MODEL
"""
import argparse
import asyncio
import logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="phdcode")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sweep", help="run all saved searches and write the digest")
    commands.add_parser("summarize", help="make the pending summaries through SUMMARY_MODEL")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.command == "sweep":
        from .sweep import sweep
        print(asyncio.run(sweep()))
    elif args.command == "summarize":
        print("\n".join(asyncio.run(_summarize())) or "No pending papers.")


async def _summarize() -> list[str]:
    from .memory import close_pool, init_schema, run_pending_steps
    await init_schema()
    try:
        return await run_pending_steps()
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
