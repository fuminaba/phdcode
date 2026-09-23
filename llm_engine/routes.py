"""Model routes: name -> endpoint, and whether it consumes this machine's GPU.

Extracted from tgi.GenerativeService.get_client so that callers do not depend
on the agent loop, which is being deleted (ADR-0002). `embeddings.embed()`
reached the model through that classmethod, which would have taken the
knowledge store down with tgi.py.

A Route carries `local`, which decides whether a call takes the GPU lock. A
hosted API consumes no VRAM here, so routing it through the lock would park
it behind a document ingestion for no reason -- head-of-line blocking wearing
the costume of resource protection. `local` defaults to True: taking the lock
unnecessarily costs latency, while missing it costs an out-of-memory failure,
so the safe default is the conservative one.

Configured by LLM_ROUTING in .env:

    LLM_ROUTING="{
        'qwen3:14b'   : {'host_endpoint': 'http://host:11434/v1', 'api_key': '-'},
        'bonsai2-27b' : {'host_endpoint': 'http://host:8080/v1',  'api_key': '-'},
        'gemini-3.7-flash': {'host_endpoint': 'https://...', 'api_key': '...',
                             'local': false}
    }"

Single quotes and stray newlines are tolerated for backward compatibility
with the existing file; strict JSON is tried first.
"""
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv(override=True)

log = logging.getLogger(__name__)

DEFAULT_SERVER_IP = os.getenv("DEFAULT_SERVER_IP")


@dataclass(frozen=True)
class Route:
    """One model endpoint and the policy for calling it."""
    name: str
    endpoint: str
    api_key: str
    # Does calling this consume THIS machine's VRAM? Decides the GPU lock.
    local: bool = True
    # Concurrent calls this endpoint tolerates. Local routes are 1 by
    # construction: one card, one model resident at a time.
    max_concurrency: int = 1


def _parse_routing(raw: Optional[str]) -> dict[str, Any]:
    """Parses LLM_ROUTING, preferring strict JSON.

    Falls back to the legacy shape (single-quoted keys, newlines inside the
    value) that .env already contains. The fallback strips whitespace
    wholesale, so it would corrupt a value that legitimately contains a
    space -- write strict JSON if you need one.
    """
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(
            raw.strip().replace("\n", "").replace(" ", "").replace("'", '"')
        )
    except json.JSONDecodeError as e:
        log.warning("Couldn't parse LLM_ROUTING (%s); using the default route only.", e)
        return {}


_ROUTING = _parse_routing(os.getenv("LLM_ROUTING"))


def resolve(model_name: str) -> Route:
    """Returns the Route for model_name.

    An unknown model falls back to DEFAULT_SERVER_IP, matching the previous
    behaviour of get_client -- and is treated as local, because the default
    server is this machine's Ollama.
    """
    entry = _ROUTING.get(model_name)
    if entry is None:
        if not DEFAULT_SERVER_IP:
            raise RuntimeError(
                f"No route for {model_name!r} and DEFAULT_SERVER_IP is unset."
            )
        return Route(name=model_name, endpoint=f"{DEFAULT_SERVER_IP}/v1", api_key="-")

    return Route(
        name=model_name,
        endpoint=entry["host_endpoint"],
        api_key=entry.get("api_key", "-"),
        local=bool(entry.get("local", True)),
        max_concurrency=int(entry.get("max_concurrency", 1)),
    )


def client(route: Route):
    """An OpenAI-compatible client for this route.

    Built per call rather than cached: routes point at different hosts, and a
    client is cheap next to the request it carries.
    """
    from openai import OpenAI

    return OpenAI(base_url=route.endpoint, api_key=route.api_key)


def known_models() -> list[str]:
    """Model names LLM_ROUTING declares. Does not include the default-route
    fallback, which accepts any name."""
    return sorted(_ROUTING)
