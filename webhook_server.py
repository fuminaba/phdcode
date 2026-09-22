import asyncio
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from utils import setup_logging
from llm_engine.context import set_main_loop
from llm_engine.documents import Attachment
from llm_engine.frontend.telegram import TelegramFeedback
from llm_engine.frontend.telegram_format import markdown_to_telegram_html, strip_telegram_html
from llm_engine.memory import get_due_hooks, init_schema, mark_hook_fired, render_plan_markdown
from llm_engine.tgi import GenerativeService
from llm_engine import pipeline
from llm_engine.hooks import HOOK_HANDLERS

load_dotenv()
logger = setup_logging("webhook_server")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MODEL_NAME = os.getenv("MODEL_NAME")
# How often the hook ticker (_hook_ticker below) checks for due hooks
# rows (reminders, gmail_poll, ...). Not a GPU-touching cost by itself --
# a tick with nothing due is one cheap SQL query -- so this can be fairly
# frequent without worry; 60s keeps reminders feeling close to on-time
# without polling so often it'd matter if the DB were ever slow.
# HOOK_TICK_SECONDS replaces the old REMINDER_TICK_SECONDS name (still
# read as a fallback for one release, so an existing .env isn't silently
# ignored) now that firing a hook can mean more than sending a reminder.
HOOK_TICK_SECONDS = int(os.getenv("HOOK_TICK_SECONDS", os.getenv("REMINDER_TICK_SECONDS", "60")))

_TELEGRAM_MESSAGE_LIMIT = 4096  # Telegram's hard per-message cap; sendMessage raises BadRequest past it.

# Single source of truth for /help's reply -- also mirrored in docs/COMMANDS.md
# for reference outside Telegram. Keep both in sync when a command changes.
_COMMANDS = [
    ("/start", "Start a new conversation (archives the previous one in the background)."),
    ("/summarize", "Summarize + embed the current conversation without closing it."),
    ("/switch_model [name]", "Show the current/available models, or switch this chat to one."),
    ("/context_stats", "Show how much of the live conversation-history budget this chat is using."),
    ("/help", "Show this list of commands."),
]

# Things the agent can do that have no dedicated command -- just ask for
# them in normal conversation and the model decides which tool to call.
# Also mirrored in docs/COMMANDS.md; keep both in sync when a capability
# changes.
_CAPABILITIES = [
    ("Web search", "Ask me to look something up online."),
    ("Memory", "Ask what we discussed earlier -- this conversation, or a past one."),
    ("Documents", "Send me a file (PDF, etc.) and ask questions about it."),
    ("Literature search", "Ask me to search arXiv or PubMed for papers on a topic."),
    ("Reminders & habits", "Ask me to remind you about something, once or on a recurring "
        "schedule (e.g. \"remind me to take my vitamins every day at 8am\") -- I'll message "
        "you proactively, even if you're not chatting. Ask what's currently scheduled, or to "
        "cancel one, any time."),
]


def _split_for_telegram(text: str, limit: int = _TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Splits text into <=limit-char pieces, preferring the last newline
    before the limit so a long agent reply doesn't get sliced mid-sentence."""
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    chunks.append(text)
    return chunks


async def _send_formatted(send_fn, text: str) -> None:
    """Converts the model's markdown to Telegram's HTML subset (see
    frontend/telegram_format.py) and sends it in <=limit-char chunks --
    confirmed necessary independent of formatting: a real document-summary
    reply once hit 5054 chars and crashed reply_text with BadRequest
    ("Message is too long"). Falls back to plain, tag-stripped text for
    any single chunk Telegram rejects as malformed HTML, rather than
    losing that chunk's content entirely.

    send_fn(text, parse_mode) is the actual Telegram call, abstracted so
    this same logic serves both an incoming-message reply (message.
    reply_text) and the reminder ticker's proactive send (bot.
    send_message) without duplicating it."""
    html_text = markdown_to_telegram_html(text)
    for chunk in _split_for_telegram(html_text):
        try:
            await send_fn(chunk, "HTML")
        except BadRequest as e:
            logger.warning(f"HTML parse_mode rejected, falling back to plain text for this chunk: {e}")
            await send_fn(strip_telegram_html(chunk), None)


async def _reply(message, text: str) -> None:
    """reply_text, formatted for Telegram -- see _send_formatted."""
    await _send_formatted(lambda t, pm: message.reply_text(t, parse_mode=pm), text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the user and starts a fresh conversation, archiving (summarize
    + embed) whichever conversation was open before, in the background."""
    chat_id = str(update.effective_chat.id)
    await pipeline.start_new_conversation(chat_id, model_name=MODEL_NAME)
    await update.message.reply_text(
        "👋 Hello! I'm your local AI agent — I can search the web, remember our conversation, "
        "and read documents you send me."
    )


async def summarize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Summarizes + embeds the current conversation on demand, without
    closing it — you can keep chatting in it afterward."""
    chat_id = str(update.effective_chat.id)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        summary = await pipeline.summarize_open_conversation(chat_id, model_name=MODEL_NAME)
        await _reply(update.message, f"📝 Summary:\n\n{summary}")
    except ValueError:
        await update.message.reply_text("No active conversation yet — send a message first.")
    except Exception as e:
        logger.error(f"chat={chat_id} /summarize failed: {e}", exc_info=True)
        await update.message.reply_text("❌ Failed to summarize.")


async def switch_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows or changes which model this chat talks to. /switch_model with no
    args shows the current model + what's available; /switch_model <name>
    switches. The override is per-chat and lasts for the life of the process
    (or until switched again) — it doesn't survive a bot restart."""
    chat_id = str(update.effective_chat.id)
    available = sorted(GenerativeService.model_routing.keys())

    if not context.args:
        current = pipeline.get_model_override(chat_id) or MODEL_NAME
        await update.message.reply_text(
            f"Current model: {current}\n"
            f"Available: {', '.join(available) or '(none configured)'}\n"
            "Usage: /switch_model <model_name>"
        )
        return

    requested = context.args[0]
    if requested not in available:
        await update.message.reply_text(
            f"Unknown model {requested!r}. Available: {', '.join(available)}"
        )
        return

    pipeline.set_model_override(chat_id, requested)
    await update.message.reply_text(f"✅ Switched this chat to {requested}.")


async def context_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports how much of the live conversation-history budget this chat
    is using -- see pipeline.get_context_usage/count_context_tokens.
    Named for what it'll grow into: more than just context/token stats
    eventually, not just this one number forever.

    The token count is real, not the chars/4 estimate everywhere else in
    this project uses -- it costs an actual (tiny, max_tokens=1) model
    call, dispatched through the same Celery queue as everything else
    GPU-touching, so this can take a moment if a real turn is already in
    flight, and can time out or fail if the worker isn't running at all.
    Falls back to the chars/4 estimate in that case rather than showing
    an error for what's otherwise a working command."""
    chat_id = str(update.effective_chat.id)
    usage = await pipeline.get_context_usage(chat_id)
    if usage is None:
        await update.message.reply_text("No active conversation yet — send a message first.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    model_name = pipeline.get_model_override(chat_id) or MODEL_NAME
    try:
        tokens = await pipeline.count_context_tokens(chat_id, model_name)
        tokens_line = f"{tokens:,} tokens (real count, {model_name}'s own tokenizer)"
    except Exception as e:
        logger.warning(f"chat={chat_id} /context_stats real token count failed, falling back to estimate: {e}")
        tokens_est = round(usage["history_chars_used"] / 4)
        tokens_line = f"~{tokens_est:,} tokens estimated (real count unavailable: {e})"

    chars_used = usage["history_chars_used"]
    budget = usage["history_char_budget"]
    pct = round(100 * chars_used / budget, 1) if budget else 0.0

    lines = [
        "**Context usage**",
        "",
        f"{chars_used:,} / {budget:,} chars ({pct}%) — {tokens_line}",
        f"{usage['turns_included']} of {usage['turns_total']} turns included in live history",
    ]
    if usage["turns_included"] < usage["turns_total"]:
        lines.append("")
        lines.append(
            "Older turns aren't lost — I can still look them up on request "
            "(they're searchable, just not carried in every call by default)."
        )
    await _reply(update.message, "\n".join(lines))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists every command (_COMMANDS) and every capability that has no
    command -- just ask for it in conversation (_CAPABILITIES)."""
    command_lines = [f"{cmd} — {desc}" for cmd, desc in _COMMANDS]
    capability_lines = [f"• **{name}** — {desc}" for name, desc in _CAPABILITIES]
    await _reply(
        update.message,
        "**Commands**\n\n" + "\n".join(command_lines) +
        "\n\n**Just ask — no command needed, these work in normal conversation:**\n\n" +
        "\n".join(capability_lines) +
        "\n\nAnything else you send me is treated as a message to the agent."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs the tool-calling agent on the user's text message. Telegram only
    ever sees the final answer; everything else is logged/persisted."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    feedback = TelegramFeedback(context.bot, chat_id, action="typing")
    try:
        # No explicit model_name here: pipeline resolves this chat's
        # /switch_model override (if any) before falling back to MODEL_NAME.
        result = await pipeline.handle_incoming_message(
            user_id=str(user_id), chat_id=str(chat_id), text=update.message.text, feedback=feedback,
        )
        await _reply(update.message, result["output"])
        logger.info(f"user={user_id} chat={chat_id} tokens_used={result['tokens_used']}")
    except Exception as e:
        logger.error(f"chat={chat_id} failed: {e}", exc_info=True)
        await update.message.reply_text("❌ Something went wrong processing your request.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs the agent on an uploaded document (+ optional caption as the
    message text). The document is extracted to markdown (marker-pdf) and
    made available to the model via the read_document tool."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    document = update.message.document
    caption = update.message.caption or f"I've uploaded a document: {document.file_name}"

    feedback = TelegramFeedback(context.bot, chat_id, action="upload_document")
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        file_bytes = bytes(await telegram_file.download_as_bytearray())

        result = await pipeline.handle_incoming_message(
            user_id=str(user_id),
            chat_id=str(chat_id),
            text=caption,
            attachments=[Attachment(document.file_name, file_bytes)],
            feedback=feedback,
        )
        await _reply(update.message, result["output"])
        logger.info(
            f"user={user_id} chat={chat_id} uploaded {document.file_name} "
            f"tokens_used={result['tokens_used']}"
        )
    except Exception as e:
        logger.error(f"chat={chat_id} document handling failed: {e}", exc_info=True)
        await update.message.reply_text("❌ Something went wrong processing your document.")


async def _hook_ticker(app: Application) -> None:
    """Background loop (started from _on_startup): every HOOK_TICK_SECONDS,
    fires every active hooks row whose next_trigger_at has passed, via
    whichever handler is registered for its kind (llm_engine.hooks.
    HOOK_HANDLERS -- see llm_engine/hooks/registry.py). A kind with no
    registered handler (e.g. one that used to exist and was removed) logs
    an error every tick rather than silently deactivating the row --
    losing a hook silently, with no trace of why it stopped firing, is a
    worse failure mode than a noisy log.

    A per-row failure is caught and logged, never allowed to escape --
    one broken hook must not take down every other user's hooks, or stop
    this loop from ever checking again. Same for the outer tick itself
    (e.g. a transient DB hiccup): logged, then just tries again next tick
    rather than crashing the whole process.
    """
    while True:
        try:
            due = await get_due_hooks(datetime.now(timezone.utc))
            for row in due:
                try:
                    handler = HOOK_HANDLERS.get(row["kind"])
                    if handler is None:
                        logger.error(f"hook id={row['id']} kind={row['kind']!r} has no registered handler -- skipping")
                        continue

                    result = await handler(row)
                    if result.message:
                        await _send_formatted(
                            lambda t, pm, cid=row["chat_id"]: app.bot.send_message(chat_id=cid, text=t, parse_mode=pm),
                            result.message,
                        )
                    logger.info(
                        f"hook id={row['id']} kind={row['kind']} user={row['user_id']} fired "
                        f"sent={bool(result.message)}"
                        + (f" deferred_to={result.reschedule_to.isoformat()}" if result.reschedule_to else "")
                    )

                    if result.reschedule_to:
                        # The handler deliberately skipped its real work
                        # this tick (e.g. hooks.reminder finding the user
                        # in a calendar meeting) and wants another try at
                        # a specific time -- overrides the normal
                        # recurrence/deactivation math entirely, for both
                        # one-shot and recurring hooks: a deferral isn't
                        # "this cycle is done," it's "try again shortly."
                        await mark_hook_fired(row["id"], result.reschedule_to)
                    else:
                        recurrence = row["recurrence_seconds"]
                        if recurrence:
                            # Step forward past "now" rather than just once --
                            # if this process was down past one whole
                            # interval, a daily reminder shouldn't fire in a
                            # rapid-fire burst catching up, just resume on
                            # schedule from here.
                            next_trigger_at = row["next_trigger_at"] + timedelta(seconds=recurrence)
                            now = datetime.now(timezone.utc)
                            while next_trigger_at <= now:
                                next_trigger_at += timedelta(seconds=recurrence)
                            await mark_hook_fired(row["id"], next_trigger_at)
                        else:
                            await mark_hook_fired(row["id"], None, deactivate=True)
                    if row["kind"] == "reminder":
                        await render_plan_markdown(row["user_id"])
                except Exception as e:
                    logger.error(f"hook id={row.get('id')} failed: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"hook ticker tick failed: {e}", exc_info=True)
        await asyncio.sleep(HOOK_TICK_SECONDS)


async def _on_startup(app: Application) -> None:
    set_main_loop(asyncio.get_running_loop())
    await init_schema()
    logger.info("Memory database schema ready.")
    asyncio.create_task(_hook_ticker(app))
    logger.info(f"Hook ticker started (every {HOOK_TICK_SECONDS}s).")


def main():
    """Starts the bot loop."""
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("summarize", summarize_command))
    app.add_handler(CommandHandler("switch_model", switch_model_command))
    app.add_handler(CommandHandler("context_stats", context_stats_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"Bot active. Model: {MODEL_NAME}")
    app.run_polling()

if __name__ == '__main__':
    main()
