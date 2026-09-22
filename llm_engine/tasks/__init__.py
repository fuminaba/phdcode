try:
    from .celery_app import app
except ImportError:
    from celery_app import app

from . import diagnostics  # noqa: F401  (import registers its @app.task functions)
from . import ingestion_tasks  # noqa: F401
from . import chat_tasks  # noqa: F401

__all__ = ["app"]
