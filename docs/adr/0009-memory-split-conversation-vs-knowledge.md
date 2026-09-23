# ADR-0009: Memory split — conversation vs knowledge

**Status:** Accepted — 2026-09-22

## Context

Adopting dcode creates two memory systems. dcode owns threads, stored under
`DEEPAGENTS_HOME`. ALiRA2 owns `conversations`, `conversation_turns`, summaries
and embeddings in Postgres with pgvector, plus `documents` and
`document_chunks`. Left alone, these drift, and it stops being clear which one
answers "what did we decide about X".

Cross-session recall is one of ALiRA2's genuinely good features and should not
be lost to the migration.

## Decision

Split by kind:

- **dcode owns conversation.** Threads on disk are the record of what was said.
- **Postgres owns knowledge** — documents, papers, findings, embeddings, and the
  hybrid vector/full-text search over them.

Two bridges keep recall working:

1. Tools that read dcode's conversation files directly, mirroring the existing
   `read_conversation(file_path)` tool, which already reads markdown transcripts
   from disk.
2. A `Stop` middleware (ADR-0010) that writes each thread's summary and topics,
   with an embedding, into Postgres — so semantic search across past sessions
   keeps working without duplicating full transcripts.

## Consequences

- One sentence answers where things live: *conversation is dcode's, knowledge is
  ours.* That was the clarity this session was after.
- `search_memory` and `search_turns` change meaning: they search summaries and
  knowledge, not raw turn rows. `recall_turn_detail` loses its backing table.
- Summaries are lossy by construction. Full fidelity is available via the
  conversation-file tools, at the cost of knowing which thread to open.
- Postgres stops being written on every turn, which removes the `persist_turn`
  path that once cost a 352-second delay.

## Alternatives rejected

- **Mirror every dcode turn into Postgres.** Preserves everything and keeps two
  authoritative records in sync forever — the drift this ADR exists to prevent.
- **Keep both systems fully.** Duplication with no rule for which wins.
