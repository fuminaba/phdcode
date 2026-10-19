"""Triage: the probability that the user wants to read a hit (ADR-0018).

The summary model (TRIAGE_MODEL, default SUMMARY_MODEL) reads the titles of
the user's library and one hit, and gives the probability that the PhD
student who keeps this library wants to read the hit. The library is the
papers that the user added (db.library_titles); the sweep's own papers do
not count, so that the judgement does not feed on itself. One call for each
hit, so a score does not depend on the other hits of the day. Thinking is
off: about 0.65 s for each hit with Bonsai 2 27B, because llama-server
keeps the prompt of the library in its cache.

Tested against Fumi's labels of one week (71 hits, 24 relevant): AUC 0.87.
At 0.3 it kept 41 hits and missed 2 relevant ones. The interest statement of
each saved search scored worse with every reranker (AUC 0.59 to 0.83), because
the user labels by the library, not by the wording of a search.

The model server is the GPU tenant (ADR-0017). If it cannot answer (the GPU
is cleared for training, the server does not start), decide() raises and the
hits wait for the next sweep.
"""
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv

from .generation import generate
from .prompts import triage_prompt

load_dotenv()

log = logging.getLogger(__name__)

TRIAGE_MODEL = os.getenv("TRIAGE_MODEL") or None

TRIAGE_SCHEMA = {
    "probability": (float, ...),
}


def _judge(hit: dict[str, Any], library: str) -> Optional[float]:
    """None if the reply is not a probability."""
    try:
        probability = generate(triage_prompt(library, hit["title"], hit["abstract_text"]),
                               TRIAGE_MODEL, schema=TRIAGE_SCHEMA, thinking=False)["probability"]
    except ValueError:  # pydantic.ValidationError is a ValueError
        log.warning("triage: %s: the reply is not a probability", hit["ref"])
        return None
    return probability if 0 <= probability <= 1 else None


def decide(hits: list[dict[str, Any]], library: list[str]) -> list[Optional[float]]:
    """The probability that the student with this library (titles) wants to
    read each hit (a source record); None for a hit whose reply was not a
    probability. Raises if the model server cannot answer: the hits then
    wait for the next sweep."""
    if not library:
        raise ValueError("The library is empty: add papers before the sweep triages.")
    text = "\n".join(f"- {title}" for title in library)
    return [_judge(hit, text) for hit in hits]
