try:
    from .registry import tool
except ImportError:
    from registry import tool

# The name is matched directly by tgi.GenerativeService._run_tool_loop,
# which intercepts a call to it before normal dispatch and extends its own
# step budget instead of calling this function's body -- this function
# exists only so @tool can introspect its signature/docstring to build the
# schema the model actually sees. tgi.py imports this constant rather than
# defining its own, so the two can't drift out of sync.
CONTINUE_RESEARCH_TOOL = "continue_research"


@tool
def continue_research(reason: str) -> dict:
    """
    Requests more steps to keep working on the current task -- use this
    when you're in the middle of a multi-step investigation (e.g. an
    iterative literature search: searching, reviewing results, tweaking
    keywords, searching again) and are about to run out of room, but more
    steps would meaningfully improve the answer. State briefly why.

    This is subject to a hard ceiling you cannot exceed -- if your request
    is denied, wrap up with what you already have instead of asking again.
    """
    return {"acknowledged": True}  # never actually runs -- see module docstring
