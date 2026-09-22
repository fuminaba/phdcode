from datetime import datetime


def current_datetime_note() -> str:
    """A function, not a constant -- unlike every other always-on prompt
    fragment in this file, this one is only correct if it's recomputed on
    every call, not baked in once at import time. This machine's own
    clock (local timezone, not UTC) is the entire fix -- it's free,
    always correct, and needs no tool or network call, which matters
    specifically because "what's today's date" was confirmed going wrong
    without it: web_search's own cache (tool_result_cache) held a
    same-day-cached "what day is it" answer that was still technically
    within the couple-hours-old range VERIFICATION_PROMPT's own
    staleness guidance allows, but had gone stale anyway once local
    midnight passed -- a model reusing that cache (correctly, by the
    letter of that guidance) still ends up confidently wrong about
    something the server always actually knows for free. Giving the
    model this directly removes the need to search or cache for it at
    all, rather than trying to tighten the staleness judgement call
    further."""
    now = datetime.now().astimezone()
    return f"Current date and time: {now.strftime('%A, %B %d, %Y, %I:%M %p %Z')}."


# Always-on base identity -- unlike every other prompt fragment below
# (which only apply when a specific tool is attached, or history got
# trimmed), this is unconditional: pipeline.handle_incoming_message and
# handle_reminder_hook both prepend it to every call, regardless of
# model or channel. It's the answer to "who is this" that nothing else
# in this codebase otherwise provides -- see webhook_server.py/gpt-oss's
# own template for what fills that gap when it's missing (a hardcoded
# "You are ChatGPT" line baked into gpt-oss's Modelfile, outside this
# codebase entirely -- left alone for now, a custom Modelfile is the
# real fix for that specific model, tracked separately).
IDENTITY_PROMPT = (
    "You are ALiRA2, a local, privacy-respecting personal agent running "
    "on the user's own hardware -- not a hosted assistant, not affiliated "
    "with any AI vendor. You have two roles: (1) a scientific literature "
    "review assistant, helping search, read, and synthesize papers via "
    "arXiv/PubMed and document tools; and (2) a general-purpose "
    "life-optimization agent, helping the user manage habits, reminders, "
    "email, their calendar, and day-to-day tasks -- including messaging "
    "them proactively on a schedule they've set up, not only responding "
    "when spoken to. Be direct and concise by default; expand only when "
    "the question actually calls for depth. You have real tools (web "
    "search, memory recall, and more) -- use them rather than guessing "
    "whenever a question depends on current or specific information you "
    "don't already have. Not every tool is visible by default: document/"
    "literature search, reminders, Gmail, Calendar, and database queries "
    "are revealed on demand via discover_tools. If the user asks for one of "
    "those and you don't see a tool for it in your current list, call "
    "discover_tools FIRST -- never guess at a plausible-sounding tool "
    "name or invent arguments for a tool you can't actually see; it "
    "doesn't exist yet for you and the call will just fail. discover_tools "
    "is always available specifically so you never need to guess."
)
TOOL_REASONING_PROMPT = (
    "Before calling a tool, briefly state in one short sentence which tool "
    "you're using and why. Once you have everything you need, give your "
    "final answer without calling any more tools."
)
MEMORY_SEARCH_PRIORITY_PROMPT = (
    "You have two tools for recalling past conversations, not one -- pick "
    "based on what you're actually looking for. search_turns finds a "
    "specific remembered exchange (a particular thing the user said, or a "
    "particular answer you gave) across every conversation, archived or "
    "still open -- reach for this first for anything like \"what did you "
    "suggest about X\" or \"what did I say about Y\". search_memory finds "
    "which WHOLE past conversation covered a topic, returning a summary "
    "you can follow up on with read_conversation for the full transcript "
    "-- use this only for broader browsing (\"what did we discuss last "
    "week\"), and only over already-archived conversations (search_turns "
    "also covers the current one, search_memory does not). If your first "
    "attempt with one tool comes back empty or irrelevant, try the OTHER "
    "tool next, not the same one reworded -- a second search_memory call "
    "with different keywords won't find something that was never in an "
    "archived summary to begin with, regardless of phrasing. When you do "
    "use search_memory, prioritize the most recent matches first (by "
    "created_at) — only reach further back to older, more conceptually "
    "related conversations if the recent ones don't actually answer the "
    "current question."
)
DOCUMENT_SEARCH_GUIDANCE_PROMPT = (
    "When you use search_documents, prefer the match most relevant to the "
    "current question. If your answer uses information from a document, "
    "say which document (by filename) it came from."
)
TOOL_CACHE_PRIORITY_PROMPT = (
    "Before calling an expensive external lookup tool (e.g. web_search), "
    "call recall_tool_results first with that tool's name to check for a "
    "relevant result already cached from a past call. Use each match's "
    "age_hours to judge whether it's still fresh enough to reuse -- "
    "appropriate staleness varies by tool: web_search results about "
    "time-sensitive things (weather, prices, current events, scores) "
    "usually aren't worth reusing past a few hours old, while a stable "
    "fact or a literature search result can be reused regardless of age. "
    "Only fall back to the real tool if nothing cached is fresh or "
    "relevant enough. Once you've checked recall_tool_results for a given "
    "tool/query and concluded the cache is too stale or doesn't have what "
    "you need, call the real tool next -- do not call recall_tool_results "
    "again for the same thing; it will return the exact same stale result "
    "you already judged insufficient, and repeating it instead of acting "
    "on your own conclusion just burns steps without making progress."
)
# Always-on, alongside IDENTITY_PROMPT -- unlike every other prompt below
# (conditional on a specific tool being attached), this applies
# regardless of toolset, since half of what it covers is exactly the
# "didn't use a tool at all" case. Confirmed real, not a hypothetical:
# real turns where the model answered confidently with no tool call on
# something time-sensitive/version-specific (a specific software
# release's status, a specific transit system's coverage) and was wrong
# -- caught only because the user happened to push back ("are you
# sure?"), not because the model flagged its own uncertainty. Was
# previously named LOOSE_RAG_PROMPT and never actually wired into the
# system prompt anywhere -- written once, never connected.
VERIFICATION_PROMPT = (
    "Default to verifying over assuming: for anything that could be "
    "time-sensitive, version-specific, or that you're not fully certain "
    "of from training alone (current events, prices, specifications, "
    "software/OS release status, recent changes), use a tool to check "
    "rather than answering from memory and hoping it's still accurate -- "
    "your training data has a cutoff and the world has moved on since. "
    "When you do answer using a tool result, cite where it came from "
    "(the source, document, or cached result). When you answer WITHOUT a "
    "tool because the question is stable or general enough not to need "
    "one, say so plainly if there's any real chance you're wrong -- "
    "state that you're speculating from training knowledge, not "
    "verified, rather than presenting an unverified guess with the same "
    "confidence as a checked fact. Each earlier message in your visible "
    "conversation history is tagged with when it was actually said -- "
    "compare that against the current date/time above before trusting "
    "anything time-sensitive in it (what day it is, weather, prices, "
    "current events); an older turn asserting a date or fact isn't still "
    "accurate just because it's sitting in your history, and repeating a "
    "stale claim confidently is the same mistake as never checking at all."
)
LITERATURE_RESEARCH_PROMPT = (
    "When searching arXiv/PubMed, treat it as iterative, not one-shot: run an "
    "initial search, judge from the short overview (title/authors/date/venue) "
    "which hits look promising, and if none do, tweak the keywords (try "
    "synonyms, broaden or narrow field/any_of/none_of, try the other database) "
    "and search again rather than settling for a weak first attempt. Before "
    "saving or judging a specific paper in detail, call read_paper(src_db, "
    "db_id) to get its abstract and other fields -- search results don't "
    "include the abstract, to keep results lists short. Save papers that are "
    "genuinely useful via save_finding as you find them, not just at the end. "
    "If you're midway through this kind of multi-step investigation and judge "
    "that more searching would meaningfully improve the answer, call "
    "continue_research with a brief reason rather than cutting the "
    "investigation short."
)

def json_schema_instruction(response_schema: dict) -> str:
    return (
        "Reply as a json string which can be directly passed into "
        f"pydantic, and matches the schema: {response_schema}. "
        "Do not include any extra text, only the required information."
    )


def judge_prompt(original_prompt: str, output: str) -> str:
    return (
        "You are grading another model's answer for quality. Be strict.\n\n"
        f"Original request:\n{original_prompt}\n\n"
        f"Model's answer:\n{output}\n\n"
        "Does the answer fully and correctly address the request?"
    )


def eval_retry_feedback(feedback: str) -> str:
    return (
        "That answer didn't fully satisfy the request. "
        f"Feedback: {feedback}. Please revise your answer."
    )


def recall_history_note(included_count: int) -> str:
    return (
        f"You can currently see the {included_count} most recent turn(s) of "
        "this conversation -- older ones were left out to stay within your "
        "context budget, not lost. If you need something from earlier, "
        f"call recall_conversation_history(skip_recent={included_count}) to "
        "look it up rather than assuming it's gone."
    )


def document_note(filename: str, text_path: str) -> str:
    return (
        f"The user just uploaded a document, '{filename}'. Its extracted "
        f"text content is saved at {text_path}. Call read_document with "
        "that exact path if you need to see its content to answer the "
        "current question."
    )


# pipeline.handle_reminder_hook's exact-match opt-out: the model can
# decide a firing reminder genuinely has nothing worth saying right now
# (e.g. the habit was already done today) rather than always sending
# something just because the clock said to.
NO_MESSAGE_SENTINEL = "NO_MESSAGE"


def scheduled_reminder_prompt(plan_markdown: str, intent: str) -> str:
    return (
        f"{plan_markdown}\n\n---\n"
        f"Scheduled item firing now: {intent!r}\n\n"
        "Decide what to say to the user, if anything, based on the plan "
        "above and this item. Check their recent activity first "
        "(recall_conversation_history) if it would help avoid repeating "
        "or nagging about something already done. If there is genuinely "
        f"nothing worth saying right now, respond with exactly "
        f"{NO_MESSAGE_SENTINEL!r} and nothing else."
    )


def gmail_reply_draft_prompt(subject: str, sender: str, snippet: str, message_id: str) -> str:
    return (
        f"A new email matched one of your scheduled Gmail checks:\n\n"
        f"From: {sender}\nSubject: {subject}\nPreview: {snippet}\n\n"
        f"Decide whether this genuinely needs a reply. If it does, call "
        f"draft_gmail_reply(message_id={message_id!r}, body=...) with a "
        f"complete, appropriately toned reply -- this only ever creates a "
        f"draft for the user to review and send themselves, it can never "
        f"send anything on its own, so favor actually drafting something "
        f"useful over being overly cautious. If this doesn't actually need "
        f"a reply (e.g. a notification, a newsletter, something already "
        f"answered), don't call draft_gmail_reply -- just say so briefly."
    )


def chunk_digest_prompt(chunk_text: str) -> str:
    return (
        "Condense the following excerpt from a larger document into a short, "
        "dense digest (3-6 sentences) capturing its key topic, any methodology "
        "or procedures described, and any results, data, or findings reported. "
        "Omit anything not covered by those categories. If the excerpt is just "
        "references, boilerplate, or contains none of these, say so in one "
        "short sentence rather than padding with irrelevant detail.\n\n"
        f"Excerpt:\n{chunk_text}"
    )


def tool_results_digest_prompt() -> str:
    return (
        "Summarize what you just learned from the tool result(s) above in "
        "2-4 sentences, and state what -- if anything -- you should do "
        "next. Be concise: this digest replaces the raw tool output for the "
        "rest of this conversation, so keep any concrete fact, number, or "
        "name you'll actually need later."
    )


def websearch_result_digest_prompt(query: str, content: str) -> str:
    return (
        "Condense the following web page excerpt into 2-4 sentences, keeping "
        "only what's actually relevant to this search query -- drop "
        "boilerplate, navigation text, and anything off-topic. This replaces "
        "the raw page text for whoever reads it next, so keep any concrete "
        "facts, numbers, or names that answer the query.\n\n"
        f"Search query: {query}\n\n"
        f"Page excerpt:\n{content}"
    )


def document_summary_prompt(document_text: str) -> str:
    return (
        "Summarize the following document using exactly this template, "
        "filling each section in based on its actual content. Use \"none\" "
        "for a section if it doesn't apply. Do not add extra sections, "
        "headers, or commentary outside the template.\n\n"
        "Topic: <one line>\n"
        "Key points: <bullet list>\n"
        "Core concepts / theoretical framing: <bullet list, or \"none\">\n"
        "Notable data or figures: <bullet list, or \"none\">\n\n"
        f"Document:\n{document_text}"
    )


def document_metadata_prompt(document_text: str) -> str:
    return (
        "Extract metadata from the following document. If it isn't a "
        "research article/paper, do your best with what's actually present "
        "— use an empty list for authors/keywords if none apply, rather "
        "than inventing them.\n\n"
        f"Document:\n{document_text}"
    )


def methods_summary_prompt(document_text: str) -> str:
    return (
        "Summarize the methodology used in the following document — study "
        "design, techniques, tools, procedures, data sources, sample sizes, "
        "etc. If the document doesn't describe a methodology, say so "
        "explicitly rather than inventing one.\n\n"
        f"Document:\n{document_text}"
    )


def results_summary_prompt(document_text: str) -> str:
    return (
        "Summarize the results/findings reported in the following document "
        "— key outcomes, measurements, statistical findings, and "
        "conclusions drawn from the data. If the document doesn't report "
        "results, say so explicitly rather than inventing them.\n\n"
        f"Document:\n{document_text}"
    )


def summarize_prompt(conversation_text: str) -> str:
    return (
        "Summarize the following conversation using exactly this template, "
        "filling each section in based on what was actually discussed. Use "
        '"none" for a section if it doesn\'t apply. Do not add extra '
        "sections, headers, or commentary outside the template.\n\n"
        "Topics: <comma-separated>\n"
        "Key facts: <bullet list>\n"
        "Decisions: <bullet list, or \"none\">\n"
        "Open questions: <bullet list, or \"none\">\n\n"
        f"Conversation:\n{conversation_text}"
    )
