import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")


def embed(text: str, model_name: Optional[str] = None) -> list[float]:
    """Embeds text via the OpenAI-compatible /v1/embeddings endpoint (Ollama
    serving EMBEDDING_MODEL locally by default). The returned dimension must
    match EMBEDDING_DIM in .env / the `embedding` column in schema.sql.

    Goes through the "chat" Celery queue (tasks.dispatch.run_gpu_task)
    rather than calling Ollama directly, so it is serialized against
    every other GPU-touching call instead of competing with them -- see
    that module's docstring for the 352-second reply delay that motivated
    this. run_gpu_task runs the work inline when already inside a worker,
    so ingest_and_index_document (which embeds every chunk from inside
    its own task) doesn't deadlock waiting on the slot it already holds.

    Imports are function-local, not module-level: tasks.chat_tasks imports
    tgi, and tgi's package imports reach back here, so importing either at
    module scope reintroduces the circular import this module was
    previously structured to avoid.
    """
    model_name = model_name or EMBEDDING_MODEL
    try:
        from .tasks.chat_tasks import embed_text
        from .tasks.dispatch import run_gpu_task
    except ImportError:
        from tasks.chat_tasks import embed_text
        from tasks.dispatch import run_gpu_task

    return run_gpu_task(embed_text, [model_name, text])
