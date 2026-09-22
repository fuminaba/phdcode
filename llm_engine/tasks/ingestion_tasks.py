"""Document ingestion as a Celery task -- the whole pipeline, not just the
GPU-touching calls inside it (see docs/ARCHITECTURE.md's "Planned:
Celery/Redis" section for why ingestion and chat get different task
boundaries). run_ingestion is the worker-side task; dispatch_ingestion is
the main-process-side counterpart callers actually use -- it dispatches,
relays the worker's progress back through a real ChannelFeedback (the
worker never talks to Telegram directly), and returns exactly what
ingest_and_index_document itself returns.
"""
import asyncio
import base64
import json
import uuid
from typing import Optional

try:
    from .celery_app import app, REDIS_URL
except ImportError:
    from celery_app import app, REDIS_URL


@app.task(name="alira.run_ingestion")
def run_ingestion(
        user_id: str,
        conversation_id: Optional[int],
        filename: str,
        content_b64: str,
        model_name: Optional[str],
        job_channel: str,
) -> tuple[int, str]:
    return asyncio.run(
        _run_ingestion_task(user_id, conversation_id, filename, content_b64, model_name, job_channel)
    )


async def _run_ingestion_task(
        user_id: str,
        conversation_id: Optional[int],
        filename: str,
        content_b64: str,
        model_name: Optional[str],
        job_channel: str,
) -> tuple[int, str]:
    try:
        from ..frontend.redis_relay import RedisRelayFeedback
        from ..memory import ingest_and_index_document
    except ImportError:
        from frontend.redis_relay import RedisRelayFeedback
        from memory import ingest_and_index_document

    content = base64.b64decode(content_b64)
    feedback = RedisRelayFeedback(job_channel, REDIS_URL)
    return await ingest_and_index_document(
        user_id, conversation_id, filename, content, model_name=model_name, feedback=feedback,
    )


async def dispatch_ingestion(
        user_id: str,
        conversation_id: Optional[int],
        filename: str,
        content: bytes,
        model_name: Optional[str] = None,
        feedback=None,
        queue: str = "chat",
) -> tuple[int, str]:
    """Dispatches run_ingestion and waits for its result, relaying the
    worker's progress events back through feedback in the meantime.

    queue="chat" (default) for a live chat-attached upload -- it should
    never wait behind a literature-processing batch. queue="literature"
    for a pull_pdf-triggered one, where waiting its turn is the point.

    content travels as base64 (not raw bytes) since Celery's JSON
    serializer can't carry bytes directly, and base64-through-Redis (vs. a
    shared staging path on disk) keeps this compatible with running the
    literature/ingestion worker on a different machine entirely later --
    see "Multi-machine scaling" in docs/ARCHITECTURE.md.
    """
    try:
        from ..frontend.base import ChannelFeedback
    except ImportError:
        from frontend.base import ChannelFeedback

    feedback = feedback or ChannelFeedback()
    job_channel = f"ingestion:{uuid.uuid4().hex}"
    content_b64 = base64.b64encode(content).decode("ascii")

    import redis.asyncio as aioredis
    client = aioredis.from_url(REDIS_URL)
    pubsub = client.pubsub()
    # Subscribe *before* dispatching -- a Redis pub/sub message published
    # before anything has subscribed is simply lost, and the task could
    # start publishing progress within milliseconds of being dispatched.
    await pubsub.subscribe(job_channel)

    async_result = run_ingestion.apply_async(
        args=[user_id, conversation_id, filename, content_b64, model_name, job_channel],
        queue=queue,
    )
    relay = asyncio.create_task(_relay_progress(pubsub, feedback))
    try:
        document_id, note = await asyncio.to_thread(async_result.get)
    finally:
        relay.cancel()
        try:
            await relay
        except asyncio.CancelledError:
            pass
        await pubsub.unsubscribe(job_channel)
        await client.aclose()
    return document_id, note


async def _relay_progress(pubsub, feedback) -> None:
    """Forwards each event published on pubsub's channel to feedback --
    runs concurrently with dispatch_ingestion's wait for the task's actual
    result, cancelled the moment that result arrives."""
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        event = json.loads(message["data"])
        if event["type"] == "start":
            await feedback.start(event["label"])
        elif event["type"] == "progress":
            await feedback.progress(event["label"], event["fraction"])
        elif event["type"] == "finish":
            await feedback.finish(event.get("label"))
