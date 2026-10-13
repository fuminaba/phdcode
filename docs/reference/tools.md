# MCP tools

The tools of `phdcode-mcp`, from their docstrings in `llm_engine/mcp_server.py`. The agent reads the same text. The server instructions, which tell the agent how to use the tools together, are the constant `INSTRUCTIONS` in the same file.

::: llm_engine.mcp_server
    options:
      show_root_heading: false
      show_root_toc_entry: false
      show_source: false
      heading_level: 2
      members:
        - search_sources
        - get_paper
        - search_papers
        - search_chunks
        - get_chunks
        - list_saved_searches
        - save_paper
        - ingest_pdf
        - add_saved_search
        - cancel_saved_search
