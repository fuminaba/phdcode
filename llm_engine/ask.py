"""The page Ask (ADR-0030): an agent harness answers one question from the
library with the local read-only phdcode tools only, and each quote of the
answer is checked against the papers that it cites.

The harness is dcode with Bonsai 2 on llama-server, or Claude Code
(claude -p) with a Claude route of LLM_ROUTING. phdcode has no agent loop of
its own (ADR-0016): this module starts the harness and reads its answer.

    python -m llm_engine.ask   # the self-check of the quote check
"""
import asyncio
import json
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .generation import CLAUDE_BIN
from .routes import ROUTE_NAMES, resolve

# A systemd user unit has no ~/.local/bin in PATH.
DCODE_BIN = shutil.which("dcode") or str(Path.home() / ".local/bin/dcode")
MCP_BIN = str(Path(sys.executable).parent / "phdcode-mcp")
# The model name on the page for dcode, and its name in ~/.deepagents/config.toml.
DCODE = "bonsai2-27b"
_DCODE_MODEL = f"llama_server:{DCODE}"
# No tool that writes or calls an outside service: an agent that reads a
# secret (dcode's read_file reads any path) has no way to send it out.
TOOLS = ("search_papers", "search_chunks", "get_chunks")
TIMEOUT = 600

PROMPT = """\
Answer the question from the user's library, with the phdcode tools only.

- Search first: search_papers, then search_chunks on the papers that it
  finds; get_chunks reads the text around a passage. If the hits are weak,
  search again with other words.
- Cite each claim: the title of the paper, then [P<paper_id>], the page,
  and an exact quote from a chunk in double quotes. Copy each quote word for
  word; mark a cut with "...".
- If a claim rests only on an abstract, say so. If the library has nothing
  on the question, say so, and do not answer from memory.
- Answer in Markdown.

Question: {question}"""


def models() -> list[str]:
    """The choices of the page: dcode with Bonsai 2, then each Claude route."""
    return [DCODE, *sorted(r for r in ROUTE_NAMES if resolve(r).provider == "claude-code")]


async def answer(question: str, model: str) -> str:
    """The answer of the harness of model to question, in Markdown. It runs
    in an empty temporary folder, so that no project file or CLAUDE.md is
    read, with its own MCP config: phdcode with TOOLS only."""
    prompt = PROMPT.format(question=question)
    with tempfile.TemporaryDirectory(prefix="phdcode-ask-") as cwd:
        config = Path(cwd, "ask-mcp.json")
        config.write_text(json.dumps({"mcpServers": {"phdcode": {
            "command": MCP_BIN, "env": {"PHDCODE_TOOLS": ",".join(TOOLS)}}}}))
        if model == DCODE:
            # The config replaces the user-level server of the same name.
            command = [DCODE_BIN, "-n", prompt, "-M", _DCODE_MODEL, "-q", "--no-interpreter",
                       "--allow-fs-tools", "read_file", "--mcp-config", str(config), "--timeout", str(TIMEOUT)]
            stdin = b""
        else:
            command = [CLAUDE_BIN, "-p", "--model", resolve(model).name, "--tools", "", "--setting-sources", "",
                       "--strict-mcp-config", "--mcp-config", str(config),
                       "--allowedTools", ",".join(f"mcp__phdcode__{t}" for t in TOOLS),
                       "--disable-slash-commands", "--no-session-persistence", "--output-format", "json"]
            stdin = prompt.encode()
        process = await asyncio.create_subprocess_exec(
            *command, cwd=cwd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(stdin), TIMEOUT + 30)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(f"No answer in {TIMEOUT // 60} minutes.")
    out, err = out.decode(), err.decode()
    if model == DCODE:
        if process.returncode != 0:
            raise RuntimeError(f"dcode failed (exit {process.returncode}): {(err or out)[-300:]}")
        return out.strip()
    try:
        reply = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p failed (exit {process.returncode}): {(err or out)[:300]}")
    if reply.get("is_error"):
        raise RuntimeError(f"{model} gave no answer: {reply.get('result')}")
    return reply["result"].strip()


_CITE = re.compile(r"\[P(\d+)\]")
_QUOTE = re.compile(r'["“]([^"“”]*)["”]')
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# A shorter quote is a term ("hot" tumours), not a quote of the text.
_MIN_WORDS = 5


def _words(text: str) -> str:
    """The words of text, in lower case, with no Markdown link, no
    punctuation and no list dash: the model removes these from a quote."""
    text = unicodedata.normalize("NFKC", _LINK.sub(r"\1", text)).lower()
    return " ".join(re.findall(r"[^\W_]+", text))


def check_quotes(text: str, sources: list[str]) -> list[dict[str, Any]]:
    """Each quote of text (_MIN_WORDS words or more, in double quotes), with
    found = each part between two "..." is in one of sources. A title in
    quotes is found when sources holds the titles."""
    sources = [_words(s) for s in sources]
    checked = []
    for quote in _QUOTE.findall(text):
        parts = [p for p in map(_words, re.split(r"\.\.\.|…", quote)) if p]
        if sum(len(p.split()) for p in parts) >= _MIN_WORDS:
            checked.append({"quote": quote.strip(),
                            "found": any(all(p in s for p in parts) for s in sources)})
    return checked


async def cited_sources(text: str) -> list[str]:
    """The title and abstract, and each chunk, of each paper that text cites."""
    from .memory import get_pool
    ids = sorted({int(i) for i in _CITE.findall(text)})
    rows = await (await get_pool()).fetch(
        "SELECT concat_ws(' ', title, abstract) AS text FROM papers WHERE id = ANY($1) "
        "UNION ALL SELECT content FROM chunks WHERE paper_id = ANY($1)", ids)
    return [r["text"] for r in rows]


# The one run of the page: llama-server answers one request at a time.
# ponytail: kept in the process, so a restart of the interface loses the last
# answer; a table when the page needs a history.
run: dict[str, Any] = {"status": "idle"}


def start(question: str, model: str) -> None:
    """Starts a run in the background; the page reads `run`."""
    if run["status"] == "running":
        raise RuntimeError("A question is already running.")
    run.clear()
    run.update(status="running", question=question, model=model,
               started=datetime.now(timezone.utc), clock=time.monotonic())
    run["task"] = asyncio.create_task(_finish())


async def _finish() -> None:
    try:
        text = await answer(run["question"], run["model"])
        run.update(answer=text, quotes=check_quotes(text, await cited_sources(text)), status="done")
    except Exception as e:  # the model server is stopped, the harness failed, or the time ran out
        run.update(error=str(e), status="failed")
    finally:
        run["seconds"] = round(time.monotonic() - run["clock"])


def _self_check() -> None:
    chunk = "The TMEs are modelled as cellular graphs:\n- each node is a cell, and edges capture [neighbor](#ref) relations."
    text = ('In "Graph deep learning for tumour microenvironments" [P75], "each node is a cell, and edges capture '
            'neighbor relations" (p. 2). "Hot" tumours differ. "TMEs are modelled ... each node is a cell". '
            '"each node is a protein, and edges capture neighbor relations".')
    checked = check_quotes(text, ["Graph deep learning for tumour microenvironments", chunk])
    assert [c["found"] for c in checked] == [True, True, True, False], checked
    assert _CITE.findall(text) == ["75"]
    print("ask: self-check ok")


if __name__ == "__main__":
    _self_check()
