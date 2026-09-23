"""Smoke test for MedGemma 1.5 4B (google/medgemma-1.5-4b-it) served
unquantized in its native bfloat16 via transformers.

Deliberately NOT Ollama/GGUF: this model is evaluated for medical imaging,
and a GGUF conversion would replace the official SigLIP preprocessing path
with a reimplementation. Preprocessing drift is a silent failure mode --
the model still answers, just from a differently-resampled image -- which
is the wrong risk to accept on a model whose whole point is fidelity.
transformers uses the reference processor, and the process releasing all
VRAM on exit matches this project's existing unload discipline.

Reports VRAM and tokens/sec for both a text-only and an image+text turn,
so the numbers can go straight into docs/MODEL_BENCHMARK.md.
"""
import time
import torch
from PIL import Image, ImageDraw
from transformers import AutoProcessor, Gemma3ForConditionalGeneration

MODEL_ID = "google/medgemma-1.5-4b-it"


def vram_mib() -> int:
    return torch.cuda.memory_allocated() // (1024 * 1024)


def synthetic_image() -> Image.Image:
    """A plain synthetic image, not a clinical one.

    This test verifies that the image pathway is wired correctly --
    processor, vision tower, token interleaving -- not that the model is
    clinically accurate. Using a generated shape keeps that distinction
    honest: any medical-sounding answer here would be about an image with
    no medical content, so the only thing being asserted is plumbing.
    Real evaluation needs real data with real provenance.
    """
    img = Image.new("L", (896, 896), color=40)
    d = ImageDraw.Draw(img)
    d.ellipse((300, 250, 600, 650), fill=180)
    d.rectangle((120, 120, 220, 220), fill=230)
    return img.convert("RGB")


def run(model, processor, messages, label, max_new_tokens=128):
    inputs = processor.apply_chat_template(
        messages, add_generation_prompt=True, tokenize=True,
        return_dict=True, return_tensors="pt",
    ).to(model.device, dtype=torch.bfloat16)
    prompt_len = inputs["input_ids"].shape[-1]

    torch.cuda.synchronize()
    t0 = time.perf_counter()
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - t0

    new_tokens = out.shape[-1] - prompt_len
    text = processor.decode(out[0][prompt_len:], skip_special_tokens=True)
    print(f"\n--- {label} ---")
    print(f"prompt_tokens={prompt_len}  new_tokens={new_tokens}  "
          f"{elapsed:.2f}s  {new_tokens / elapsed:.1f} tok/s")
    print(text.strip()[:600])


def main() -> None:
    print(f"torch {torch.__version__} | {torch.cuda.get_device_name(0)}")
    t0 = time.perf_counter()
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = Gemma3ForConditionalGeneration.from_pretrained(
        MODEL_ID, dtype=torch.bfloat16, device_map="cuda:0",
    ).eval()
    print(f"loaded in {time.perf_counter() - t0:.1f}s | "
          f"weights {vram_mib()} MiB | dtype {model.dtype}")

    run(model, processor, [{
        "role": "user",
        "content": [{"type": "text", "text":
                     "In two sentences, what is a pathology whole-slide image?"}],
    }], "text-only")

    run(model, processor, [{
        "role": "user",
        "content": [
            {"type": "image", "image": synthetic_image()},
            {"type": "text", "text":
             "Describe the shapes and their positions in this image."},
        ],
    }], "image+text")

    print(f"\npeak VRAM {torch.cuda.max_memory_allocated() // (1024 * 1024)} MiB "
          f"of {torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)} MiB")


if __name__ == "__main__":
    main()
