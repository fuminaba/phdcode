"""backend/litup/services/ai_engine_v2/tgi.py

The text response generation interface.
Communicates with some server (currently set to Ollama) to get output response.

"""

import os
import json
import logging
import time
from typing import Optional, Dict, Any
from uuid import UUID

import requests
from dotenv import load_dotenv

from openai import OpenAI
from openai import APIError

from celery.exceptions import TimeoutError as CeleryTimeoutError

from pydantic import BaseModel, create_model, TypeAdapter

try:
    from .tooling import TOOLS, TOOL_FUNCTIONS
    from .tooling.control_tools import CONTINUE_RESEARCH_TOOL
    from .tooling.discovery import DISCOVER_TOOLS_NAME, TOOL_CATEGORIES
    from .context import current_user_id, run_on_main_loop
    from .frontend.base import ChannelFeedback
    from .schemas import JUDGE_SCHEMA
    from .tasks.chat_tasks import call_model
    from .tasks.dispatch import run_gpu_task
    from .prompts import (
        DOCUMENT_SEARCH_GUIDANCE_PROMPT,
        LITERATURE_RESEARCH_PROMPT,
        MEMORY_SEARCH_PRIORITY_PROMPT,
        TOOL_CACHE_PRIORITY_PROMPT,
        TOOL_REASONING_PROMPT,
        json_schema_instruction,
        judge_prompt,
        eval_retry_feedback,
        tool_results_digest_prompt,
    )
except ImportError:
    from tooling import TOOLS, TOOL_FUNCTIONS
    from tooling.control_tools import CONTINUE_RESEARCH_TOOL
    from tooling.discovery import DISCOVER_TOOLS_NAME, TOOL_CATEGORIES
    from context import current_user_id, run_on_main_loop
    from frontend.base import ChannelFeedback
    from schemas import JUDGE_SCHEMA
    from tasks.chat_tasks import call_model
    from tasks.dispatch import run_gpu_task
    from prompts import (
        DOCUMENT_SEARCH_GUIDANCE_PROMPT,
        LITERATURE_RESEARCH_PROMPT,
        MEMORY_SEARCH_PRIORITY_PROMPT,
        TOOL_CACHE_PRIORITY_PROMPT,
        TOOL_REASONING_PROMPT,
        json_schema_instruction,
        judge_prompt,
        eval_retry_feedback,
        tool_results_digest_prompt,
    )

load_dotenv(override = True)  # Load environment variables from .env file

_CONTINUE_RESEARCH_STEP_INCREMENT = 5

log = logging.getLogger(__name__)

generative_kwargs = {
    "temperature" : 0.0,
    "top_p" : 0.1,
    "stream" : False,
}


class ToolLoopError(RuntimeError):
    """Raised when _run_tool_loop can't reach a final answer (an API
    failure mid-loop, or exceeding max_steps). Carries whatever partial
    state exists at the point of failure -- tokens used and partial output
    text if any -- so callers can persist/dump the attempt instead of
    losing it entirely when this propagates out of generate_with_tools.
    No longer carries a trace: turn_actions (see memory.db.write_turn_action)
    already has a durable, live record of every llm_call/tool_call made
    before the failure, tagged by turn_id -- a strictly better version of
    what an in-memory trace on the exception could offer, since it
    survives even a hard process crash, not just a caught exception."""

    def __init__(
            self,
            message: str,
            tokens_used: int,
            partial_output: Optional[str] = None,
    ):
        super().__init__(message)
        self.tokens_used = tokens_used
        self.partial_output = partial_output


def _log_action(
        turn_id: Optional[UUID],
        conversation_id: Optional[int],
        action_type: str,
        payload: dict[str, Any],
) -> None:
    """Best-effort live-log write for one action in the chat tool loop --
    see memory.db.write_turn_action (which is itself already best-effort
    about the DB write specifically). This wrapper additionally guards
    against run_on_main_loop itself failing (e.g. set_main_loop was never
    called) -- a live-log write going wrong should never break an
    otherwise-working chat turn. No-ops if turn_id wasn't given (e.g. a
    direct generate_with_tools call outside pipeline.handle_incoming_message,
    which is what actually mints one)."""
    if turn_id is None:
        return
    try:
        from .memory.db import write_turn_action
    except ImportError:
        from memory.db import write_turn_action
    try:
        run_on_main_loop(
            write_turn_action(turn_id, conversation_id, current_user_id.get(), action_type, payload)
        )
    except Exception as e:
        log.warning(f"Couldn't log turn_action ({action_type}): {e}")


MODEL_ROUTING = (
    os.getenv("LLM_ROUTING")
        .strip()
        .replace("\n", "")
        .replace(" ", "")
        .replace("'", '"')
)
DEFAULT_SERVER_IP = os.getenv("DEFAULT_SERVER_IP")
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL")
# Native (no /v1) root of the llama-server router -- the second model
# backend, serving models Ollama structurally can't load. Only
# _unload_llama_server_models uses it; ordinary generation goes through
# LLM_ROUTING's /v1 endpoint like every other model. Optional: unset
# just means "no llama-server here", and the unload sweep skips it.
LLAMA_SERVER_URL = os.getenv("LLAMA_SERVER_URL")

class GenerativeService:
    """
    Service to generate text with pre-defined routines.
    Returns dict output with keys 'output' and 'tokens_used'.
    {
        'output': ... ,
        'tokens_used': ... ,
    }
    """
    _client = None
    model_routing = {}
    adapter = TypeAdapter(Dict[str, Any])
    try:
        model_routing = adapter.validate_json(MODEL_ROUTING)
    except Exception as e:
        print(e)
        log.warning(
            "Couldn't parse model routing dict. Falling back to local hosted."
        )

    @classmethod
    def get_client(cls, model_name: str = "qwen3:14b") -> OpenAI:
        """
        Thread-safe runtime lookup. Ensures the environment variable is read 
        only when the client is actually needed, not when the file is imported.
        """
        model_server = GenerativeService.model_routing.get(model_name, None)
        if model_server is None:
            _server_ip = f"{DEFAULT_SERVER_IP}/v1"
            _api_key = "-"
        else:
            _server_ip = model_server["host_endpoint"]
            _api_key = model_server["api_key"]

        # Change the client each time along with the model_name as different
        # models are hosted on different servers.
        cls._client = OpenAI(base_url = _server_ip, api_key = _api_key)

        return cls._client

    @classmethod
    def unload_all_models(cls, wait_timeout: float = 5.0) -> None:
        """Frees GPU VRAM across EVERY model backend this deployment can
        route to, not just Ollama -- called right before another
        GPU-heavy process (marker-pdf extraction) needs the card.

        Two backends, two completely different unload mechanisms, so two
        helpers: Ollama wants /api/generate with keep_alive=0, while
        llama-server wants POST /models/unload. Both are called
        unconditionally and each swallows its own failures -- a backend
        that isn't running (or isn't configured) must never stop the
        other one from being swept, which is exactly what the old
        single-backend version did with its early `return` on a failed
        /api/ps query.

        Adding a backend here is one more `cls._unload_*` call plus its
        helper; callers never change.
        """
        cls._unload_ollama_models(wait_timeout)
        cls._unload_llama_server_models(wait_timeout)

    @classmethod
    def _unload_ollama_models(cls, wait_timeout: float = 5.0) -> None:
        """Frees GPU VRAM immediately by telling Ollama to unload every
        currently-loaded model (keep_alive=0), instead of waiting out its
        default 5-minute idle timeout. Meant to be called right before
        another GPU-heavy process (e.g. marker-pdf extraction) needs the
        card -- on hardware where Ollama and that process share one GPU,
        a resident model can leave too little VRAM for the other process
        to even load its own weights, independent of any batch-size tuning.

        Uses Ollama's native /api/generate directly rather than the
        OpenAI-compatible endpoint the rest of this class uses: passing
        keep_alive via the OpenAI SDK's extra_body is silently ignored by
        Ollama (confirmed: https://github.com/ollama/ollama/issues/11458),
        so this is the only path that actually works.

        Best-effort: swallows request failures rather than raising, since
        callers use this as a courtesy VRAM-freeing step, not a hard
        dependency -- a model that fails to unload just reloads on the
        caller's next real request either way, same as if this was never
        called. Polls /api/ps for up to wait_timeout seconds so the
        caller's next GPU-heavy step doesn't race the actual eviction.
        """
        try:
            response = requests.get(f"{OLLAMA_API_URL}/api/ps", timeout=5)
            response.raise_for_status()
            models = response.json().get("models", [])

        except Exception as e:
            log.warning(f"Couldn't query Ollama's loaded models: {e}")

            return

        if not models:
            return

        for model in models:
            name = model.get("model")
            if not name:
                continue

            try:
                requests.post(
                    f"{OLLAMA_API_URL}/api/generate",
                    json={"model": name, "keep_alive": 0},
                    timeout=10,
                )

            except Exception as e:
                log.warning(f"Couldn't unload Ollama model {name!r}: {e}")

        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            try:
                still_loaded = (
                    requests.get(f"{OLLAMA_API_URL}/api/ps", timeout=3)
                        .json()
                        .get("models", [])
                )
            except Exception:
                return
            if not still_loaded:
                return
            time.sleep(0.3)

    @classmethod
    def _unload_llama_server_models(cls, wait_timeout: float = 5.0) -> None:
        """Same job as _unload_ollama_models, for models served by the
        PrismML llama.cpp fork's llama-server instead of by Ollama (see
        .env's LLAMA_SERVER_URL). Needed because Ternary Bonsai 2 27B's
        ternary GGML tensor types can't be loaded by Ollama at all, so
        that model is served by a second, different backend -- one which
        Ollama's /api/ps sweep above is completely blind to, meaning a
        resident Bonsai would have silently survived a call that reports
        success.

        Requires llama-server to be running in ROUTER mode
        (--models-preset / --models-dir, no -m): GET /models and
        POST /models/unload return 404 in single-model mode. Verified
        directly, not assumed -- see models/alira-models.ini.

        No confirmation poll, unlike the Ollama path: Ollama's unload is
        fire-and-forget, so that one has to poll /api/ps to avoid the
        caller racing the eviction. The router's unload is synchronous --
        it stops the instance before responding (bounded by the preset's
        stop-timeout, default 10s) -- so a {"success": true} response
        already means the VRAM is back. Measured end to end: 84 MiB
        within 2s of the call returning, on a model holding 13.5GB.
        wait_timeout is accepted for signature symmetry with the Ollama
        helper and used as the request timeout floor.

        Best-effort throughout, same contract as the Ollama path: every
        failure is logged and swallowed. A model that fails to unload
        just stays resident, exactly as if this had never been called.
        """
        if not LLAMA_SERVER_URL:
            return

        try:
            response = requests.get(f"{LLAMA_SERVER_URL}/models", timeout=5)
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            log.warning(f"Couldn't query llama-server's models: {e}")
            return

        # Router mode answers in BOTH shapes: an OpenAI-style "data" list
        # (id) and an Ollama-style "models" list (model/name). Prefer
        # "data" -- it's the one whose ids match what gets sent as the
        # OpenAI "model" field, which is what /models/unload expects.
        entries = payload.get("data") or payload.get("models") or []
        model_ids = [
            e.get("id") or e.get("model") or e.get("name")
            for e in entries
            if isinstance(e, dict)
        ]

        for model_id in filter(None, model_ids):
            try:
                unload = requests.post(
                    f"{LLAMA_SERVER_URL}/models/unload",
                    json={"model": model_id},
                    timeout=max(wait_timeout, 15.0),
                )
                if unload.ok and unload.json().get("success"):
                    log.info(f"Unloaded llama-server model {model_id!r}.")
                else:
                    log.warning(
                        f"llama-server wouldn't unload {model_id!r}: "
                        f"HTTP {unload.status_code} {unload.text[:200]}"
                    )
            except Exception as e:
                log.warning(f"Couldn't unload llama-server model {model_id!r}: {e}")

    @classmethod
    def generate_text(
            cls,
            prompt: str,
            system_prompt: Optional[str] = None,
            conversation_history: Optional[list[dict]] = None,
            response_schema: Optional[dict] = None,
            as_json: bool = False,
            model_name: str = "qwen3:14b",
    ) -> dict[str, Any]:
        """

        Notes:
            the response_schema should be a dict formatted as:
            {
                "key1" : (dtype, ...), # ... means required
                "username" : (str, ...),
                "age" : (int, 0), # optional with default value 0
            }

        Args:
            prompt (str): _description_
            system_prompt (Optional[str], optional): _description_. Defaults to None.
            response_schema (Optional[dict], optional): _description_. Defaults to None.
            as_json (bool, optional): _description_. Defaults to False.

        Raises:
            RuntimeError: _description_
            RuntimeError: _description_

        Returns:
            dict[str, Any]: _description_
        """
        if as_json and not response_schema is None:
            # NOTE: We favor response format as a string formatted as a json
            # for speed. Passing strict json schemas or objects slows down
            # response significantly. For now, we will keep this, but it may
            # demonstrate unstable behavior as we switch models etc.
            system_prompt = (
                (system_prompt + "\n" if not system_prompt is None else "")
                + json_schema_instruction(response_schema)
            )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": prompt})

        def fmt_result(content: str) -> dict | str:
            result = content
            if as_json:
                try:
                    ValidationSchema = (
                        create_model("TempJSON", **response_schema)
                    )
                    result = result.strip("```json").strip("```")
                    result = (
                        ValidationSchema
                            .model_validate_json(result)
                            .model_dump()
                    ) # As dict
                except:
                    log.error(
                        "Error parsing schema with pydantic, "
                        "falling back to text."
                    )
                    raise RuntimeError()
            return result
        # Routed through the same concurrency=1 "chat" Celery queue as the
        # tool loop's own model calls, instead of talking to the client
        # directly -- see tasks/dispatch.py for why (this path used to
        # bypass the queue entirely, and once a second backend started
        # holding VRAM indefinitely that turned into a 352-second reply
        # delay). call_model is reused verbatim with tools=[], exactly the
        # shape _run_tool_loop's digest call already passes, rather than
        # adding a near-duplicate task that would drift from it.
        #
        # run_gpu_task runs inline when already inside a worker, which is
        # what keeps ingest_and_index_document's own summary/metadata
        # calls from deadlocking on the slot that task already holds.
        try:
            response = run_gpu_task(call_model, [model_name, messages, []])
        except (APIError, RuntimeError, CeleryTimeoutError) as e:
            raise RuntimeError(f"Text generation failed: {e}")

        # Deliberately OUTSIDE the try above: fmt_result raises a
        # RuntimeError of its own when a JSON response doesn't validate
        # against response_schema, and callers treat that as a parse
        # failure to retry, not a generation failure. Catching it up there
        # (now that RuntimeError is in the except clause, since that's how
        # call_model reports client errors across the process boundary)
        # would silently relabel every schema-parse failure as
        # "Text generation failed".
        content = response["choices"][0]["message"]["content"]
        tokens_used = response["usage"]["total_tokens"]
        return {
            "output": fmt_result(content),
            "tokens_used": tokens_used,
        }

    @classmethod
    def _run_tool_loop(
            cls,
            messages: list[dict],
            tools: list[dict],
            model_name: str,
            max_steps: int,
            hard_max_steps: Optional[int] = None,
            feedback: Optional[ChannelFeedback] = None,
            turn_id: Optional[UUID] = None,
            conversation_id: Optional[int] = None,
            history_len: int = 0,
    ) -> dict[str, Any]:
        """Drives the model<->tool loop to a final answer, mutating `messages`
        in place. Returns {"raw_output", "tokens_used"}.

        Each round that actually calls a tool is two model calls, not one:
        a decide call (tools attached, produces the tool_calls), then --
        once those tools have run -- a digest call (no tools attached at
        all) that condenses the raw results into a short note and replaces
        them in `messages` with it, before the next decide call. This costs
        an extra round trip per tool-using step, deliberately traded for
        three things: the digest call skips the ~2,780 tokens tool schemas
        cost on every other call, right when a tool result is also
        competing for room; messages stays lean for the rest of the loop
        too, since the raw tool output never lingers past the round it
        arrived in; and the digest call additionally drops the leading
        history_len messages (this conversation's older turns, spliced in
        by generate_with_tools) entirely -- digesting a fresh tool result
        never needed yesterday's chat to do it, so paying for that history
        a second time on every digest call was pure waste, independent of
        how long the open conversation has gotten. Decide calls are left
        untouched and still carry full history, since tool-selection can
        genuinely depend on earlier context -- only the pure-digest step,
        which structurally can't need it, is trimmed. Each of these calls
        still counts as its own step against steps_budget.

        turn_id/conversation_id (minted by pipeline.handle_incoming_message),
        if given, tag a live_action row (see memory.db.write_turn_action)
        after every model call (decide or digest) and every individual tool
        call -- the durable, granular record a caller can poll for "what's
        the agent doing right now", distinct from feedback.progress()'s
        ephemeral per-step signal below.

        The actual model call is dispatched to the "chat" Celery queue
        (tasks/chat_tasks.call_model) rather than called directly -- the
        one GPU-touching line in this whole loop, so it shares the same
        concurrency=1 worker as every other GPU-touching call in the
        system (see docs/ARCHITECTURE.md's "Planned: Celery/Redis"
        section for why only this one call moves, not the whole loop).
        Tool dispatch, current_user_id, and the run_on_main_loop bridge
        below are all unchanged -- they never leave this process.

        The step budget starts at max_steps but isn't fixed: if the model
        calls the continue_research tool (tooling/control_tools.py) before
        running out, the budget extends by _CONTINUE_RESEARCH_STEP_INCREMENT
        -- capped at hard_max_steps (defaults to max_steps, i.e. no
        extension allowed, if not given), so the model can ask for more
        room but never override an actual ceiling. continue_research is
        intercepted by name here, before normal TOOL_FUNCTIONS dispatch --
        its own function body never runs.

        feedback (see frontend/base.py), if given, gets one
        progress(label, fraction) call per step -- label names whichever
        tools were called (or "Thinking..." for a reasoning-only step),
        fraction is steps_taken / current_budget (recomputed each step, so
        it can move backward right after a continue_research grant -- an
        honest reflection of more work having just been added, not a bug).
        Defaults to a no-op ChannelFeedback() if not given.

        A truncated final answer (finish_reason == "length") is NOT treated
        as a failure: the partial text is returned as raw_output (with a
        note appended) like any other success, since the model did produce
        something usable, just not a complete answer. Raises ToolLoopError
        (never a bare RuntimeError) on a mid-loop API failure or exceeding
        the step budget -- genuine failures with no well-formed answer to
        fall back on -- always carrying whatever tokens/partial content
        exist at that point, so a caller can still persist/dump the
        attempt instead of losing it."""
        feedback = feedback or ChannelFeedback()
        hard_max_steps = max(hard_max_steps or max_steps, max_steps)
        steps_budget = max_steps
        steps_taken = total_tokens = 0
        last_content = None
        # Shallow-copied, not the caller's own list, before discover_tools
        # can ever append to it: CORE_TOOLS/REMINDER_TOOLS/GMAIL_HOOK_TOOLS
        # (pipeline.py) and the module-level TOOLS default above are each
        # one shared list object reused across every call that passes it --
        # mutating it in place would permanently leak one turn's discovered
        # categories into every other call (any user, any conversation)
        # that happens to reuse the same list afterward. The individual
        # tool schema dicts themselves are still shared by reference (never
        # mutated in place, only appended-or-not), so a shallow copy is
        # sufficient -- no need to deep-copy each schema too.
        tools = list(tools)
        try:
            while steps_taken < steps_budget:
                # >> Update steps
                steps_taken += 1

                # >> Actual response call
                response = call_model.apply_async(
                    args = [
                        model_name,
                        messages,
                        tools
                    ],
                    queue="chat",
                ).get(timeout=600)

                # >> Extract response and update messages
                total_tokens += response["usage"]["total_tokens"]
                choice = response["choices"][0]
                message = choice["message"]
                last_content = message["content"]
                tool_calls = message.get("tool_calls") or []

                _log_action(turn_id, conversation_id, "llm_call", {
                    "content": message["content"],
                    # Some models return reasoning/thinking separately
                    # from their actual answer (a "reasoning" field
                    # alongside "content" in the API response, confirmed
                    # directly against a real qwen3.5 call -- not every
                    # model populates it, hence .get() not indexing).
                    # Never fed back into messages (only content is), so
                    # this is purely for observability here.
                    "reasoning": message.get("reasoning"),
                    "finish_reason": choice["finish_reason"],
                    "tokens_used": response["usage"]["total_tokens"],
                    "had_tool_calls": bool(tool_calls),
                    # Named here, on the decide call itself -- not just
                    # inferred from whichever tool_call rows happen to
                    # follow. Confirmed real gap, not hypothetical: a
                    # tool that crashed before its own tool_call action
                    # could log (see json.dumps(result) above) left no
                    # record anywhere of which tool had even been
                    # requested, in either the live viewer or the DB.
                    "requested_tools": [
                        {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                        for c in tool_calls
                    ],
                })

                if not tool_calls:
                    if choice["finish_reason"] == "length":
                        # Degrade gracefully rather than failing the whole
                        # turn: the model did produce a partial answer,
                        # just not a complete one.
                        # Returned as a normal success (not raised) so the
                        # user gets something useful instead of a hard error.
                        log.warning(
                            "Model output was truncated (finish_reason=length);"
                            " returning the partial text instead of failing the"
                            " turn."
                        )
                        content = (message["content"] or "") + (
                            "\n\n_(response was cut short — it exceeded the "
                            "model's output limit)_"
                        )
                        return {
                            "raw_output": content,
                            "tokens_used": total_tokens,
                        }
                    return {
                        "raw_output": message["content"],
                        "tokens_used": total_tokens,
                    }

                # >> Update messages with the assistant's tool calls
                round_start_index = len(messages)
                messages.append({
                    "role": "assistant",
                    "content": message["content"],
                    "tool_calls": tool_calls,
                })
                tool_names_called = []
                # >> Run tool calls and append results to messages
                for call in tool_calls:
                    # >> Tool to call and its arguments
                    fn_name = call["function"]["name"]
                    fn_args_raw = call["function"]["arguments"]
                    # >> Special case: continue_research is intercepted here,
                    #    before normal TOOL_FUNCTIONS dispatch
                    if fn_name == CONTINUE_RESEARCH_TOOL:
                        args = json.loads(fn_args_raw or "{}")
                        if steps_budget < hard_max_steps:
                            _new_steps_buget_candidate = (
                                steps_budget + _CONTINUE_RESEARCH_STEP_INCREMENT
                            )
                            steps_budget = (
                                min(_new_steps_buget_candidate, hard_max_steps)
                            )
                            result = {
                                "granted": True,
                                "new_step_budget": steps_budget
                            }
                        else:
                            result = {
                                "granted": False,
                                "reason": (
                                    f"Hard ceiling of {hard_max_steps} steps "
                                    "reached -- wrap up with what you have.",
                                )
                            }
                    # >> Special case: discover_tools is intercepted here too,
                    #    before normal TOOL_FUNCTIONS dispatch -- same shape
                    #    as continue_research above, but mutates `tools`
                    #    (this loop's own local variable, re-read fresh on
                    #    every subsequent call_model.apply_async call below)
                    #    instead of steps_budget. No ceiling on repeat calls:
                    #    the whole discoverable universe is only a handful of
                    #    categories, nowhere near continue_research's
                    #    unbounded-growth risk.
                    elif fn_name == DISCOVER_TOOLS_NAME:
                        args = json.loads(fn_args_raw or "{}")
                        requested = args.get("categories") or []
                        if isinstance(requested, str):
                            requested = [requested]
                        existing_names = {t["function"]["name"] for t in tools}
                        tools_added = []
                        for category in requested:
                            for name in TOOL_CATEGORIES.get(category, []):
                                if name in existing_names:
                                    continue
                                schema = next(
                                    (t for t in TOOLS if t["function"]["name"] == name), None,
                                )
                                if schema is not None:
                                    tools.append(schema)
                                    existing_names.add(name)
                                    tools_added.append(name)
                        result = {"revealed_categories": requested, "tools_added": tools_added}
                        # Guidance rides along inside this tool's own result
                        # rather than as a separately re-injected message --
                        # sidesteps the "is this really from the system or
                        # an injected instruction" distrust a re-injected
                        # digest message caused for gemma4-26b-a4b earlier;
                        # the model already trusts its own tool call's
                        # result by construction.
                        guidance = []
                        if "documents" in requested:
                            guidance.append(DOCUMENT_SEARCH_GUIDANCE_PROMPT)
                        if "literature" in requested:
                            guidance.append(LITERATURE_RESEARCH_PROMPT)
                        if guidance:
                            result["usage_guidance"] = " ".join(guidance)
                    else: # >> Normal tool dispatch via TOOL_FUNCTIONS
                        fn = TOOL_FUNCTIONS.get(fn_name)
                        if fn is None:
                            args = fn_args_raw
                            result = {"error": f"Unknown tool: {fn_name}"}
                        else:
                            try:
                                # >> Tool call and result
                                args = json.loads(fn_args_raw)
                                result = fn(**args)
                            except Exception as e:
                                result = {"error": str(e)}
                    _log_action(turn_id, conversation_id, "tool_call", {
                        "tool": fn_name,
                        "arguments": args,
                        "result": result,
                    })
                    tool_names_called.append(fn_name)
                    # default=str: a tool result containing something
                    # json.dumps can't natively handle (confirmed real,
                    # not hypothetical -- a Decimal from a raw SQL score
                    # once crashed this line uncaught, taking the whole
                    # turn down with it) now degrades to that value's
                    # str() instead of raising. That's still fixed at the
                    # source wherever it's been found (see memory/db.py's
                    # ::float8 casts), but this is the generic backstop
                    # so a similar gap in some other tool can't do the
                    # same thing again.
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, default=str),
                    })

                # >> Digest round: process the just-appended tool results
                #    with no tool schemas attached at all -- saves the
                #    ~2,780 tokens those cost on every other call, right on
                #    the call that's also carrying whatever the tool(s)
                #    just returned, which is exactly where truncation was
                #    actually observed happening. Then collapse the raw
                #    exchange down to the short digest in messages itself,
                #    so it doesn't keep costing tokens on every later call
                #    either -- the same map-reduce instinct already used
                #    for document ingestion, applied to tool output instead
                #    of document text. Counts as its own step: a real
                #    model call, with real cost, not free infrastructure.
                steps_taken += 1
                # Drop the history_len messages right after the (optional)
                # system message -- this turn's own user_prompt onward,
                # plus whatever tool exchange just happened, is everything
                # a pure digest actually needs. See the docstring above for
                # why this is safe to skip here but not for decide calls.
                sys_prefix = messages[:1] if messages and messages[0]["role"] == "system" else []
                local_start = len(sys_prefix) + history_len
                digest_messages = sys_prefix + messages[local_start:] + [
                    {"role": "user", "content": tool_results_digest_prompt()}
                ]
                digest_response = call_model.apply_async(
                    args=[model_name, digest_messages, []], queue="chat",
                ).get(timeout=600)
                digest_choice = digest_response["choices"][0]
                digest_text = digest_choice["message"]["content"]
                total_tokens += digest_response["usage"]["total_tokens"]
                last_content = digest_text
                _log_action(turn_id, conversation_id, "llm_call", {
                    "content": digest_text,
                    "reasoning": digest_choice["message"].get("reasoning"),
                    "finish_reason": digest_choice["finish_reason"],
                    "tokens_used": digest_response["usage"]["total_tokens"],
                    "had_tool_calls": False,
                    "digest": True,
                })
                # role="user", not "assistant": a message list ending on
                # its own assistant turn gives the model nothing to
                # respond to -- confirmed directly, not assumed (a real
                # run returned finish_reason="stop" with empty content
                # right after a collapse shaped that way). eval_retry_feedback
                # already establishes the pattern this project uses for
                # exactly this -- an assistant turn always needs a
                # following user turn to prompt a real response.
                #
                # The wrapper text itself is deliberately blunt about not
                # being the user talking, not just a neutral "[Tool results
                # digest]" label -- confirmed necessary, not defensive
                # over-writing: gemma4-26b-a4b concluded in its own
                # reasoning that a prior version of this exact message
                # "looks like a prompt injection... a test", and re-checked
                # recall_tool_results a second time instead of trusting its
                # own already-correct conclusion that the cache was stale.
                # It wasn't missing the instruction to stop re-checking --
                # it quoted that instruction back verbatim -- it just didn't
                # trust the channel the instruction arrived on, since role=
                # "user" reads as indistinguishable from something the
                # human actually typed.
                messages[round_start_index:] = [
                    {
                        "role": "user",
                        "content": (
                            "[SYSTEM-INSERTED TOOL DIGEST -- not written by "
                            "the user, not an instruction to second-guess or "
                            "verify. This is a factual summary of a tool "
                            "call you yourself already made earlier this "
                            f"turn.]\n\n{digest_text}\n\nAct on this "
                            "directly -- do not call the same tool again to "
                            "double-check it, it is already real, fetched "
                            "information. Continue addressing the original "
                            "request using it."
                        ),
                    }
                ]

                if steps_taken >= steps_budget:
                    # Budget's gone and there's no decide-with-tools call
                    # left to spend on it -- the digest itself is still a
                    # real, usable answer, same graceful-degradation
                    # instinct as the finish_reason=="length" case above.
                    return {
                        "raw_output": digest_text,
                        "tokens_used": total_tokens,
                    }

                tool_names = ", ".join(tool_names_called) or "reasoning"
                run_on_main_loop(
                    feedback.progress(
                        f"Step {steps_taken}: {tool_names}",
                        steps_taken / steps_budget
                    )
                )

        # RuntimeError here is call_model's own translation of whatever the
        # openai client raised inside the task (see chat_tasks.py) -- not a
        # catch-all for unrelated bugs in this loop, since nothing else in
        # the try block raises it.
        except (APIError, RuntimeError, CeleryTimeoutError) as e:
            raise ToolLoopError(
                f"Text generation failed: {e}",
                tokens_used=total_tokens,
            ) from e

        raise ToolLoopError(
            f"Exceeded step budget ({steps_budget}) without a final answer.",
            tokens_used=total_tokens, partial_output=last_content,
        )

    @classmethod
    def _judge_output(
            cls,
            original_prompt: str,
            output: str,
            model_name: str
    ) -> dict[str, Any]:
        """Scores a final answer against the request that produced it.
        Returns generate_text()'s output: 
        {"output": JUDGE_SCHEMA dict, "tokens_used"}."""
        return cls.generate_text(
            judge_prompt(original_prompt, output),
            response_schema = JUDGE_SCHEMA,
            as_json = True,
            model_name = model_name,
        )

    @classmethod
    def generate_with_tools(
            cls,
            prompt: str,
            tools: list[dict] = TOOLS,
            system_prompt: Optional[str] = None,
            conversation_history: Optional[list[dict]] = None,
            response_schema: Optional[dict] = None,
            as_json: bool = False,
            model_name: str = "qwen3:14b",
            max_steps: int = 5,
            hard_max_steps: Optional[int] = None,
            feedback: Optional[ChannelFeedback] = None,
            explain_reasoning: bool = True,
            evaluate: bool = False,
            eval_model_name: Optional[str] = None,
            max_eval_retries: int = 1,
            turn_id: Optional[UUID] = None,
            conversation_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Runs the model with function-calling enabled, dispatching any
        tool_calls it makes via TOOL_FUNCTIONS and feeding the results back
        in until it returns a final text answer (or the step budget is
        hit). The model can extend its own step budget mid-loop by calling
        continue_research (tooling/control_tools.py) -- see
        _run_tool_loop's docstring; hard_max_steps is the ceiling that
        caps how far it can push that, regardless of how many times it asks.

        Notes:
            response_schema follows the same format as generate_text() and is
            only applied to the final answer, once the model stops calling
            tools:
            {
                "key1" : (dtype, ...), # ... means required
                "username" : (str, ...),
                "age" : (int, 0), # optional with default value 0
            }

            When evaluate=True, the final answer is scored by a judge call
            (see prompts.judge_prompt / schemas.JUDGE_SCHEMA). A failing
            judgement feeds its feedback back into the conversation and
            re-runs the tool loop, up to max_eval_retries times.

        Args:
            prompt (str): user prompt.
            tools (list[dict], optional): OpenAI-style tool schemas. Defaults
                to the full registered index in tooling.TOOLS.
            system_prompt (Optional[str], optional): Defaults to None.
            conversation_history (Optional[list[dict]], optional): Defaults to None.
            response_schema (Optional[dict], optional): Defaults to None.
            as_json (bool, optional): Defaults to False.
            model_name (str, optional): Defaults to "qwen3:14b".
            max_steps (int, optional): starting cap on model<->tool round
                trips per attempt, to guard against infinite tool-call
                loops. The model can extend this (see continue_research)
                up to hard_max_steps. Defaults to 5.
            hard_max_steps (Optional[int], optional): absolute ceiling the
                step budget can never exceed, however many times the model
                asks via continue_research. Defaults to max_steps (i.e. no
                extension allowed) if not given.
            feedback (Optional[ChannelFeedback], optional): progress signal
                for a long-running loop -- one progress() call per step,
                naming which tools were used. Defaults to a no-op.
            explain_reasoning (bool, optional): ask the model to state which
                tool it's using and why before each call. Defaults to True.
            evaluate (bool, optional): run a post-loop quality check. Defaults to False.
            eval_model_name (Optional[str], optional): model used to judge;
                defaults to model_name.
            max_eval_retries (int, optional): retries triggered by a failing
                evaluation. Defaults to 1.
            turn_id/conversation_id: tag every llm_call/tool_call this
                attempt makes in the live turn_actions log (see
                _run_tool_loop's docstring). Minted by
                pipeline.handle_incoming_message; not given (and
                harmlessly skipped) by a direct/standalone call.

        Raises:
            RuntimeError: on API failure, on JSON/schema validation failure,
                or if max_steps is exceeded without a final answer.

        Returns:
            dict[str, Any]: {
                "output": str | dict,
                "tokens_used": int,
                "evaluation": dict | None,# judge verdict, only if evaluate=True
            }
        """
        # >> Cascading addition to system prompt based on 
        #    options and tools present
        if as_json and not response_schema is None:
            system_prompt = (
                (system_prompt + "\n" if not system_prompt is None else "")
                + json_schema_instruction(response_schema)
            )
        if explain_reasoning:
            system_prompt = (
                (system_prompt + "\n" if not system_prompt is None else "")
                + TOOL_REASONING_PROMPT
            )
        if any(t["function"]["name"] in ("search_memory", "search_turns") for t in tools):
            system_prompt = (
                (system_prompt + "\n" if not system_prompt is None else "")
                + MEMORY_SEARCH_PRIORITY_PROMPT
            )
        if any(t["function"]["name"] == "search_documents" for t in tools):
            system_prompt = (
                (system_prompt + "\n" if not system_prompt is None else "")
                + DOCUMENT_SEARCH_GUIDANCE_PROMPT
            )
        if any(
            t["function"]["name"] in ("search_arxiv", "search_pubmed")
            for t in tools
        ):
            system_prompt = (
                (system_prompt + "\n" if not system_prompt is None else "")
                + LITERATURE_RESEARCH_PROMPT
            )
        if any(t["function"]["name"] == "recall_tool_results" for t in tools):
            system_prompt = (
                (system_prompt + "\n" if not system_prompt is None else "")
                + TOOL_CACHE_PRIORITY_PROMPT
            )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": prompt})
        history_len = len(conversation_history) if conversation_history else 0

        def fmt_result(content: str) -> dict | str:
            result = content
            if as_json:
                try:
                    ValidationSchema = (
                        create_model("TempJSON", **response_schema)
                    )
                    result = result.strip("```json").strip("```")
                    result = (
                        ValidationSchema
                            .model_validate_json(result)
                            .model_dump()
                    ) # As dict
                except:
                    log.error(
                        "Error parsing schema with pydantic, "
                        "falling back to text."
                    )
                    raise RuntimeError()
            return result

        # >> Run actual tool loop
        attempt = (
            cls._run_tool_loop(
                messages,
                tools,
                model_name,
                max_steps,
                hard_max_steps,
                feedback,
                turn_id,
                conversation_id,
                history_len=history_len,
            )
        )
        total_tokens = attempt["tokens_used"]

        evaluation = None
        if evaluate:
            retries_left = max_eval_retries
            while True:
                judgement = (
                    cls._judge_output(
                        prompt,
                        attempt["raw_output"],
                        eval_model_name or model_name
                    )
                )
                evaluation = judgement["output"]
                total_tokens += judgement["tokens_used"]

                if evaluation.get("passed") or retries_left <= 0:
                    break
                retries_left -= 1

                messages.append({
                    "role": "assistant",
                    "content": attempt["raw_output"]
                })
                messages.append({
                    "role": "user",
                    "content": eval_retry_feedback(evaluation.get("feedback"))
                })
                attempt = (
                    cls._run_tool_loop(
                        messages,
                        tools,
                        model_name,
                        max_steps,
                        hard_max_steps,
                        feedback,
                        turn_id,
                        conversation_id,
                        history_len=history_len,
                    )
                )
                total_tokens += attempt["tokens_used"]

        return {
            "output": fmt_result(attempt["raw_output"]),
            "tokens_used": total_tokens,
            "evaluation": evaluation,
        }

if __name__ == "__main__":

    # >> Set response output schema
    response_output = {
        "capital" : (str, ...),
        "year" : (int, ...),
    }

    # >> Get response
    test_response = (
        GenerativeService.generate_with_tools(
            "What is the capital of France? When did it become the capital?",
            response_schema = response_output,
            as_json = True
        )
    )

    print(test_response)