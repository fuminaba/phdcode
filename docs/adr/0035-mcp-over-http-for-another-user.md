# ADR-0035: MCP over HTTP with a token, for a client of another Linux user

**Status:** Accepted — 2026-11-12
**Amends:** [ADR-0016](0016-mcp-server-is-the-only-integration.md) (its rejected alternative "MCP over HTTP")

## Context

ADR-0016 chose stdio and rejected HTTP: HTTP "needs a service that runs all
the time", and a warm connection pool does not justify one. That premise
assumed that the harness runs as the user who owns the deployment.

That is no longer true. An agent harness (autopilot) runs each of its domains
as its own Linux user. That user cannot read the `.env` of the deployment
(the database password), so it cannot start the server over stdio. A second
deployment, with its own clone, `.env` and database, serves a second domain.

Two facts of the code shape the design (checked 2026-11-12):

- Each module calls `load_dotenv()` with no path, so the `.env` comes from the
  folder of the imported code. The environment `alira` holds an editable
  install of one clone. Started from any other folder, the server imports
  that clone, and its `.env` gives the database of the other deployment,
  with no error.
- `%E/phdcode/env` (the env file of the units) is a link to the `.env` of
  one clone. Its `DATABASE_URL` wins over the `.env` of any other clone.

## Decision

1. **With `PHDCODE_MCP_PORT` set, `phdcode-mcp` serves MCP over streamable
   HTTP on 127.0.0.1**, at `/mcp`. Without it, stdio works as before.
2. **Each request needs `Authorization: Bearer <PHDCODE_MCP_TOKEN>`**, or the
   server answers 401. The comparison is constant-time. Every local user can
   reach the port, so the token is the only check: each deployment has its
   own token.
3. **The server refuses to start** without a token of 32 characters or more,
   or when its code is not from `PHDCODE_DIR`. At start it logs the code
   folder, the database name and the papers folder.
4. **Over HTTP, no tool reads a local file for the client.** `ingest_pdf`
   takes a reference only: the server runs as the owner of the deployment,
   and a local path would give the client each PDF that the owner can read,
   also from the papers folder of the other deployment.
5. **One template unit, `phdcode-mcp@<name>.service`**, for each deployment.
   It reads only `%E/phdcode/mcp-<name>.env` (`PHDCODE_ENV`, `PHDCODE_DIR`,
   `PHDCODE_MCP_PORT`, `PHDCODE_MCP_TOKEN`), never `%E/phdcode/env`, and
   starts `python -m llm_engine.mcp_server` in `PHDCODE_DIR`. The rest of
   the settings come from the `.env` of that clone.

## Consequences

- A client of another user connects with a URL and a header, and needs no
  file of the deployment.
- One server process serves all sessions. The lifespan (the schema and the
  pool) runs once for each process, not for each session (mcp 2.3.0,
  `streamable_http_manager.py`).
- A server that runs all the time holds a few database connections.
- Over HTTP, a client adds a full-text paper only from a reference whose
  PDF the server downloads.
- The token is a secret in a file of the deployment's user. To revoke it,
  change it and restart the unit.
- The other units (the dashboard, the sweep) still read `%E/phdcode/env`,
  so they serve one deployment only. A second deployment that needs the PDF
  queue or the summaries needs its own units for them.

## Alternatives rejected

- **The built-in OAuth support of the SDK (`AuthSettings`, `TokenVerifier`).**
  It needs an issuer URL and advertises OAuth metadata, for one static token.
- **A second Postgres role and stdio for each client user.** The client user
  then holds the database password, and the server runs with the
  permissions of the client.
- **One clone, with the per-deployment settings in the env file of the unit.**
  Each setting that the env file forgets falls back to the `.env` of the
  other deployment, with no error.
