import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic import create_model

try:
    from .gpu import gpu_lock
    from .prompts import json_schema_instruction
    from .routes import client, resolve
except ImportError:
    from gpu import gpu_lock
    from prompts import json_schema_instruction
    from routes import client, resolve

load_dotenv()

log = logging.getLogger(__name__)

GENERATION_MODEL = os.getenv("GENERATION_MODEL", "qwen3:14b")


def generate(
        prompt: str,
        model_name: Optional[str] = None,
        schema: Optional[dict[str, Any]] = None,
) -> str | dict[str, Any]:
    """One chat completion through the route of model_name.

    With schema, the reply must be JSON that matches it, and the parsed dict
    is returned. schema has the shape of pydantic.create_model's fields:
    {"title": (str, ...), "authors": (list[str], ...)}. A reply that does not
    validate raises pydantic.ValidationError.

    Replaces tgi.GenerativeService.generate_text, which reached the model
    through the Celery queue (ADR-0015). Like embed(), it holds the GPU lock
    for local routes only.
    """
    route = resolve(model_name or GENERATION_MODEL)
    messages = [{"role": "user", "content": prompt}]
    if schema is not None:
        messages.insert(0, {"role": "system", "content": json_schema_instruction(schema)})

    with gpu_lock(enabled=route.local, label=f"generate:{route.name}"):
        response = client(route).chat.completions.create(model=route.name, messages=messages)
    content = response.choices[0].message.content

    if schema is None:
        return content
    content = content.strip().removeprefix("```json").removesuffix("```")
    return create_model("Reply", **schema).model_validate_json(content).model_dump()
