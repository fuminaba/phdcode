"""The one piece of the chat tool loop that actually touches the GPU -- the
raw model call -- as a Celery task. Everything else (tool dispatch,
current_user_id, the run_on_main_loop bridge, Telegram access, conversation
state) stays in the main process, unmodified; see docs/ARCHITECTURE.md's
"Planned: Celery/Redis" section for why chat and document ingestion get
different task boundaries.
"""
try:
    from .celery_app import app
except ImportError:
    from celery_app import app


@app.task(name="alira.call_model")
def call_model(model_name: str, messages: list[dict], tools: list[dict]) -> dict:
    """Runs exactly one model<->tool round trip and returns the raw
    OpenAI-style response as a plain dict (response.model_dump()) -- the
    caller (tgi._run_tool_loop) reads it via dict access rather than
    attribute access, since that's what survives the Celery/JSON round
    trip. Resolves its own client via GenerativeService.get_client rather
    than receiving one as an argument -- an OpenAI client instance isn't
    something that can cross a process boundary.
    """
    try:
        from ..tgi import GenerativeService, generative_kwargs
    except ImportError:
        from tgi import GenerativeService, generative_kwargs

    client = GenerativeService.get_client(model_name)
    try:
        response = client.chat.completions.create(
            model = model_name,
            messages = messages,
            tools = tools,
            **generative_kwargs,
        )
    except Exception as e:
        # Translate to a plain built-in exception before it leaves the
        # task -- openai's own exception types may not reconstruct cleanly
        # through Celery's JSON-based result serialization.
        raise RuntimeError(f"Model call failed: {e}") from e
    return response.model_dump()


@app.task(name="alira.embed_text")
def embed_text(model_name: str, text: str) -> list[float]:
    """Runs one embedding call and returns the raw vector.

    An embedding is a small model (nomic-embed-text is ~0.3GB) but it is
    still a GPU-touching call, and on a card already holding a 13.5GB
    chat model that last 0.3GB is exactly what there isn't room for. It
    belongs on the same concurrency=1 queue as every other GPU call for
    the same reason they all do -- confirmed the hard way when an
    unqueued embed() sat 352 seconds behind a background summarization.

    Dispatched via tasks.dispatch.run_gpu_task, never directly, so that
    ingest_and_index_document -- which already runs inside a worker and
    embeds every chunk -- calls it inline instead of deadlocking on its
    own occupied slot.
    """
    try:
        from ..tgi import GenerativeService
    except ImportError:
        from tgi import GenerativeService

    client = GenerativeService.get_client(model_name)
    try:
        response = client.embeddings.create(model=model_name, input=text)
    except Exception as e:
        # Same translation-to-builtin reasoning as call_model above:
        # openai's own exception types may not survive Celery's JSON
        # result serialization.
        raise RuntimeError(f"Embedding call failed: {e}") from e
    return response.data[0].embedding


@app.task(name="alira.count_prompt_tokens")
def count_prompt_tokens(model_name: str, text: str) -> int:
    """Returns the real tokenizer count for text on model_name -- a
    separate task from call_model (not a mode of it) since this needs a
    different generative_kwargs (max_tokens=1, not the normal chat
    settings) and returns a bare int, not the full response shape
    _run_tool_loop expects. Confirmed directly: this Ollama build (0.32.15)
    has no dedicated tokenize endpoint (/api/tokenize and /v1/tokenize
    both 404), but its OpenAI-compat layer reports the true prompt token
    count in usage.prompt_tokens regardless of how much gets generated
    after it -- so max_tokens=1 gets a real count almost for free (~0.2s
    measured, vs. tens of seconds for a normal reply) rather than an
    estimate. Still a real prefill pass, though, so this goes through the
    same "chat" queue/worker as every other GPU-touching call, not
    called directly -- concurrency=1 serialization applies here too, just
    with a much smaller decode cost than a normal turn.
    """
    try:
        from ..tgi import GenerativeService
    except ImportError:
        from tgi import GenerativeService

    client = GenerativeService.get_client(model_name)
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": text}],
            max_tokens=1,
            temperature=0.0,
        )
    except Exception as e:
        raise RuntimeError(f"Token count call failed: {e}") from e
    return response.usage.prompt_tokens
