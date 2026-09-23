import logging
import os
from typing import Optional

from dotenv import load_dotenv

try:
    from .gpu import gpu_lock
    from .routes import client, resolve
except ImportError:
    from gpu import gpu_lock
    from routes import client, resolve

load_dotenv()

log = logging.getLogger(__name__)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")


def embed(text: str, model_name: Optional[str] = None) -> list[float]:
    """Embeds text via the OpenAI-compatible /v1/embeddings endpoint (Ollama
    serving EMBEDDING_MODEL locally by default). The returned dimension must
    match EMBEDDING_DIM in .env and the `embedding` column in schema.sql.

    Holds the GPU lock for local routes (see gpu.py). An embedding model is
    small -- nomic-embed-text is ~0.3GB -- but on a card already holding a
    13.5GB chat model that last 0.3GB is exactly what there is not room for.
    Confirmed the hard way once: an unserialized embed() sat 352 seconds
    behind a background summarization. The lock is re-entrant, so calling
    this from inside a library that already holds it (an ingestion embedding
    every chunk, say) runs inline instead of waiting on the slot it holds.

    Previously reached the model through tgi.GenerativeService.get_client and
    a Celery queue. Both are being deleted (ADR-0002, ADR-0008), so this now
    resolves its own route and takes the lock directly -- which is why the
    deletion of tgi.py could not happen before this change.
    """
    route = resolve(model_name or EMBEDDING_MODEL)
    with gpu_lock(enabled=route.local, label=f"embed:{route.name}"):
        response = client(route).embeddings.create(model=route.name, input=text)
    return response.data[0].embedding
