# ADR-0005: Integrate via dcode extensions, not MCP

**Status:** Accepted — 2026-09-22

## Context

ALiRA2's domain capabilities must reach dcode. Two mechanisms exist:

- **MCP** — expose them as a server dcode connects to (`--mcp-config`).
- **Extensions** — `deepagents_code.extensions.api` offers
  `register_tool(tool: BaseTool | Callable)`, plus `register_middleware` and
  `register_backend_route`, loaded with `-e`.

The tool dependency set is light — `asyncpg`, `pgvector`, `tavily`,
`google-api-python-client`, `requests`, `pydantic`, `dotenv` — with no torch and
no marker-pdf, so it installs cleanly into dcode's Python 3.13 environment.

An earlier draft favoured MCP over HTTP, on the grounds that a long-lived server
keeps the Postgres pool and OAuth credentials warm. That argument was wrong:
connection setup is milliseconds against ~7s of prefill per run.

## Decision

Integrate via extensions. Register ALiRA2's tools as Python objects in dcode's
process.

## Consequences

- No transport, no serialization, no server to keep alive. Tools are the same
  functions the adapters call directly.
- `register_middleware` becomes available, which ADR-0010 uses for observability
  and is a better home for cross-cutting concerns than shell hooks.
- The integration is dcode-specific. Switching harnesses later means rewriting
  this layer — accepted deliberately: hedging against a switch with no plan
  behind it is the duplication this design set out to remove.
- dcode's version is pinned. The CLI labels the project-extension trust path
  experimental, so upgrades are tested rather than taken blindly.
- Tool dependencies are installed twice — once in the `alira` conda env for the
  adapters, once in dcode's uv env. Acceptable: they are light and pure-Python.

## Alternatives rejected

- **MCP over HTTP.** Portable across harnesses and decoupled from dcode's
  release cycle, but buys portability for a migration that is not planned, at
  the cost of a server to run and a serialization boundary.
- **MCP over stdio.** Same serialization cost, and re-spawns the server per run
  without the portability benefit being any larger.
