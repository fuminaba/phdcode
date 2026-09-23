"""MedGemma image+text inference, run locally in native bfloat16.

Deliberately NOT served through Ollama/GGUF like the chat models are.
This model is used to look at medical images, and a GGUF conversion
replaces the official SigLIP preprocessing with a reimplementation --
drift there is silent (the model still answers confidently, just from a
differently-resampled image), which is the wrong failure mode for the one
model chosen specifically for fidelity. transformers runs the reference
path, and bfloat16 IS this checkpoint's native dtype (config.json's
`dtype`), so "unquantized" here means no conversion at all rather than a
dequantization.

`use_fast=False` is pinned for the same reason, not left to the default:
transformers warns that this checkpoint ships a slow image processor and
that switching to the fast one "will result in minor differences in
outputs". Minor output differences from a library upgrade are exactly
what must not happen silently between one evaluation run and the next.

MedGemma cannot call tools -- its chat_template.jinja handles only
system/user/assistant, with no tool role and no tool_call construct
anywhere in it (checked directly, 1,532 bytes). So this module is written
to be *wrapped* as a tool by a tool-capable orchestrator, never to drive
an agent loop itself. `analyze_image` is the tool-shaped entry point;
`VisionSession` is for interactive multi-turn questioning of one image.

GPU serialization: these calls are NOT currently routed through the
"chat" Celery queue that every other GPU-touching call in this project
uses (see tasks/dispatch.py). That is safe only while this runs
standalone. Weights are 8.2GB resident, so before registering
analyze_image as an ALiRA2 tool it must go through run_gpu_task or the
adapter-level semaphore -- otherwise it competes with chat generation and
document extraction for the same card, which is the contention that cost
a 352-second reply delay once already.
"""
import logging
import os
import time
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

MEDGEMMA_MODEL = os.getenv("MEDGEMMA_MODEL", "google/medgemma-1.5-4b-it")

# Loaded lazily and kept resident: the weights take ~6s to load and 8.2GB
# of VRAM, so reloading per call would dominate the ~4s inference time.
# unload() exists for the caller that needs the card back -- same
# courtesy-eviction role tgi.unload_all_models plays for the chat
# backends, and needed here for the same reason: nothing else fits
# alongside 8.2GB on a 16.3GB card except a small embedding model.
_PROCESSOR = None
_MODEL = None
_LOADED_ID: Optional[str] = None


def load(model_id: Optional[str] = None):
    """Loads (or returns the already-loaded) processor and model.

    Idempotent for the same model_id; loading a different one evicts the
    current weights first rather than holding both, which would not fit.
    """
    global _PROCESSOR, _MODEL, _LOADED_ID
    model_id = model_id or MEDGEMMA_MODEL

    if _MODEL is not None and _LOADED_ID == model_id:
        return _PROCESSOR, _MODEL
    if _MODEL is not None:
        unload()

    import torch
    from transformers import AutoProcessor, Gemma3ForConditionalGeneration

    t0 = time.perf_counter()
    # use_fast=False: see module docstring -- pinned, not defaulted.
    _PROCESSOR = AutoProcessor.from_pretrained(model_id, use_fast=False)
    _MODEL = Gemma3ForConditionalGeneration.from_pretrained(
        model_id, dtype=torch.bfloat16, device_map="cuda:0",
    ).eval()
    _LOADED_ID = model_id
    log.info(
        "Loaded %s in %.1fs (%d MiB)", model_id, time.perf_counter() - t0,
        torch.cuda.memory_allocated() // (1024 * 1024),
    )
    return _PROCESSOR, _MODEL


def unload() -> None:
    """Frees the weights and empties the CUDA cache. Best-effort and safe
    to call when nothing is loaded."""
    global _PROCESSOR, _MODEL, _LOADED_ID
    if _MODEL is None:
        return
    _PROCESSOR = _MODEL = _LOADED_ID = None
    try:
        import gc

        import torch
        gc.collect()
        torch.cuda.empty_cache()
    except Exception as e:  # never let a cleanup failure propagate
        log.warning("CUDA cache clear failed: %s", e)


def _open(image_path: str):
    """Loads one image as RGB. Raises FileNotFoundError with the path
    rather than letting PIL's less specific error surface."""
    from PIL import Image

    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"No such image: {image_path}")
    return Image.open(image_path).convert("RGB")


def _generate(messages, max_new_tokens: int, model_id: Optional[str]) -> dict:
    """Shared generate path for both entry points. Returns the decoded
    text plus timing, so callers can report tok/s without re-measuring."""
    import torch

    processor, model = load(model_id)
    inputs = processor.apply_chat_template(
        messages, add_generation_prompt=True, tokenize=True,
        return_dict=True, return_tensors="pt",
    ).to(model.device, dtype=torch.bfloat16)
    prompt_len = inputs["input_ids"].shape[-1]

    torch.cuda.synchronize()
    t0 = time.perf_counter()
    with torch.inference_mode():
        out = model.generate(
            **inputs, max_new_tokens=max_new_tokens, do_sample=False,
        )
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - t0

    text = processor.decode(out[0][prompt_len:], skip_special_tokens=True)
    # The slow tokenizer pinned above does not flag <end_of_turn> as a
    # special token, so skip_special_tokens leaves it in the decoded
    # string (confirmed: it appeared verbatim at the end of real output).
    # Strip whatever the generation config actually declares as a
    # terminator rather than a hardcoded literal, so this stays correct
    # if the checkpoint's markers ever change.
    eos = model.generation_config.eos_token_id
    for tid in (eos if isinstance(eos, (list, tuple)) else [eos]):
        if tid is None:
            continue
        marker = processor.tokenizer.decode([tid], skip_special_tokens=False)
        if marker:
            text = text.replace(marker, "")
    new_tokens = int(out.shape[-1] - prompt_len)
    return {
        "text": text.strip(),
        "prompt_tokens": int(prompt_len),
        "new_tokens": new_tokens,
        "seconds": elapsed,
        "tokens_per_second": new_tokens / elapsed if elapsed else 0.0,
    }


def analyze_image(
        image_path: str,
        question: str,
        system_prompt: Optional[str] = None,
        max_new_tokens: int = 512,
        model_id: Optional[str] = None,
) -> str:
    """Asks MedGemma one question about one image and returns its answer.

    Single-turn and stateless by design -- this is the shape a tool-calling
    orchestrator wants, since it can be described by its signature alone
    and carries no session the caller has to manage. For asking several
    questions about the same image without re-encoding it each time, use
    VisionSession instead.

    Args:
        image_path: path to the image (any format PIL reads).
        question: what to ask about it.
        system_prompt: optional role/context framing, e.g. naming the
            specialty. Goes in as a system turn, which this chat template
            folds into the first user message.
        max_new_tokens: generation cap. 512 fits a detailed description;
            raise it for long structured reports.
        model_id: override MEDGEMMA_MODEL for a one-off comparison.

    Returns:
        The model's answer as plain text.

    Raises:
        FileNotFoundError: if image_path doesn't exist.
    """
    messages = []
    if system_prompt:
        messages.append({
            "role": "system",
            "content": [{"type": "text", "text": system_prompt}],
        })
    messages.append({
        "role": "user",
        "content": [
            {"type": "image", "image": _open(image_path)},
            {"type": "text", "text": question},
        ],
    })
    return _generate(messages, max_new_tokens, model_id)["text"]


class VisionSession:
    """Multi-turn questioning of a single image.

    Keeps the image in the first user turn and appends text-only turns
    after it, which is what this chat template expects -- re-attaching the
    image to every turn would re-encode it (256 image tokens per turn) for
    no benefit, and the model already has it in context.

    Not thread-safe, and holds a reference to the decoded image for the
    session's lifetime: it is meant for one interactive conversation, not
    as a shared server object.
    """

    def __init__(
            self,
            image_path: Optional[str] = None,
            system_prompt: Optional[str] = None,
            model_id: Optional[str] = None,
    ):
        self.model_id = model_id
        self.system_prompt = system_prompt
        self.image_path = image_path
        self._image = _open(image_path) if image_path else None
        self.messages: list[dict] = []
        self.last_stats: dict = {}

    def set_image(self, image_path: str) -> None:
        """Switches to a new image and clears the history -- answers about
        the previous image would be actively misleading once the subject
        has changed, so they are not carried over."""
        self._image = _open(image_path)
        self.image_path = image_path
        self.messages = []

    def reset(self) -> None:
        """Clears the conversation, keeping the current image."""
        self.messages = []

    def ask(self, question: str, max_new_tokens: int = 512) -> str:
        """Asks one question in this session's context and returns the
        answer, recording timing in `last_stats`."""
        content: list[dict] = []
        # The image rides on the first user turn only -- see class docstring.
        if self._image is not None and not self.messages:
            content.append({"type": "image", "image": self._image})
        content.append({"type": "text", "text": question})
        self.messages.append({"role": "user", "content": content})

        prefix = []
        if self.system_prompt:
            prefix = [{
                "role": "system",
                "content": [{"type": "text", "text": self.system_prompt}],
            }]

        result = _generate(prefix + self.messages, max_new_tokens, self.model_id)
        self.last_stats = result
        self.messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": result["text"]}],
        })
        return result["text"]
