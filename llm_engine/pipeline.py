"""Channel-agnostic message-ingestion pipeline.

Conversation lifecycle, attachment ingestion, agent invocation, and
persistence — everything that isn't specific to *how* a message arrived.
webhook_server.py (Telegram) is a thin adapter over this; a future
Django-based chat interface would be another one, calling the exact same
functions instead of duplicating this logic.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv

try:
    from .context import current_chat_id, current_conversation_id, current_user_id
    from .documents import Attachment
    from .frontend.base import ChannelFeedback
    from .frontend.logging_feedback import LoggingFeedback
    from .prompts import (
        IDENTITY_PROMPT,
        NO_MESSAGE_SENTINEL,
        VERIFICATION_PROMPT,
        current_datetime_note,
        gmail_reply_draft_prompt,
        recall_history_note,
        scheduled_reminder_prompt,
    )
    from .tasks.chat_tasks import count_prompt_tokens
    from .tasks.ingestion_tasks import dispatch_ingestion
    from .tgi import GenerativeService
    from .tooling import TOOLS
    from .memory import (
        ConversationTranscript,
        create_conversation,
        get_open_conversation,
        get_turns,
        persist_turn,
        read_plan_markdown,
        set_conversation_file_path,
        summarize_and_embed_conversation,
        write_turn_action,
    )
except ImportError:
    from context import current_chat_id, current_conversation_id, current_user_id
    from documents import Attachment
    from frontend.base import ChannelFeedback
    from frontend.logging_feedback import LoggingFeedback
    from prompts import (
        IDENTITY_PROMPT,
        NO_MESSAGE_SENTINEL,
        VERIFICATION_PROMPT,
        current_datetime_note,
        gmail_reply_draft_prompt,
        recall_history_note,
        scheduled_reminder_prompt,
    )
    from tasks.chat_tasks import count_prompt_tokens
    from tasks.ingestion_tasks import dispatch_ingestion
    from tgi import GenerativeService
    from tooling import TOOLS
    from memory import (
        ConversationTranscript,
        create_conversation,
        get_open_conversation,
        get_turns,
        persist_turn,
        read_plan_markdown,
        set_conversation_file_path,
        summarize_and_embed_conversation,
        write_turn_action,
    )

load_dotenv()
log = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = os.getenv("MODEL_NAME", "qwen3:14b")
CONVERSATION_IDLE_TIMEOUT_SECONDS = float(os.getenv("CONVERSATION_IDLE_TIMEOUT_SECONDS", "3600"))
# Character budget for live conversation_history, not a turn count -- a
# handful of big, tool-heavy turns can cost as much context as many short
# ones, so a turn-count cap alone never actually bounded how much of the
# model's tight 4096-token window this could eat (confirmed cause of
# repeated finish_reason=length as a conversation grows). See
# _turns_to_history.
MAX_HISTORY_CHARS = int(os.getenv("MAX_HISTORY_CHARS", "3000"))
# Starting/ceiling step budget for generate_with_tools' tool loop -- the
# model can extend AGENT_MAX_STEPS up to AGENT_HARD_MAX_STEPS by calling
# continue_research (see tgi._run_tool_loop), but never past the ceiling.
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "5"))
AGENT_HARD_MAX_STEPS = int(os.getenv("AGENT_HARD_MAX_STEPS", "20"))
# Default toolset for a normal chat turn (handle_incoming_message) --
# everything relevant to nearly any conversation (memory recall, web
# search, continuing a multi-step task), plus discover_tools itself.
# Everything else (gmail, literature, documents, reminders, database --
# see tooling/discovery.py's TOOL_CATEGORIES) starts hidden and gets
# revealed mid-turn via discover_tools when actually needed, instead of
# every tool's full schema being sent on every single call regardless of
# relevance (measured cost: ~2,780 tokens at 12 tools, ~3,205 at 14 --
# see docs/ROADMAP.md -- growing linearly with the full registry, now 24
# tools and counting as more connectors land). Deliberately does NOT
# include discover_tools in REMINDER_TOOLS/GMAIL_HOOK_TOOLS below -- a
# hook's already-narrow scoped call must not be able to discover its way
# into the full registry; tool-list membership is the only gate
# discover_tools' interception in tgi.py needs.
CORE_TOOLS = [
    t for t in TOOLS
    if t["function"]["name"] in (
        "web_search", "recall_tool_results", "search_memory", "search_turns",
        "read_conversation", "recall_conversation_history", "recall_turn_detail",
        "continue_research", "discover_tools",
    )
]
# Deliberately narrow toolset for handle_reminder_hook -- it doesn't
# get the full registry (web_search, document tools, etc.), just enough
# to check what's already happened today. Widen this later (e.g. once
# habit-completion tracking exists) by adding names here, not by
# reworking the function itself.
REMINDER_TOOLS = [
    t for t in TOOLS
    if t["function"]["name"] in ("recall_conversation_history", "recall_turn_detail")
]
# Same discipline as REMINDER_TOOLS, for the gmail_poll hook's own scoped
# call (handle_gmail_reply_draft): just enough to decide on and draft a
# reply to the one matched email, never the full registry.
GMAIL_HOOK_TOOLS = [
    t for t in TOOLS
    if t["function"]["name"] in ("draft_gmail_reply", "recall_conversation_history", "recall_turn_detail")
]

# Open conversation per chat: (transcript, conversation_id, last_active_monotonic).
# Lives for the life of the process, reset by start_new_conversation(), or
# auto-archived after CONVERSATION_IDLE_TIMEOUT_SECONDS of inactivity. Keyed
# by chat_id — a single process serving more than one channel could collide
# if two channels ever reused the same chat_id string; not a concern while
# each channel is its own process (Telegram bot, future Django app).
_conversations: dict[str, tuple[ConversationTranscript, int, float]] = {}

# Keeps references to fire-and-forget background tasks (archiving on
# reset/timeout) alive until they finish — asyncio only guarantees a task
# runs if something still holds a reference to it.
_background_tasks: set[asyncio.Task] = set()

# Per-chat in-flight persist_turn task (see _persist_in_background). The
# reply no longer waits for persistence, so the NEXT turn in that chat
# has to make sure the previous one actually landed before it reads
# history -- otherwise a fast follow-up could call get_turns() before the
# turn it's replying to exists, and the model would answer with a hole in
# its own conversation. Keyed by chat_id, same as _conversations.
_pending_persists: dict[str, asyncio.Task] = {}

# Per-chat model override (e.g. via a /switch_model command), keyed the same
# way as _conversations. Only affects handle_incoming_message's own model
# choice — an explicit model_name argument to any function still wins, and
# archiving/summarization always use their own model_name/DEFAULT_MODEL_NAME.
_model_overrides: dict[str, str] = {}


def set_model_override(chat_id: str, model_name: str) -> None:
    _model_overrides[chat_id] = model_name


def get_model_override(chat_id: str) -> Optional[str]:
    return _model_overrides.get(chat_id)


def clear_model_override(chat_id: str) -> None:
    _model_overrides.pop(chat_id, None)


def _archive_in_background(conversation_id: int, chat_id: str, model_name: str) -> None:
    task = asyncio.create_task(_archive_conversation(conversation_id, chat_id, model_name))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _archive_conversation(conversation_id: int, chat_id: str, model_name: str) -> None:
    try:
        summary = await summarize_and_embed_conversation(conversation_id, model_name=model_name)
        log.info(f"chat={chat_id} archived conversation={conversation_id}: {summary[:120]!r}")
    except ValueError:
        log.info(f"chat={chat_id} conversation={conversation_id} had no turns, skipping summary.")
    except Exception:
        log.error(f"chat={chat_id} failed to archive conversation={conversation_id}", exc_info=True)


def _persist_in_background(chat_id: str, *args, **kwargs) -> None:
    """Runs persist_turn without the caller waiting for it, so the user's
    reply goes out as soon as the agent has an answer.

    persist_turn used to be awaited before handle_incoming_message
    returned, which put it squarely on the reply's critical path -- and
    it is not cheap: it embeds both the prompt and the output, two
    GPU-touching calls, before its DB write. Measured in production: a
    turn whose model call finished in 4 seconds didn't reach the user for
    another 352, because those embeddings were queued behind a background
    conversation-archive summarization competing for the same GPU. The
    answer was already sitting complete in turn_actions the whole time,
    visible in the live viewer, while the chat looked dead.

    Losing a turn to a crash in this window costs little: turn_actions
    already holds a durable, row-per-action record of the whole turn,
    committed as each action happened rather than at the end.

    Failures are logged, never raised -- this runs detached, so an
    exception here would otherwise surface only as an unretrieved-task
    warning at some unrelated later moment."""
    task = asyncio.create_task(persist_turn(*args, **kwargs))
    _pending_persists[chat_id] = task
    _background_tasks.add(task)

    def _done(finished: asyncio.Task) -> None:
        _background_tasks.discard(finished)
        # Identity check, not a bare pop: a later turn in this same chat
        # may already have replaced the entry, and clearing that one would
        # let the turn after it skip a wait it genuinely needed.
        if _pending_persists.get(chat_id) is finished:
            _pending_persists.pop(chat_id, None)
        if not finished.cancelled() and finished.exception() is not None:
            log.error(
                f"chat={chat_id} failed to persist turn in background",
                exc_info=finished.exception(),
            )

    task.add_done_callback(_done)


async def _await_pending_persist(chat_id: str) -> None:
    """Blocks until this chat's previous turn has finished persisting, so
    the history this turn is about to read is complete. Normally returns
    instantly -- persistence has long since finished by the time the user
    types again; it only actually waits when a follow-up arrives while
    the previous turn's embeddings are still in flight.

    Swallows the previous turn's failure: it's already been logged by
    _persist_in_background's callback, and a turn that failed to persist
    must not also take down the next, unrelated turn."""
    task = _pending_persists.get(chat_id)
    if task is None or task.done():
        return
    try:
        await task
    except Exception:
        pass


async def get_conversation(chat_id: str, user_id: str) -> tuple[ConversationTranscript, int]:
    """Returns the chat's open (transcript, conversation_id), creating one
    if needed or if the previous one has been idle past
    CONVERSATION_IDLE_TIMEOUT_SECONDS (auto-archiving it first).

    On an in-memory cache miss, checks the DB for an already-open (never
    archived) conversation for this chat_id before creating a new one --
    confirmed real gap otherwise: _conversations is pure in-memory and
    empty on every process restart (routine during active development),
    so without this check a restart silently orphans whatever
    conversation was open. The abandoned one just sits in the DB forever
    unarchived -- invisible to search_memory (only covers archived
    conversations) and unreachable from the new one via
    recall_conversation_history (scoped to whichever conversation_id is
    actually current)."""
    now = time.monotonic()
    existing = _conversations.get(chat_id)

    if existing is not None:
        transcript, conversation_id, last_active = existing
        if now - last_active > CONVERSATION_IDLE_TIMEOUT_SECONDS:
            log.info(f"chat={chat_id} conversation={conversation_id} idle timeout, archiving")
            _archive_in_background(conversation_id, chat_id, DEFAULT_MODEL_NAME)
            existing = None

    if existing is None:
        open_row = await get_open_conversation(chat_id)
        # Idle-check this path too, not just the in-memory cache-hit branch
        # above -- get_open_conversation exists specifically for the
        # cache-miss/process-restart case, which used to resume an old
        # conversation unconditionally regardless of how long it had been
        # idle (confirmed real: one sat open 2.7 days and several restarts
        # before being resumed wholesale, stale "today's date" turns and
        # all -- restarting during active development is routine, so this
        # path is hit constantly, not an edge case).
        if open_row is not None:
            idle_seconds = (
                datetime.now(timezone.utc) - open_row["last_active_at"]
            ).total_seconds()
            if idle_seconds > CONVERSATION_IDLE_TIMEOUT_SECONDS:
                log.info(
                    f"chat={chat_id} conversation={open_row['id']} found open in DB but idle "
                    f"{idle_seconds:.0f}s > {CONVERSATION_IDLE_TIMEOUT_SECONDS:.0f}s, archiving instead of resuming"
                )
                _archive_in_background(open_row["id"], chat_id, DEFAULT_MODEL_NAME)
                open_row = None

        if open_row is not None and open_row["file_path"]:
            conversation_id = open_row["id"]
            transcript = ConversationTranscript(
                user_id=user_id, chat_id=chat_id, conversation_id=conversation_id,
                path=open_row["file_path"],
            )
            log.info(f"user={user_id} chat={chat_id} resumed conversation={conversation_id} (cache miss, found open in DB)")
        else:
            conversation_id = await create_conversation(user_id, chat_id)
            transcript = ConversationTranscript(
                user_id=user_id, chat_id=chat_id, conversation_id=conversation_id,
            )
            await set_conversation_file_path(conversation_id, str(transcript.path))
            log.info(f"user={user_id} chat={chat_id} opened conversation={conversation_id} at {transcript.path}")
    else:
        transcript, conversation_id, _ = existing

    _conversations[chat_id] = (transcript, conversation_id, now)
    return transcript, conversation_id


def _turns_to_history(turns: list[dict]) -> tuple[list[dict], int]:
    """Projects the conversation's most recent turns into a
    conversation_history list for generate_with_tools — live, short-term
    memory for the still-open conversation. Distinct from search_memory,
    which only covers already-archived conversations.

    Bounded by MAX_HISTORY_CHARS (a character budget), walking backward
    from the most recent turn and stopping once it's spent -- not a fixed
    turn count, since a handful of big turns can cost as much as many
    small ones. Always includes at least the single most recent turn, even
    if it alone exceeds the budget, on the theory that some history beats
    silently dropping the last thing said.

    Returns (history, included_count) -- included_count is how many of the
    most recent turns made it in, so the caller can tell the model exactly
    what to pass recall_conversation_history's skip_recent as, if anything
    was left out.

    Each turn's user_prompt is prefixed with when it actually happened
    (same format as prompts.current_datetime_note(), local time, so the
    two are directly comparable) -- confirmed a real gap otherwise, not
    hypothetical: this history used to carry no timestamp at all, so a
    turn from days ago asserting "today is September 2" read as
    equally current as anything actually said today, and a model with no
    reason to doubt it repeated the stale date confidently. VERIFICATION_PROMPT
    tells the model to actually use these against current_datetime_note()'s
    "now," not just decorates the text."""
    history = []
    included = 0
    budget = MAX_HISTORY_CHARS
    for turn in reversed(turns):
        output = turn["result"].get("output")
        content = output if isinstance(output, str) else json.dumps(output)
        timestamp = turn["created_at"].astimezone().strftime("%A, %B %d, %Y, %I:%M %p %Z")
        prompt_with_timestamp = f"[{timestamp}] {turn['user_prompt']}"
        turn_chars = len(prompt_with_timestamp) + len(content)
        if included > 0 and turn_chars > budget:
            break
        budget -= turn_chars
        history.append({"role": "assistant", "content": content})
        history.append({"role": "user", "content": prompt_with_timestamp})
        included += 1
    history.reverse()
    return history, included


async def get_context_usage(chat_id: str) -> Optional[dict[str, Any]]:
    """Stats about chat_id's live conversation-history window -- what
    webhook_server.py's /context_stats command reports. Returns None if
    there's no open conversation for this chat yet (nothing to report).

    A dict, not a fixed set of return values, deliberately -- this is
    meant to grow (e.g. current model, tool-cache hit rate) without
    changing this function's shape; a caller just adds a line per new
    key rather than this needing a new return type each time."""
    existing = _conversations.get(chat_id)
    if existing is None:
        return None
    _, conversation_id, _ = existing
    turns = await get_turns(conversation_id)
    conversation_history, included_count = _turns_to_history(turns)
    return {
        "history_chars_used": sum(len(m["content"]) for m in conversation_history),
        "history_char_budget": MAX_HISTORY_CHARS,
        "turns_included": included_count,
        "turns_total": len(turns),
    }


async def count_context_tokens(chat_id: str, model_name: str) -> Optional[int]:
    """Real (not chars/4-estimated) token count for chat_id's current
    live conversation-history window, on model_name's own tokenizer --
    see tasks.chat_tasks.count_prompt_tokens for how. Returns None under
    the same condition get_context_usage does (no open conversation
    yet); 0 for an open conversation whose history window is empty (no
    real prompt to send a model, and an empty one isn't guaranteed to
    behave the same across models).

    Concatenates conversation_history's message contents only, not the
    exact chat-template-formatted prompt the model actually sees (role
    markers, system prompt) -- close enough for a usage estimate, and
    avoids needing to reconstruct that formatting here just to count it."""
    existing = _conversations.get(chat_id)
    if existing is None:
        return None
    _, conversation_id, _ = existing
    turns = await get_turns(conversation_id)
    conversation_history, _ = _turns_to_history(turns)
    if not conversation_history:
        return 0
    text = "\n".join(m["content"] for m in conversation_history)
    return await asyncio.to_thread(
        lambda: count_prompt_tokens.apply_async(args=[model_name, text], queue="chat").get(timeout=120)
    )


async def start_new_conversation(chat_id: str, model_name: Optional[str] = None) -> None:
    """Archives whichever conversation is open for chat_id (if any), in the
    background, and drops it from the cache — the next message opens a
    fresh one. Used by e.g. a /start command."""
    old = _conversations.pop(chat_id, None)
    if old is not None:
        _, old_conversation_id, _ = old
        _archive_in_background(old_conversation_id, chat_id, model_name or DEFAULT_MODEL_NAME)


async def summarize_open_conversation(chat_id: str, model_name: Optional[str] = None) -> str:
    """Summarizes + embeds the chat's current, still-open conversation on
    demand, without closing it. Used by e.g. a /summarize command.

    Raises ValueError if there's no open conversation, or it has no turns.
    """
    existing = _conversations.get(chat_id)
    if existing is None:
        raise ValueError(f"No open conversation for chat_id={chat_id}.")
    _, conversation_id, _ = existing
    return await summarize_and_embed_conversation(conversation_id, model_name=model_name or DEFAULT_MODEL_NAME)


async def handle_incoming_message(
        user_id: str,
        chat_id: str,
        text: str,
        attachments: Optional[list[Attachment]] = None,
        model_name: Optional[str] = None,
        feedback: Optional[ChannelFeedback] = None,
) -> dict[str, Any]:
    """The channel-agnostic entry point: get/create the conversation, ingest
    any attachments, run the agent with live conversation history, persist
    the turn, and return generate_with_tools()'s result dict. Callers are
    responsible for sending result["output"] back over their own channel —
    this never touches Telegram, Django, or anything channel-specific.

    model_name resolution: explicit argument > this chat's set_model_override
    (e.g. via a /switch_model command) > DEFAULT_MODEL_NAME.

    feedback (see frontend/base.py), if given, is passed straight through to
    ingest_and_index_document for any attachments (see its own docstring for
    the stage-by-stage progress it reports), and separately wraps the
    generate_with_tools call below with its own start()/finish() so a long
    tool-calling turn keeps reporting activity too. Channel-agnostic by
    design -- this function never constructs one itself.

    Mints turn_id (a plain uuid4) once, up front -- it tags every row this
    turn writes to the live turn_actions log (see memory.db.write_turn_action
    and tgi._run_tool_loop's docstring), including any ingestion pipeline
    nested inside it, and carries onto the final conversation_turns row too
    once persist_turn runs, so the two can be joined later.
    """
    model_name = model_name or _model_overrides.get(chat_id) or DEFAULT_MODEL_NAME
    feedback = feedback or ChannelFeedback()
    turn_id = uuid.uuid4()
    transcript, conversation_id = await get_conversation(chat_id, user_id)
    # The previous turn's persist_turn now runs detached (see
    # _persist_in_background), so make sure it actually landed before
    # reading history -- otherwise a quick follow-up could read turns
    # that don't include the exchange it's responding to.
    await _await_pending_persist(chat_id)
    turns = await get_turns(conversation_id)
    conversation_history, included_count = _turns_to_history(turns)
    await write_turn_action(turn_id, conversation_id, user_id, "turn_start", {"user_prompt": text})

    # Attachments become part of the effective prompt text itself (not just
    # a one-off system-prompt note), so the reference to the uploaded
    # document's file_path survives into conversation_history on later
    # turns too, via the persisted user_prompt. dispatch_ingestion runs the
    # whole ingest_and_index_document pipeline (extraction, chunking,
    # summarizing, persistence -- see memory/document_index.py) as a
    # Celery task on the "chat" queue, so a live upload is never stuck
    # behind a background literature-processing batch (see
    # docs/ARCHITECTURE.md's "Planned: Celery/Redis" section). Wrapped in
    # LoggingFeedback so ingestion's own start()/progress()/finish() calls
    # also land in turn_actions (as pipeline_start/pipeline_step/
    # pipeline_end) under this same turn_id, with zero changes needed in
    # document_index.py itself.
    effective_text = text
    if attachments:
        ingestion_feedback = LoggingFeedback(turn_id, conversation_id, user_id, feedback)
        notes = []
        for attachment in attachments:
            _document_id, note = await dispatch_ingestion(
                user_id, conversation_id, attachment.filename, attachment.content,
                model_name=model_name, feedback=ingestion_feedback, queue="chat",
            )
            notes.append(note)
        effective_text = (effective_text + "\n\n" if effective_text else "") + "\n".join(notes)

    system_prompt = IDENTITY_PROMPT + "\n\n" + current_datetime_note() + "\n\n" + VERIFICATION_PROMPT
    if included_count < len(turns):
        system_prompt += "\n\n" + recall_history_note(included_count)

    # Scopes search_memory/read_conversation/read_document/
    # recall_conversation_history to this user (and conversation), without
    # ever exposing either as a model-settable tool parameter.
    current_user_id.set(user_id)
    current_conversation_id.set(conversation_id)
    current_chat_id.set(chat_id)

    # generate_with_tools is a blocking call; run it off the event loop so
    # it doesn't stall the caller's event loop while the model/tools run.
    await feedback.start("Thinking")
    try:
        result = await asyncio.to_thread(
            GenerativeService.generate_with_tools,
            effective_text,
            system_prompt=system_prompt,
            tools=CORE_TOOLS,
            conversation_history=conversation_history,
            model_name=model_name,
            max_steps=AGENT_MAX_STEPS,
            hard_max_steps=AGENT_HARD_MAX_STEPS,
            feedback=feedback,
            turn_id=turn_id,
            conversation_id=conversation_id,
        )
    except Exception as e:
        # Dump whatever's available (ToolLoopError carries tokens/
        # partial_output) rather than losing the attempt entirely --
        # persisted the same way a successful turn is, so it's visible in
        # the transcript/DB for debugging even though the caller still
        # sees this exception. The detailed step-by-step history up to the
        # failure already lives in turn_actions (this turn_id), durably,
        # regardless of whether this except block even runs.
        await feedback.finish(f"❌ Failed: {e}")
        await write_turn_action(turn_id, conversation_id, user_id, "turn_end", {
            "status": "error", "error": str(e),
        })
        error_result = {
            "output": getattr(e, "partial_output", None) or f"[No output produced -- error: {e}]",
            "tokens_used": getattr(e, "tokens_used", 0),
            "evaluation": None,
            "error": str(e),
        }
        await persist_turn(
            transcript, conversation_id, user_id, effective_text, error_result,
            model_name=model_name, turn_id=turn_id,
        )
        raise
    await feedback.finish()
    await write_turn_action(turn_id, conversation_id, user_id, "turn_end", {
        "status": "success", "output": result.get("output"), "tokens_used": result.get("tokens_used"),
    })

    # Detached, NOT awaited: returning immediately is what lets the
    # caller send the reply the moment the answer exists, instead of
    # after two embedding calls have fought their way through the GPU
    # queue. See _persist_in_background. The error path above still
    # awaits persist_turn deliberately -- there the turn is about to
    # raise, nobody is waiting on a reply, and a durable record of the
    # failure matters more than the latency.
    _persist_in_background(
        chat_id,
        transcript, conversation_id, user_id, effective_text, result,
        model_name=model_name, turn_id=turn_id,
    )
    return result


async def handle_reminder_hook(
        user_id: str, chat_id: str, intent: str, model_name: Optional[str] = None,
) -> dict[str, Any]:
    """Fires one 'reminder'-kind hooks row -- called by
    llm_engine.hooks.reminder's @hook_handler("reminder") adapter, which
    the hook ticker (webhook_server.py) reaches via its generic
    HOOK_HANDLERS dispatch. A turn triggered by the clock instead of an
    incoming message. Otherwise mirrors handle_incoming_message's
    structure (mint turn_id, write turn_start/turn_end, persist_turn) so
    it's visible in turn_actions/conversation_turns exactly like a real
    turn -- a reply to the resulting message becomes the next normal turn
    in the same open conversation, with this one already in its history.

    Deliberately scoped like the tool loop's own digest call, not like a
    normal turn: conversation_history is explicitly omitted (None) and
    the toolset is the narrow REMINDER_TOOLS, not the full registry --
    the model gets the user's plan markdown plus this one intent, and
    can reach for recall_conversation_history itself if it actually needs
    to check what's already happened today, rather than that history
    being dumped in by default regardless of length.

    The persisted/replayed prompt is a short synthetic placeholder
    ("[Scheduled reminder: ...]"), NOT the full plan-markdown prompt
    actually sent to the model -- otherwise every future turn's history
    window would carry the entire plan forward, exactly the bloat this
    whole scoping exercise exists to avoid.

    Returns generate_with_tools()'s result dict plus "skip_send": True if
    the model opted out via prompts.NO_MESSAGE_SENTINEL -- the turn is
    still persisted either way, but the caller must not forward output to
    the user when this is True. That caller is llm_engine.hooks.reminder's
    handler, which translates skip_send into the generic
    HookResult(message=None) every hook kind's handler returns -- this
    function itself doesn't know about HookResult, on purpose, to keep it
    at the same "one scoped model call" level as before this existed.
    """
    model_name = model_name or DEFAULT_MODEL_NAME
    turn_id = uuid.uuid4()
    transcript, conversation_id = await get_conversation(chat_id, user_id)

    persisted_prompt = f"[Scheduled reminder: {intent}]"
    model_prompt = scheduled_reminder_prompt(read_plan_markdown(user_id), intent)

    await write_turn_action(turn_id, conversation_id, user_id, "turn_start", {"user_prompt": persisted_prompt})

    current_user_id.set(user_id)
    current_conversation_id.set(conversation_id)
    current_chat_id.set(chat_id)

    try:
        result = await asyncio.to_thread(
            GenerativeService.generate_with_tools,
            model_prompt,
            system_prompt=IDENTITY_PROMPT + "\n\n" + current_datetime_note() + "\n\n" + VERIFICATION_PROMPT,
            tools=REMINDER_TOOLS,
            conversation_history=None,
            model_name=model_name,
            max_steps=AGENT_MAX_STEPS,
            hard_max_steps=AGENT_HARD_MAX_STEPS,
            turn_id=turn_id,
            conversation_id=conversation_id,
        )
    except Exception as e:
        await write_turn_action(turn_id, conversation_id, user_id, "turn_end", {
            "status": "error", "error": str(e),
        })
        error_result = {
            "output": getattr(e, "partial_output", None) or f"[No output produced -- error: {e}]",
            "tokens_used": getattr(e, "tokens_used", 0),
            "evaluation": None,
            "error": str(e),
        }
        await persist_turn(
            transcript, conversation_id, user_id, persisted_prompt, error_result,
            model_name=model_name, turn_id=turn_id,
        )
        raise

    await write_turn_action(turn_id, conversation_id, user_id, "turn_end", {
        "status": "success", "output": result.get("output"), "tokens_used": result.get("tokens_used"),
    })
    await persist_turn(
        transcript, conversation_id, user_id, persisted_prompt, result,
        model_name=model_name, turn_id=turn_id,
    )

    output = (result.get("output") or "").strip()
    result["skip_send"] = (output == NO_MESSAGE_SENTINEL)
    return result


async def handle_gmail_reply_draft(
        user_id: str, chat_id: str, message: dict[str, Any], model_name: Optional[str] = None,
) -> dict[str, Any]:
    """Fires one matched email from a 'gmail_poll' hook that's configured
    to draft (on_match in {"draft", "both"}) -- called by
    llm_engine.hooks.gmail's handler. Structurally identical to
    handle_reminder_hook: mint turn_id, synthetic persisted prompt (never
    the full email body, same anti-bloat reasoning), narrow
    GMAIL_HOOK_TOOLS (just draft_gmail_reply plus read-only recall), write
    turn_start/turn_end, persist_turn -- a reply-drafting decision is just
    as real a turn as an incoming message or a fired reminder, and belongs
    in turn_actions/conversation_turns the same way.

    message is one normalized dict from
    tooling._tooling_deps.gmail_client.list_matching_messages (id,
    thread_id, subject, from, snippet, internal_date). Returns
    generate_with_tools()'s result dict, same shape as handle_reminder_hook
    -- callers read result["output"] for what (if anything) to tell the
    user; whether draft_gmail_reply actually got called is visible in
    turn_actions, not this return value, since drafting is silent by
    design (see hooks/gmail.py's on_match handling)."""
    model_name = model_name or DEFAULT_MODEL_NAME
    turn_id = uuid.uuid4()
    transcript, conversation_id = await get_conversation(chat_id, user_id)

    persisted_prompt = f"[Gmail: drafting reply to {message['subject']!r} from {message['from']}]"
    model_prompt = gmail_reply_draft_prompt(
        message["subject"], message["from"], message["snippet"], message["id"],
    )

    await write_turn_action(turn_id, conversation_id, user_id, "turn_start", {"user_prompt": persisted_prompt})

    current_user_id.set(user_id)
    current_conversation_id.set(conversation_id)
    current_chat_id.set(chat_id)

    try:
        result = await asyncio.to_thread(
            GenerativeService.generate_with_tools,
            model_prompt,
            system_prompt=IDENTITY_PROMPT + "\n\n" + current_datetime_note() + "\n\n" + VERIFICATION_PROMPT,
            tools=GMAIL_HOOK_TOOLS,
            conversation_history=None,
            model_name=model_name,
            max_steps=AGENT_MAX_STEPS,
            hard_max_steps=AGENT_HARD_MAX_STEPS,
            turn_id=turn_id,
            conversation_id=conversation_id,
        )
    except Exception as e:
        await write_turn_action(turn_id, conversation_id, user_id, "turn_end", {
            "status": "error", "error": str(e),
        })
        error_result = {
            "output": getattr(e, "partial_output", None) or f"[No output produced -- error: {e}]",
            "tokens_used": getattr(e, "tokens_used", 0),
            "evaluation": None,
            "error": str(e),
        }
        await persist_turn(
            transcript, conversation_id, user_id, persisted_prompt, error_result,
            model_name=model_name, turn_id=turn_id,
        )
        raise

    await write_turn_action(turn_id, conversation_id, user_id, "turn_end", {
        "status": "success", "output": result.get("output"), "tokens_used": result.get("tokens_used"),
    })
    await persist_turn(
        transcript, conversation_id, user_id, persisted_prompt, result,
        model_name=model_name, turn_id=turn_id,
    )
    return result
