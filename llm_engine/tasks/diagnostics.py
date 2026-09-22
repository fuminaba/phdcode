try:
    from .celery_app import app
except ImportError:
    from celery_app import app


@app.task(name="alira.ping")
def ping() -> str:
    """Round-trip smoke test: dispatch this, and getting "pong" back
    confirms a worker is alive, consuming its queue, and the result backend
    works -- nothing more. Kept as a standing health check, not just a
    one-off infra proof."""
    return "pong"
