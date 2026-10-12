"""The phdcode command: the work that runs outside a harness session.

    phdcode sweep    runs all saved searches and writes the digest
"""
import argparse
import asyncio
import logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="phdcode")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sweep", help="run all saved searches and write the digest")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.command == "sweep":
        from .sweep import sweep
        print(asyncio.run(sweep()))


if __name__ == "__main__":
    main()
