import re
from typing import Any

try:
    from .registry import tool
except ImportError:
    from registry import tool

try:
    from ..context import current_user_id, run_on_main_loop
except ImportError:
    from context import current_user_id, run_on_main_loop

_MAX_ROWS = 50

# Real enforcement is the alira_agent_readonly role's grants (SELECT on the
# agent_readonly views only -- see schema.sql); this is a first, cheap line
# of defense that rejects an obviously-wrong query before it ever reaches
# the database.
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CREATE|CALL|COPY|VACUUM|EXECUTE|DO|MERGE)\b",
    re.IGNORECASE,
)

@tool
def query_database(sql: str) -> list[dict[str, Any]] | str:
    """
    Runs a read-only SQL query against the current user's own data, for
    exploratory questions the other search tools don't answer directly --
    counting, filtering by date, joining across documents and chunks, etc.

    Available views (each already scoped to the current user -- never
    filter by user_id yourself, it's redundant and the views don't expose
    a way to see anyone else's data anyway):
      agent_readonly.my_conversations(id, user_id, chat_id, summary, created_at, updated_at)
      agent_readonly.my_conversation_turns(id, conversation_id, user_id, user_prompt, result, model_name, created_at)
      agent_readonly.my_documents(id, user_id, conversation_id, filename, title, authors, summary, methods_summary, results_summary, created_at, updated_at)
      agent_readonly.my_document_chunks(id, document_id, chunk_index, content, start_line, end_line, created_at)
      agent_readonly.my_document_keywords(document_id, word)

    Only a single SELECT statement is permitted -- no INSERT/UPDATE/DELETE,
    no schema changes, nothing else. Results are capped at 50 rows.
    """
    user_id = current_user_id.get()
    if not user_id:
        return "No active user context."

    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        return "Only a single statement is allowed."
    if not re.match(r"^\s*SELECT\b", stripped, re.IGNORECASE):
        return "Only SELECT statements are allowed."
    if _FORBIDDEN_KEYWORDS.search(stripped):
        return (
            "That statement isn't allowed -- "
            "only read-only SELECT queries against the agent_readonly views."
        )

    try:
        from ..memory.db import run_readonly_query
    except ImportError:
        from memory.db import run_readonly_query

    try:
        return run_on_main_loop(
            run_readonly_query(user_id, stripped, limit=_MAX_ROWS)
        )
    except Exception as e:
        return f"Query failed: {e}"
