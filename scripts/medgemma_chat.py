#!/usr/bin/env python3
"""Interactive REPL for asking MedGemma questions about an image.

Keeps both the weights (~8.2GB, ~6s to load) and the decoded image
resident across questions, so only the first turn pays the load cost.
The image is encoded once into the first user turn rather than re-sent
each time -- see llm_engine.vision.VisionSession.

Usage:
    python scripts/medgemma_chat.py scan.png
    python scripts/medgemma_chat.py scan.png -q "Describe the findings."
    python scripts/medgemma_chat.py            # start with no image, /image later

In-session commands:
    /image PATH   switch image (clears history -- see set_image)
    /reset        clear history, keep image
    /system TEXT  set the system prompt and clear history
    /tokens N     change the generation cap
    /vram         show current VRAM use
    /help         list commands
    /exit         quit (Ctrl-D and Ctrl-C also work)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_engine import vision  # noqa: E402

BANNER = "MedGemma REPL -- /help for commands, /exit to quit"


def vram_line() -> str:
    try:
        import torch
        if not torch.cuda.is_available():
            return "CUDA unavailable"
        used = torch.cuda.memory_allocated() // (1024 * 1024)
        total = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
        return f"{used} MiB / {total} MiB"
    except Exception as e:
        return f"unavailable ({e})"


def show(session: vision.VisionSession, question: str, max_new_tokens: int) -> None:
    """Runs one turn and prints the answer with its timing."""
    try:
        answer = session.ask(question, max_new_tokens=max_new_tokens)
    except Exception as e:
        # A bad turn shouldn't end the session -- the usual causes
        # (context overflow, a malformed image) are recoverable with
        # /reset or /image rather than a restart.
        print(f"error: {e}", file=sys.stderr)
        return
    s = session.last_stats
    print(f"\n{answer}\n")
    print(f"  [{s['new_tokens']} tok in {s['seconds']:.1f}s -- "
          f"{s['tokens_per_second']:.1f} tok/s, prompt {s['prompt_tokens']}]\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Ask MedGemma about an image.")
    ap.add_argument("image", nargs="?", help="path to an image")
    ap.add_argument("-q", "--question", help="ask one question and exit")
    ap.add_argument("-s", "--system", help="system prompt (e.g. a specialty framing)")
    ap.add_argument("-n", "--max-new-tokens", type=int, default=512)
    ap.add_argument("-m", "--model", help="override MEDGEMMA_MODEL")
    args = ap.parse_args()

    try:
        session = vision.VisionSession(
            image_path=args.image, system_prompt=args.system, model_id=args.model,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    max_new_tokens = args.max_new_tokens

    # One-shot mode: print only the answer, so it pipes cleanly.
    if args.question:
        try:
            print(session.ask(args.question, max_new_tokens=max_new_tokens))
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    print(BANNER)
    print(f"image: {session.image_path or '(none -- /image PATH to set one)'}")
    print("loading weights on first question...\n")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        if line.startswith("/"):
            cmd, _, rest = line.partition(" ")
            rest = rest.strip()
            if cmd in ("/exit", "/quit"):
                break
            if cmd == "/help":
                print(__doc__.split("In-session commands:")[1].rstrip(), "\n")
            elif cmd == "/image":
                try:
                    session.set_image(rest)
                    print(f"image: {rest} (history cleared)\n")
                except FileNotFoundError as e:
                    print(f"error: {e}\n", file=sys.stderr)
            elif cmd == "/reset":
                session.reset()
                print("history cleared\n")
            elif cmd == "/system":
                session.system_prompt = rest or None
                session.reset()
                print(f"system prompt {'set' if rest else 'cleared'}, history cleared\n")
            elif cmd == "/tokens":
                if rest.isdigit():
                    max_new_tokens = int(rest)
                    print(f"max_new_tokens = {max_new_tokens}\n")
                else:
                    print("usage: /tokens N\n")
            elif cmd == "/vram":
                print(f"{vram_line()}\n")
            else:
                print(f"unknown command {cmd} -- /help\n")
            continue

        if session.image_path is None:
            print("no image set -- /image PATH first "
                  "(or ask anyway; it answers text-only)\n")
        show(session, line, max_new_tokens)

    vision.unload()
    print("unloaded, VRAM released.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
