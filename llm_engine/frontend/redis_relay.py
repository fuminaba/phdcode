import json

try:
    from .base import ChannelFeedback
except ImportError:
    from base import ChannelFeedback


class RedisRelayFeedback(ChannelFeedback):
    """Publishes start()/progress()/finish() as JSON events on a Redis
    pub/sub channel instead of surfacing them directly -- for code running
    inside a Celery worker, which never talks to Telegram (or any other
    channel) itself; see docs/ARCHITECTURE.md's "Planned: Celery/Redis"
    section for why that boundary exists. The main process subscribes to
    the same channel and relays each event to whichever real
    ChannelFeedback it already has for this operation (see
    tasks/ingestion_tasks.py's dispatch_ingestion).

    A synchronous redis client is deliberate, not an oversight: this runs
    inside a worker task that isn't doing anything else concurrently (the
    whole point of concurrency=1), so there's nothing to avoid blocking.
    """

    def __init__(self, channel: str, redis_url: str):
        import redis
        self._channel = channel
        self._client = redis.Redis.from_url(redis_url)

    async def start(self, label: str) -> None:
        self._publish({"type": "start", "label": label})

    async def progress(self, label: str, fraction: float) -> None:
        self._publish({"type": "progress", "label": label, "fraction": fraction})

    async def finish(self, label=None) -> None:
        self._publish({"type": "finish", "label": label})

    def _publish(self, event: dict) -> None:
        self._client.publish(self._channel, json.dumps(event))
