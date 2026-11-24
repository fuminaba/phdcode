# ADR-0038: A read-only replica of a library for a team

**Status:** Accepted — 2026-11-24

## Context

A library of phdcode can serve a team (ADR-0035: a second deployment), but the machine of the writer is not
reachable from the hosts of the team. The writer's work (the sweep, the parse, the summaries, the uploads) stays
on that machine: it needs a GPU or GPU VMs. The team's agents need only to search and read, from a host of the
team with no GPU and no local model; each person's own MCP client is the agent.

## Decision

1. **The writer publishes a dump** (`deploy/replica/publish.sh`, `phdcode-publish@<name>.timer`, daily): `pg_dump -Fc`
   without the data of `tool_calls`, uploaded to one fixed blob with the `az login` of the writer, only when a
   fingerprint of the library changed (the counts of papers, chunks and keywords, the newest `updated_at`).
2. **The replica** (`deploy/replica/docker-compose.yml`): Postgres with pgvector, Ollama for the query embeddings
   (the versions of the writer), and the MCP server, in an image with no torch and no marker. Every port is on
   127.0.0.1; the MCP server uses the host network.
3. **The swap** (`deploy/replica/sync.sh`, daily): a download with the ETag (a read SAS on that one blob), a restore
   into `phdcode_next`, then the swap of the databases only if the paper count did not fall by more than 10 %.
4. **Read tools only:** `PHDCODE_TOOLS` lists `search_sources`, `get_paper`, `search_papers`, `search_chunks`,
   `get_chunks`. A write on the replica would be lost at the next swap.
5. **Personal tokens:** `PHDCODE_MCP_AUTH_URL` replaces the one shared token: the server sends the bearer token of
   each request to that URL (an endpoint of the team that knows its people) and accepts only a 200. A good token
   is kept 10 min; any failure refuses. `PHDCODE_MCP_HOSTS` adds the public name of the proxy to the hosts that
   the MCP SDK accepts (it answers 421 to any other Host header).

## Consequences

- The replica can be one day old. It has no web page and no figures: the dump holds the database only.
- Rejected: a tunnel from the hosts of the team to the writer; the writer's work on the team's GPU VMs now
  (later, when the team wants uploads and its own parse).
- Measured in a rehearsal on the writer machine (2026-11-24): the dump 48 MB (229 papers, 10,775 chunks); the
  replica uses about 310 MB of memory when idle; a query embedding of the replica equals the writer's (cosine
  1.0); a dump with 12 papers was refused, and the live copy kept 229.
