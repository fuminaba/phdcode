# Hooks system + Gmail connector: implementation plan

**Status: built and live** (schema migrated, both hook kinds registered
and verified end-to-end against the real DB/model for reminders; Gmail
code complete but untestable until Google Cloud OAuth setup is done --
see "Remaining setup" at the bottom). Left in place as the design record;
update this file if the architecture changes rather than letting it go
stale.

## Context

The reminder system (`scheduled_messages`, `webhook_server.py`'s
`_reminder_ticker`, `pipeline.handle_scheduled_reminder`) only ever does one
thing when a trigger fires: compose a message via a scoped model call and
send it over Telegram. The user wants triggered actions that do more than
that -- checking Gmail for matching mail, drafting replies, eventually
Calendar sync -- with an explicit goal of making new integrations easy to
add, not a one-off special case each time. This plan generalizes the
existing scheduler into a `kind`-dispatched hooks system, using Gmail as the
first real connector. Calendar and anything else follow the same pattern
later; not designed in detail here beyond "the framework shouldn't preclude
it."

Two decisions already made (not open questions):
- **Rename `scheduled_messages` → `hooks`.** The table already carries an
  unused `kind` column and fully generic read/write functions -- renaming
  is mostly about not calling a table "scheduled_messages" once it fires
  actions that aren't messages.
- **Gmail only for now.** Calendar is explicitly out of scope for this pass.

## Architecture

- **`hooks` table** (renamed from `scheduled_messages`, `schema.sql`):
  same columns (`id, user_id, chat_id, kind, payload jsonb, next_trigger_at,
  recurrence_seconds, active, last_fired_at, created_at`), migrated via a
  guarded `ALTER TABLE ... RENAME` (idempotent, consistent with how
  `schema.sql` already handles change -- there's no migration framework,
  `init_schema()` just re-runs the whole file on every startup). `kind`
  gets a `CHECK (kind IN ('reminder', 'gmail_poll'))` constraint, rebuilt
  via unconditional `DROP CONSTRAINT IF EXISTS` + `ADD CONSTRAINT` each
  startup (not the one-time-only guard `documents.document_kind` uses --
  that pattern would never pick up new kinds added later, and `hooks.kind`
  is specifically meant to grow).

- **`llm_engine/hooks/` package** (new) -- a dispatch registry modeled
  directly on the existing `@tool` pattern in `tooling/registry.py`:
  `@hook_handler("kind_name")` registers an async handler into
  `HOOK_HANDLERS: dict[str, Callable]`. `llm_engine/hooks/__init__.py`
  hardcoded-imports each handler module, same convention as
  `tooling/__init__.py`. A small `HookResult(message: Optional[str])`
  dataclass replaces the ad hoc `skip_send` boolean, generalizing "nothing
  to send this time" beyond just reminders.
  - `llm_engine/hooks/reminder.py`: `pipeline.handle_scheduled_reminder` is
    renamed to `handle_reminder_hook`, logic otherwise untouched; this file
    is a thin `@hook_handler("reminder")` adapter translating its existing
    `skip_send` into `HookResult`.
  - `llm_engine/hooks/gmail.py`: `@hook_handler("gmail_poll")`, new.

- **`webhook_server.py`**: `_reminder_ticker` → `_hook_ticker`. Same
  polling loop, same per-row/per-tick error isolation and
  catch-up-safe recurrence stepping -- the only structural change is
  replacing the hardcoded `handle_scheduled_reminder` call with a
  `HOOK_HANDLERS[row["kind"]]` lookup. An unknown/removed `kind` logs an
  error every tick rather than silently deactivating the row (silent
  deactivation risks the user never noticing a hook stopped firing --
  accepted tradeoff is log noise, not lost hooks). `render_plan_markdown`
  stays reminder-only (`get_active_hooks(user_id, kind="reminder")`
  explicitly) rather than trying to represent every hook kind in one
  markdown plan.

- **Gmail connector**:
  - `llm_engine/tooling/_tooling_deps/gmail_client.py` (new) -- raw API
    client, same split already used for arXiv/PubMed
    (`_tooling_deps/arxiv_search.py` etc.): credential loading + refresh,
    `list_matching_messages`, `create_draft_reply`. **Drafts only, never
    sends** -- deliberate safety ceiling, not a v1 shortcut to relax later
    without a conscious decision.
  - `llm_engine/tooling/gmail_tools.py` (new) -- `@tool`-decorated,
    model-facing: `check_gmail`, `draft_gmail_reply` (on-demand, mid-chat),
    `schedule_gmail_check`/`list_gmail_hooks`/`cancel_gmail_hook` (direct
    analogs of `schedule_reminder`/`list_scheduled_reminders`/
    `cancel_reminder`, creating `kind="gmail_poll"` hooks instead). Added
    to `tooling/__init__.py`'s hardcoded import list, same one-line pattern
    every existing tool module already follows.
  - Poll flow (`hooks/gmail.py`): read `payload` (`query`, `on_match`:
    `"notify"|"draft"|"both"`, `last_seen_internal_date` watermark) → list
    new matches (capped per tick, same anti-burst discipline the existing
    recurrence-stepping already applies) → `"notify"` is pure string
    formatting, no LLM/Celery involved; `"draft"`/`"both"` call a new
    `pipeline.handle_gmail_reply_draft(...)`, structurally identical to
    `handle_reminder_hook` (synthetic persisted prompt, narrow toolset
    containing just `draft_gmail_reply` + read-only recall tools) -- this
    is the one point that actually reaches the Celery "chat" queue, via the
    exact same `generate_with_tools` → `call_model.apply_async` path
    everything else already uses. `payload["last_seen_internal_date"]`
    needs writing back every tick (new `update_hook_payload` in `db.py` --
    reminder payloads never needed this, they're set once and never
    mutated).

- **Credentials**: new `oauth_credentials` table (`user_id, service,
  access_token, refresh_token, expires_at, scopes, created_at,
  updated_at`, unique on `(user_id, service)`), plaintext -- consistent
  with the existing `TAVILY_TOKEN`-in-`.env` security posture, not a
  regression from it. `scripts/setup_google_oauth.py` (new, one-time,
  follows `backfill_turn_embeddings.py`'s existing ad-hoc-script shape):
  run locally from the user's own machine against the same
  `DATABASE_URL`, uses `google_auth_oauthlib.flow.InstalledAppFlow
  .run_local_server()` (standard library helper -- handles the browser
  consent + localhost redirect capture + token exchange in one call, no
  custom HTTP server needed) with narrow scopes (`gmail.readonly` +
  `gmail.compose` only, never `gmail.modify`).

## Real risk, not glossed over

**Google caps OAuth refresh tokens to 7 days while the app sits in
"Testing" publishing status**, which it will by default for a personal
project using restricted Gmail scopes. In practice this likely means
re-running `setup_google_oauth.py` roughly weekly, unless/until going
through Google's app verification once to lift the cap. `gmail_client`
must fail this gracefully -- one throttled Telegram notification
("Gmail access expired, rerun setup_google_oauth.py"), not silence and not
per-tick spam.

Other flagged, accepted tradeoffs:
- The ticker stays strictly sequential/single-process (unchanged from
  today) -- a slow Gmail call or LLM draft now blocks other due hooks,
  including other users' reminders, until it finishes. Already true today,
  gets more noticeable once a sometimes-slow external network dependency
  shares the loop. Not fixed in this pass (would need per-row timeouts / 
  `asyncio.gather`).
- `last_seen_internal_date` watermarking can miss/double-count under clock
  skew or Gmail's eventual consistency; acceptable given draft-only
  (never auto-send) semantics.
- One `oauth_credentials` row per `(user, service)` -- revoking access
  breaks every hook of that kind for that user at once, not selectively.

## What's done

All of it, code-wise:

1. `schema.sql` -- `scheduled_messages`→`hooks` renamed live (verified: old
   table gone, new one has the right columns/CHECK constraint), added
   `oauth_credentials`.
2. `memory/db.py` -- the five scheduling functions renamed
   (`create_hook`, `get_active_hooks` with an optional `kind` filter,
   `get_due_hooks`, `deactivate_hook` with an optional `kind` filter added
   for cross-kind-cancellation safety, `mark_hook_fired`), plus
   `update_hook_payload`, `get_oauth_credentials`,
   `upsert_oauth_credentials`, `update_oauth_access_token`.
3. `memory/__init__.py`, `memory/plans.py` (explicit `kind="reminder"`
   filter), `tooling/scheduling_tools.py` -- updated to the renamed
   functions, no behavior change.
4. `llm_engine/hooks/` package: `registry.py` (`HOOK_HANDLERS`,
   `@hook_handler`, `HookResult`), `reminder.py` (thin adapter over
   `pipeline.handle_reminder_hook`, renamed from `handle_scheduled_reminder`,
   logic untouched), `gmail.py`, `__init__.py`.
5. `webhook_server.py` -- ticker generalized to `_hook_ticker`, dispatches
   by `kind` via `HOOK_HANDLERS`, unknown-kind rows logged loudly rather
   than silently deactivated, `render_plan_markdown` stays reminder-only.
6. **Checkpoint passed**: scheduled a real reminder via the actual
   `schedule_reminder` tool, fired it via the real registry dispatch path,
   confirmed a genuine `NO_MESSAGE` model decision came back correctly
   translated to `HookResult(message=None)` -- not just "it imports."
7. Gmail: `_tooling_deps/gmail_client.py` (credential load/refresh,
   `list_matching_messages`, `create_draft_reply` -- draft-only, no send
   capability exists at all), `tooling/gmail_tools.py` (`check_gmail`,
   `draft_gmail_reply`, `schedule_gmail_check`, `list_gmail_hooks`,
   `cancel_gmail_hook`, registered in `tooling/__init__.py`),
   `prompts.gmail_reply_draft_prompt`, `pipeline.handle_gmail_reply_draft`,
   `hooks/gmail.py` (poll/dispatch, throttled auth-error notification).
8. `scripts/setup_google_oauth.py` -- one-time local OAuth flow.
9. `pyproject.toml` + installed: `google-api-python-client`,
   `google-auth-oauthlib`, `google-auth-httplib2`. `.env`: added
   `GMAIL_CLIENT_ID`/`GMAIL_CLIENT_SECRET` placeholders (empty).
   `HOOK_TICK_SECONDS` deliberately NOT added to `.env` -- it has a code
   default (60s) same as `REMINDER_TICK_SECONDS` always did, and falls
   back to reading that old name too.
10. Found and fixed one real bug along the way: `gmail_client.py`'s
    initial top-level `from ...memory.db import ...` created a genuine
    circular import (`tooling`→`gmail_client`→`memory`→`summarize`→
    `embeddings`→`tgi`→`tooling`) -- fixed by moving it inside
    `get_credentials()`, matching the lazy-import convention
    `tooling/cache.py` already established for exactly this reason.

Not done: `docs/ARCHITECTURE.md`/`COMMANDS.md` haven't been updated with
a hooks/Gmail section yet (no new Telegram command exists -- Gmail is
reached via natural-language tool calls, not a slash command, so
`COMMANDS.md` may not need an entry at all).

## Remaining setup (yours to do -- not something that can be automated)

Gmail code is complete but genuinely untested past this point, since it
needs real Google credentials:

1. In Google Cloud Console: create a project (or use an existing one),
   enable the Gmail API, create an OAuth 2.0 Client ID of type
   **"Desktop app"** (not "Web application").
2. Put that client id/secret in `.env` as `GMAIL_CLIENT_ID`/
   `GMAIL_CLIENT_SECRET`.
3. From your own machine (not the server): `python
   scripts/setup_google_oauth.py <your_alira_user_id>` -- opens a browser
   for consent, writes the resulting token into the same Postgres DB the
   server uses.
4. Try it from Telegram: "check my email" (→ `check_gmail`) or "let me
   know if I get an email from X" (→ `schedule_gmail_check`).
5. Expect to redo step 3 roughly weekly unless you go through Google's
   app verification once -- see "Real risk" above.

### Critical files
- `llm_engine/memory/schema.sql`
- `llm_engine/memory/db.py`
- `llm_engine/pipeline.py`
- `webhook_server.py`
- `llm_engine/tooling/registry.py` (pattern to mirror)
- `llm_engine/tooling/_tooling_deps/arxiv_search.py` (client/wrapper split
  to mirror)

## Amendment: Calendar connector + shared Google auth + calendar-aware reminders

Built as a direct follow-on, once Gmail proved the connector pattern out.
Three decisions made with the user before building:

1. **View + create events**, not edit/delete -- lower-stakes than Gmail's
   draft-only ceiling (creating an event is easy to undo/ignore), but
   editing/deleting an existing event the model might misidentify is a
   real way to lose something, so left out of this pass.
2. **Shared OAuth with Gmail, one consent flow.** Google grants scopes
   requested together in one flow onto one token, so this isn't two
   credentials -- it's one. Credential loading/refresh, previously
   Gmail-specific, moved out into `llm_engine/tooling/_tooling_deps/
   google_auth.py` (`get_credentials`, `GoogleAuthError`), shared by both
   `gmail_client.py` (which re-exports `GmailAuthError = GoogleAuthError`
   so its existing call sites didn't need touching) and the new
   `calendar_client.py`. `oauth_credentials.service` is now `'google'`
   (was `'gmail'`) -- a clean rename, not a migration, since no
   credential had actually been stored yet when this changed.
   `scripts/setup_gmail_oauth.py` was replaced outright (not kept
   alongside) by `scripts/setup_google_oauth.py`, requesting
   `gmail.readonly` + `gmail.compose` + `calendar.events` together.
3. **Calendar-aware reminders, now, not deferred.** `hooks/reminder.py`
   checks `calendar_client.get_busy_until(user_id)` before ever calling
   the model -- if the user is in a meeting right now, the reminder is
   silently deferred to just after it ends, not sent mid-meeting. This
   is the one part of this amendment that touches shared infrastructure
   every hook kind depends on: `HookResult` gained a `reschedule_to:
   Optional[datetime]` field, and `webhook_server._hook_ticker` checks it
   before applying its normal recurrence/deactivation math -- when set,
   it overrides that logic entirely (a deferral is "try again shortly,"
   not "this cycle is done," for both one-shot and recurring hooks).
   Every hook kind that doesn't set it (`gmail_poll`, and `reminder`
   itself whenever the user isn't in a meeting) behaves exactly as
   before this field existed. Calendar-busy-check failures (not
   connected, API error) fail open -- proceed as if not busy -- since a
   best-effort awareness feature must never be able to break the
   reminder it's layered on top of.

New files: `_tooling_deps/google_auth.py`, `_tooling_deps/calendar_client.py`,
`tooling/calendar_tools.py` (`list_calendar_events`, `create_calendar_event`,
scoped to `calendar.events` only -- no calendar-list/settings access).
New discoverable category: `calendar` (`tooling/discovery.py`). No new
hook kind, no schema.sql change beyond what Gmail already added (`service`
was already a free-form column).

**Real risk carried over from Gmail, now covering Calendar too**: the
same 7-day "Testing" refresh-token cap applies to the combined credential
-- Gmail's already-restricted scopes were the binding constraint before
Calendar was added, so this doesn't make the reconnect cadence any worse,
just wasn't going to be better either way.

Setup is now a single combined step -- update `.env`'s
`GMAIL_CLIENT_ID`/`GMAIL_CLIENT_SECRET` (Calendar API also needs enabling
in the same Google Cloud project) and run `scripts/setup_google_oauth.py
<user_id>` once for both connectors.
