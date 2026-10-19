"""The shadow score of triage (ADR-0028): the System One head of bonsai2-sysone
(p2-rank16 on frozen Bonsai 2 27B) gives P(yes) for each judged hit, beside the
probability of triage. It decides nothing: the page Labels compares the two
on the user's labels.

`phdcode headscore` runs after the sweep. The head needs the GPU for about a
minute (the extractor loads its own Bonsai 2), so it stops llama-server
(gpu.clear) and starts its socket again after (gpu.restore). If a training run
has cleared the GPU, the hits wait for the next run.

bonsai2-sysone does the work in its own environment: score_triage.py takes the
library and the hits as JSON and returns the probabilities.
"""
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from . import gpu
from .memory import close_pool, init_schema, library_titles, record_head_probabilities, unscored_hits

load_dotenv()

log = logging.getLogger(__name__)

# The System One head (ADR-0028): its repository and the Python that runs it.
SYSONE_DIR = os.getenv("SYSONE_DIR")
SYSONE_PYTHON = os.getenv("SYSONE_PYTHON")


async def headscore() -> str:
    """Scores each unscored hit; returns one line for the log."""
    if not (SYSONE_DIR and SYSONE_PYTHON):
        return "headscore: SYSONE_DIR and SYSONE_PYTHON are not set in .env; no hits scored"
    await init_schema()
    try:
        if os.path.exists(gpu.GPU_CLEARED):
            return "headscore: the GPU is cleared for training; the hits wait for the next run"
        hits = await unscored_hits()
        if not hits:
            return "headscore: no hits to score"
        titles = {h["title"] for h in hits}
        # A hit is not part of its own library (a labelled hit may have been saved since).
        library = [t for t in await library_titles() if t not in titles]
        request = {"library": library, "hits": [{"id": f"{h['saved_search_id']}|{h['source_id']}", "title": h["title"],
                                                 "abstract": h["abstract"]} for h in hits]}
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "in.json").write_text(json.dumps(request))
            gpu.clear()
            try:
                subprocess.run([SYSONE_PYTHON, "score_triage.py", f"{tmp}/in.json", f"{tmp}/out.json"],
                               cwd=SYSONE_DIR, env={**os.environ, "PYTHONPATH": "."}, check=True)
            finally:
                gpu.restore()
            result = json.loads(Path(tmp, "out.json").read_text())
        scores = [(int(i.split("|", 1)[0]), i.split("|", 1)[1], p) for i, p in result["probabilities"].items()]
        await record_head_probabilities(scores)
        return f"headscore: {len(scores)} hits scored by {result['head']} (T = {result['temperature']:.3f})"
    finally:
        await close_pool()
