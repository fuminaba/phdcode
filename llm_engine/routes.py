"""Model routes: name -> endpoint, and whether it uses this machine's GPU.

generation.generate() gets its client here. embeddings.embed() gets only the
server root here, because it calls Ollama's native API (see embeddings.py).
So each server is set in one place.

A Route carries `local`: True if the model runs on this machine's GPU. The
GPU policy (ADR-0017) uses it to tell a local AGENT_BACKEND, which is a GPU
tenant, from a hosted one. No call takes a lock because of it: the model
server is the tenant and holds the lock. `local` defaults to True, because a
GPU tenant that phdcode does not know about is the worse mistake.

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
    # Does this model run on THIS machine's GPU?
    local: bool = True


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
    )


def client(route: Route):
    """An OpenAI-compatible client for this route.

    Built per call rather than cached: routes point at different hosts, and a
    client is cheap next to the request it carries.
    """
    from openai import OpenAI

    return OpenAI(base_url=route.endpoint, api_key=route.api_key)

