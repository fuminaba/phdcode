"""Shared helper for routing a GPU-touching call through the "chat"
Celery queue -- and, critically, for NOT routing it when doing so would
deadlock.

Why this exists: docs/ARCHITECTURE.md claims every GPU-touching call is
serialized through a single concurrency=1 queue, but only the tool loop's
call_model ever actually was. embeddings.embed() and
GenerativeService.generate_text() both talked to Ollama directly,
bypassing the queue entirely. That was harmless while Ollama was the only
backend -- it serialized internally (-np 1) and managed its own VRAM via
keep_alive, so unqueued callers just waited their turn cheaply. It stopped
being harmless the moment a second backend (llama-server, for models
Ollama can't load at all) started holding ~13.5GB indefinitely with no
keep_alive concept: the two unqueued paths were then left fighting over
whatever VRAM remained. Confirmed in production, not theorised -- a chat
reply was delayed 352 seconds because persist_turn's embed() call sat
behind a background conversation-archive summarization, both unqueued,
both scrabbling for the same leftover VRAM.

The deadlock this guards against: a Celery worker running at
--concurrency=1 has exactly one execution slot. run_ingestion occupies
that slot for the whole of ingest_and_index_document, which itself calls
embed() and generate_text() many times. If those dispatched to the same
queue and blocked on .get(), they would be waiting for a worker slot that
is already held by the very task waiting on them -- a permanent,
self-inflicted hang. So: when already inside a worker, call the task
function directly (it is just a Python function), and only dispatch when
running in the main process.
"""
import logging
from typing import Any

log = logging.getLogger(__name__)

# Generous: the queue is concurrency=1, so a call can legitimately wait
# out whatever is already running (a document ingestion is minutes) before
# its own work even starts. Matches _run_tool_loop's existing call_model
# timeout.
DEFAULT_TIMEOUT = 600


def in_celery_worker() -> bool:
    """True when this code is running inside a Celery worker process
    executing a task, False in the main process (webhook_server,
    live_viewer, an ad-hoc script).

    Checks current_task.request.id rather than just current_task: the
    task object exists as an importable module attribute everywhere, but
    only carries a request id while actually executing inside a worker.
    """
    try:
        from celery import current_task
        request = getattr(current_task, "request", None)
        return getattr(request, "id", None) is not None
    except Exception:
        # Celery not importable/configured at all -- treat as "not in a
        # worker" so the caller runs its work directly rather than failing.
        return False


def run_gpu_task(task, args: list, timeout: int = DEFAULT_TIMEOUT) -> Any:
    """Runs one GPU-touching Celery task, serialized through the "chat"
    queue when called from the main process, or inline when already
    inside a worker (see the module docstring for why inline is
    mandatory there, not merely an optimisation).

    Blocking either way. Callers are already off the event loop --
    generate_text/embed are sync functions invoked via asyncio.to_thread
    or from inside a worker thread -- so blocking here never stalls the
    bot's event loop, same as the direct HTTP call it replaces.
    """
    if in_celery_worker():
        return task(*args)
    return task.apply_async(args=args, queue="chat").get(timeout=timeout)
