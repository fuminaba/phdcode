"""Triage: the probability that a hit is relevant to an interest statement
(ADR-0015, item 7).

decide() uses a reranker: a decision model that reads one pair (interest
statement, title and abstract) and gives P("yes") from its logits, with no
generated text. Each pair is scored alone, so a score does not depend on the
other hits. The scores are not calibrated: the labelled set selects the
threshold.

The reranker is Qwen3-Reranker-4B, on the GPU only (gpu.short_job,
ADR-0017). On the hits of one week it removed each false positive of the
0.6B model (retinal, MR and ultrasound papers at p = 0.8 to 0.98 fell below
0.2), and it found 7 of 36 frontier-AI architecture papers instead of 1
(a test on 2026-10-12). If the GPU is not free (training,
llama-server), decide() raises and the hits wait for the next sweep: one
model for all judgements, so that the scores of two days compare. The 4B
model on the CPU would need 16 GB of RAM and about 20 s for each hit.

The LLM (TRIAGE_MODEL, default SUMMARY_MODEL) judges instead only when
TRIAGE_RERANKER is set to "".
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

TRIAGE_RERANKER = os.getenv("TRIAGE_RERANKER", "Qwen/Qwen3-Reranker-4B")
TRIAGE_MODEL = os.getenv("TRIAGE_MODEL") or None
# Measured for Qwen3-Reranker-4B in bf16 at batch 8: 9,871 MiB peak; 4 s to
# load, 72 ms for each hit.
TRIAGE_VRAM_MIB = 12288
_BATCH = 8
# The longest wait for another short job (a parse) that holds the GPU lock.
_LOCK_WAIT_S = 1800

# The input format of the Qwen3-Reranker model card.
_TASK = "Given a researcher's interest statement, judge whether the paper is relevant to it"
_PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and "
           "the Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n"
           "<|im_start|>user\n")
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def _rerank(interest: str, hits: list[dict[str, Any]]) -> list[float]:
    """Loads the reranker onto the GPU for this call, and frees it after:
    the GPU has other tenants between the saved searches of a sweep."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    texts = [f"{_PREFIX}<Instruct>: {_TASK}\n<Query>: {interest}\n<Document>: Title: {h['title']}\n"
             f"Abstract: {h['abstract_text'] or '(none)'}{_SUFFIX}" for h in hits]
    gpu_seconds = 5 + 0.08 * len(texts)
    with short_job(TRIAGE_VRAM_MIB, gpu_seconds, gpu_seconds + _LOCK_WAIT_S, "triage") as device:
        if device != "cuda":
            raise RuntimeError("the GPU is not free (training or llama-server); the hits wait for the next sweep")
        tokenizer = AutoTokenizer.from_pretrained(TRIAGE_RERANKER, padding_side="left")
        model = AutoModelForCausalLM.from_pretrained(
            TRIAGE_RERANKER, dtype=torch.bfloat16, device_map="cuda").eval()
        yes_no = [tokenizer.convert_tokens_to_ids(t) for t in ("no", "yes")]
        try:
            scores: list[float] = []
            for i in range(0, len(texts), _BATCH):
                inputs = tokenizer(texts[i:i + _BATCH], return_tensors="pt", padding=True,
                                   add_special_tokens=False).to("cuda")
                with torch.no_grad():
                    logits = model(**inputs).logits[:, -1, yes_no].float()
                scores += torch.softmax(logits, dim=1)[:, 1].tolist()
            return scores
        finally:
            del model
            torch.cuda.empty_cache()


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
    interest; None for a hit that the LLM could not judge. Raises if the
    reranker cannot run (the GPU is not free, the model does not load): the
    hits then wait for the next sweep."""
    if TRIAGE_RERANKER:
        return _rerank(interest, hits)
    return [_llm_decide(hit, interest) for hit in hits]
