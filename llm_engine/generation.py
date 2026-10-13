import json
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic import create_model

try:
    from .prompts import json_schema_instruction
    from .routes import client, resolve
except ImportError:
    from prompts import json_schema_instruction
    from routes import client, resolve

load_dotenv()

log = logging.getLogger(__name__)

# The route for summaries (ADR-0015, ADR-0017). The default is the model
# that drives the harness, so that a local setup loads one model only.
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL") or os.getenv("AGENT_BACKEND")


def generate(
        prompt: str,
        model_name: Optional[str] = None,
        schema: Optional[dict[str, Any]] = None,
        thinking: bool = True,
) -> str | dict[str, Any]:
    """One chat completion through the route of model_name.

    With schema, the reply must be JSON that matches it, and the parsed dict
    is returned. schema has the shape of pydantic.create_model's fields:
    {"title": (str, ...), "authors": (list[str], ...)}. A reply that does not
    validate raises pydantic.ValidationError.

    Replaces tgi.GenerativeService.generate_text, which reached the model
    through the Celery queue (ADR-0015). It takes no GPU lock: the model
    server is the GPU tenant and holds the lock for its full life (ADR-0017).
    If the server is stopped, the call raises openai.APIConnectionError.

    thinking=False asks the chat template for no reasoning (enable_thinking;
    Qwen-style templates such as Bonsai's read it, others ignore it) and
    uses temperature 0: a short judgement that repeats (triage).
    """
    model_name = model_name or SUMMARY_MODEL
    if not model_name:
        raise RuntimeError("Set SUMMARY_MODEL or AGENT_BACKEND in .env.")
    route = resolve(model_name)
    messages = [{"role": "user", "content": prompt}]
    if schema is not None:
        # The model sees a JSON Schema: a Python repr of the fields made
        # some models reply with single quotes, which is not JSON.
        reply_model = create_model("Reply", **schema)
        instruction = json_schema_instruction(json.dumps(reply_model.model_json_schema()))
        messages.insert(0, {"role": "system", "content": instruction})

    options = {} if thinking else {
        "temperature": 0, "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    response = client(route).chat.completions.create(model=route.name, messages=messages, **options)
    # A reasoning model can put a <think> block before the answer.
    content = (response.choices[0].message.content or "").split("</think>")[-1].strip()

    if schema is None:
        return content
    # The JSON object, without a code fence or text around it.
    json_text = content[content.find("{"):content.rfind("}") + 1]
    return reply_model.model_validate_json(json_text).model_dump()
