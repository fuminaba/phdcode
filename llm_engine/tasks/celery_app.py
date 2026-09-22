"""The Celery app instance -- broker/backend config and queue defaults.

Redis is used for both the broker (the job queue itself) and the result
backend (so a caller can await a task's return value -- see
docs/ARCHITECTURE.md's "Planned: Celery/Redis" section for why the result
backend matters here: it's what lets the main process, not the worker, be
the only thing that ever talks to Telegram).

Two named queues exist by convention, not by any config below: "chat" (the
default) and "literature". Nothing forces those two names except whatever
each task is decorated/dispatched with (@app.task(queue=...) or
apply_async(queue=...)) -- Celery creates a queue on first reference, no
upfront declaration needed on a Redis transport.

Chat no longer talks to Ollama directly as of this module -- every reply
now depends on a worker actually consuming the "chat" queue. Run one (from
the repo root, so `llm_engine` resolves as a package; needs Redis already
up, e.g. `docker compose up -d redis`):

    celery -A llm_engine.tasks worker --loglevel=info --concurrency=1 \\
        --pool=prefork -Q chat,literature

concurrency=1 and --pool=prefork aren't arbitrary: concurrency=1 is the
actual OOM-avoidance guarantee (exactly one GPU task ever running), and
prefork (not solo) is specifically what makes a stuck task killable later
(see docs/ARCHITECTURE.md's "Planned: Celery/Redis" section). This has to
be running *alongside* webhook_server.py, as its own separate, persistent
process -- not a one-off, and not something either process starts for you.
"""
import os

from celery import Celery
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

app = Celery("alira", broker=REDIS_URL, backend=REDIS_URL)

app.conf.update(
    task_default_queue="chat",
    # Without this, a worker grabs several jobs ahead of time by default --
    # which would silently break "stop taking chat jobs the instant the
    # go-ahead is given" (a job already sitting in the worker's local
    # buffer doesn't care about a later cancel_consumer call).
    worker_prefetch_multiplier=1,
    task_track_started=True,
)
