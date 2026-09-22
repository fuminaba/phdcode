# Telegram commands

User-facing reference for every slash command the bot understands, plus
everything it can do that has *no* command at all — those just work by
asking for them in normal conversation, and the model decides which tool
to call. Mirrors `webhook_server.py`'s `_COMMANDS`/`_CAPABILITIES` lists,
which are also what `/help` replies with directly in Telegram — if you
change either, update both places.

| Command | Usage | What it does |
|---|---|---|
| `/start` | `/start` | Starts a new conversation. The previous one (if any) is archived — summarized and embedded — in the background, so it stays searchable via the agent's own memory/recall tools even after you leave it. |
| `/summarize` | `/summarize` | Summarizes and embeds the **current** conversation on demand, without closing it — you can keep chatting in the same thread afterward. Useful for checkpointing a long conversation. |
| `/switch_model` | `/switch_model` (no args) | Shows which model this chat is currently using and which models are available to switch to. |
| `/switch_model <name>` | e.g. `/switch_model gpt-oss-20b-114k` | Switches this chat to the named model. The override is per-chat and lasts for the life of the running process (or until switched again) — it does **not** survive a bot restart, at which point the chat falls back to the server's default `MODEL_NAME`. |
| `/context_stats` | `/context_stats` | Shows how much of the live conversation-history budget (`MAX_HISTORY_CHARS`) this chat is currently using — chars/tokens-estimate used, and how many of the conversation's turns are actually included in what the model sees each call. Named for what it'll grow into — more than just context/token stats eventually, not just this one number forever. |
| `/help` | `/help` | Shows this command list, inline in Telegram. |

## Just ask — no command needed

These work in normal conversation; the model decides on its own whether
to call the relevant tool.

| Capability | How to use it |
|---|---|
| Web search | Ask something that needs current/external information. |
| Memory | Ask what was discussed earlier — this conversation, or a past one (the agent's own recall tools cover both). |
| Documents | Send a file (PDF, etc.) with an optional caption, then ask questions about it. |
| Literature search | Ask it to search arXiv or PubMed for papers on a topic. |
| Reminders & habits | Ask it to remind you about something, once or on a recurring schedule (e.g. "remind me to take my vitamins every day at 8am"). It messages you proactively — you don't need to be chatting. Ask what's currently scheduled, or to cancel one, any time. |

## Notes

- Model names accepted by `/switch_model` come from the `LLM_ROUTING` table in
  `.env` — anything not listed there gets rejected with the current list of
  valid names.
- Document uploads don't need a command: attach a file (with an optional
  caption) and the agent reads it via its document tools automatically.
- There's no `/stop` or `/cancel` — a running turn finishes on its own; there
  is currently no way to interrupt one mid-flight from Telegram.
