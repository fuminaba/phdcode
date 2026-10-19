import json
import os
import urllib.request

from dotenv import load_dotenv

from .routes import resolve

load_dotenv()

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")


def embed(texts: list[str]) -> list[list[float]]:
    """Embeds texts in one call to Ollama's native /api/embed, on the CPU.

    `num_gpu: 0` keeps the model off the GPU (ADR-0017): 57 ms for each text
    on this machine, so search and ingest never wait for the GPU tenant and
    take no GPU lock. The OpenAI-compatible /v1/embeddings has no field for
    this option, so the call goes to the native root of the route of
    EMBEDDING_MODEL (the route without "/v1"). The route stays the one
    setting for each server. The dimension must match EMBEDDING_DIM in .env.
    """
    root = resolve(EMBEDDING_MODEL).endpoint.removesuffix("/").removesuffix("/v1")
    body = json.dumps({"model": EMBEDDING_MODEL, "input": texts, "options": {"num_gpu": 0}}).encode()
    request = urllib.request.Request(
        f"{root}/api/embed", body, {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response)["embeddings"]
