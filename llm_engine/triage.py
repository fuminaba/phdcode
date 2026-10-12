"""Triage: the probability that a hit is relevant to an interest statement
(ADR-0015, item 7).

decide() uses a reranker: a decision model that reads one pair (interest
statement, title and abstract) and gives P("yes") from its logits, with no
generated text. Each pair is scored alone, so a score does not depend on the
other hits. The scores are not calibrated: the labelled set selects the
threshold.

The reranker runs on the GPU if that is faster, waits included
(gpu.short_job, ADR-0017); else, or if CUDA runs out of memory, on the CPU
with half the cores. On the GPU a hit takes about 16 ms, on the CPU about 3 s.

The fallback is an LLM with structured output (TRIAGE_MODEL, default
SUMMARY_MODEL): set TRIAGE_RERANKER to "" to use it, and decide() also uses
it if the reranker does not load.
"""
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv

from .generation import generate
from .gpu import short_job
from .prompts import triage_prompt
from .schemas import TRIAGE_SCHEMA

load_dotenv()

log = logging.getLogger(__name__)

TRIAGE_RERANKER = os.getenv("TRIAGE_RERANKER", "Qwen/Qwen3-Reranker-0.6B")
TRIAGE_MODEL = os.getenv("TRIAGE_MODEL") or None
# Measured for Qwen3-Reranker-0.6B in fp16 at batch 8: 3.0 GB peak.
TRIAGE_VRAM_MIB = 4096
_BATCH = {"cuda": 8, "cpu": 1}  # On the CPU, padding in a batch only adds work.

# The input format of the Qwen3-Reranker model card.
_TASK = "Given a researcher's interest statement, judge whether the paper is relevant to it"
_PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and "
           "the Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n"
           "<|im_start|>user\n")
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


class Reranker:
    """Loaded once on the CPU. Each score() call moves it to the GPU if that
    is faster (gpu.short_job), and back to the CPU after. The GPU copy is
    bf16, the precision of the checkpoint, so the round trip changes no
    weight."""

    def __init__(self, name: str):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        torch.set_num_threads(max(1, (os.cpu_count() or 2) // 2))
        self.tokenizer = AutoTokenizer.from_pretrained(name, padding_side="left")
        self.model = AutoModelForCausalLM.from_pretrained(name, dtype=torch.float32).eval()
        self.yes_no = [self.tokenizer.convert_tokens_to_ids(t) for t in ("no", "yes")]

    def score(self, interest: str, hits: list[dict[str, Any]]) -> list[float]:
        import torch
        texts = [f"{_PREFIX}<Instruct>: {_TASK}\n<Query>: {interest}\n<Document>: Title: {h['title']}\n"
                 f"Abstract: {h['abstract_text'] or '(none)'}{_SUFFIX}" for h in hits]
        # Measured: 16 ms for each hit on the GPU, plus about 3 s to move the
        # model and start CUDA; 3.1 s for each hit on the CPU.
        with short_job(TRIAGE_VRAM_MIB, 3 + 0.02 * len(texts), 3.1 * len(texts), "triage") as device:
            try:
                return self._score(texts, device)
            finally:
                if device == "cuda":
                    self.model.to("cpu", torch.float32)
                    torch.cuda.empty_cache()

    def _score(self, texts: list[str], device: str) -> list[float]:
        import torch
        scores: list[float] = []
        while len(scores) < len(texts):
            try:
                if device == "cuda" and self.model.device.type != "cuda":
                    self.model.to("cuda", torch.bfloat16)
                batch = texts[len(scores):len(scores) + _BATCH[device]]
                inputs = self.tokenizer(batch, return_tensors="pt", padding=True, add_special_tokens=False)
                with torch.no_grad():
                    logits = self.model(**inputs.to(device)).logits[:, -1, self.yes_no].float()
            except torch.cuda.OutOfMemoryError:
                log.warning("reranker: CUDA out of memory; the rest runs on the CPU")
                self.model.to("cpu", torch.float32)
                torch.cuda.empty_cache()
                device = "cpu"
                continue
            scores += torch.softmax(logits, dim=1)[:, 1].tolist()
        return scores


_reranker: Optional[Reranker] = None


def _llm_decide(hit: dict[str, Any], interest: str) -> Optional[float]:
    """None if the reply is not a probability."""
    try:
        probability = generate(triage_prompt(interest, hit["title"], hit["abstract_text"]),
                               TRIAGE_MODEL, schema=TRIAGE_SCHEMA)["probability"]
    except ValueError:  # pydantic.ValidationError is a ValueError
        log.warning("triage: %s: the reply is not a probability", hit["ref"])
        return None
    return probability if 0 <= probability <= 1 else None


def decide(hits: list[dict[str, Any]], interest: str) -> list[Optional[float]]:
    """The probability that each hit (a source record) is relevant to
    interest; None for a hit that the LLM fallback could not judge. Raises
    if no triage model is available (the hits then wait for the next sweep).
    The first call loads the reranker; the process keeps it."""
    global _reranker, TRIAGE_RERANKER
    if TRIAGE_RERANKER and _reranker is None:
        try:
            _reranker = Reranker(TRIAGE_RERANKER)
        except Exception:
            log.exception("reranker %s did not load; triage uses the LLM", TRIAGE_RERANKER)
            TRIAGE_RERANKER = ""  # no second attempt in this process
    if _reranker is not None:
        return _reranker.score(interest, hits)
    return [_llm_decide(hit, interest) for hit in hits]
