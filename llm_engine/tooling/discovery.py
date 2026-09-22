from typing import Literal

try:
    from .registry import tool
except ImportError:
    from registry import tool

# The name is matched directly by tgi.GenerativeService._run_tool_loop,
# which intercepts a call to it before normal dispatch and appends the
# requested categories' real tool schemas to its own local `tools` list
# instead of calling this function's body -- same interception-before-
# dispatch shape as control_tools.CONTINUE_RESEARCH_TOOL. This function
# exists only so @tool can introspect its signature/docstring to build
# the schema the model actually sees. tgi.py imports this constant and
# TOOL_CATEGORIES rather than defining its own, so they can't drift out
# of sync.
DISCOVER_TOOLS_NAME = "discover_tools"

# Category -> tool names. The default (core) toolset -- web_search,
# recall_tool_results, the memory_tools.py set, and continue_research --
# is defined in pipeline.CORE_TOOLS, not here: this module only owns the
# tools that start OUT OF SIGHT and get revealed on demand. Adding a new
# discoverable connector later (e.g. a Calendar one) is: add its tools'
# module to tooling/__init__.py as usual, add one new entry here, and add
# its name to discover_tools' own Literal type + docstring below -- no
# other file needs to change.
TOOL_CATEGORIES: dict[str, list[str]] = {
    "gmail": [
        "check_gmail", "draft_gmail_reply", "schedule_gmail_check",
        "list_gmail_hooks", "cancel_gmail_hook",
    ],
    "literature": ["search_arxiv", "search_pubmed", "read_paper", "save_finding"],
    "documents": ["search_documents", "search_document_chunks", "read_document"],
    "reminders": ["schedule_reminder", "list_scheduled_reminders", "cancel_reminder"],
    "database": ["query_database"],
    "calendar": ["list_calendar_events", "create_calendar_event"],
}


@tool
def discover_tools(
        categories: list[Literal[
            "gmail", "literature", "documents", "reminders", "database", "calendar",
        ]],
) -> dict:
    """
    Reveals more tools for the rest of this turn. Your default toolset
    only covers web search, memory recall, and continuing a multi-step
    task -- call this first whenever the user's request touches one of
    these less-common areas, to get the specific tools for it:

    gmail      -- checking/searching email, drafting replies (draft-only,
                  never sends), scheduling ongoing email watches (5 tools).
    literature -- searching arXiv/PubMed, reading a paper's full record,
                  saving one as a bookmark (4 tools).
    documents  -- searching the user's uploaded documents, drilling into
                  one document's chunks, reading full extracted text
                  (3 tools).
    reminders  -- scheduling/listing/cancelling reminders and habits that
                  fire on their own later (3 tools).
    database   -- running a read-only SQL query against the user's own
                  data for exploratory questions the other search tools
                  don't answer directly (1 tool).
    calendar   -- listing upcoming Google Calendar events (also useful
                  for checking conflicts) and creating new events
                  (2 tools).

    Pass one or more categories at once if you already know you'll need
    more than one -- e.g. both "documents" and "literature" for a request
    that spans the user's own uploads and published papers. Revealed
    tools stay available for the rest of this turn; no need to call this
    again for a category you've already revealed.
    """
    return {"acknowledged": True}  # never actually runs -- see module docstring
