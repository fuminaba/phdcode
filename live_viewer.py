"""Simple, read-only live viewer for llm_engine's turn_actions table -- see
what the agent is doing as it happens, in a browser. Polling, not a
websocket/push: the table already gets a new row the instant something
happens (see memory.db.write_turn_action), so a ~1.5s poll interval is
already "near real-time" for this purpose, at a fraction of the
complexity of a push mechanism.

No authentication -- meant to be reached over the same Tailscale network
Ollama already sits on (see DEFAULT_SERVER_IP/OLLAMA_API_URL in .env), not
exposed publicly. Run with:

    uvicorn live_viewer:app --host 0.0.0.0 --port $LIVE_VIEWER_PORT
"""
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from utils import setup_logging
from llm_engine.memory import get_recent_turn_actions
from llm_engine.tooling import TOOLS, TOOL_FUNCTIONS
from llm_engine.tooling.discovery import TOOL_CATEGORIES
from llm_engine.pipeline import CORE_TOOLS

load_dotenv()
logger = setup_logging("live_viewer")

app = FastAPI(title="ALiRA2 Live Activity")


@app.get("/api/actions")
async def api_actions(since_id: int = 0, limit: int = 200) -> list[dict]:
    rows = await get_recent_turn_actions(since_id=since_id, limit=limit)
    return [
        {
            "id": r["id"],
            "turn_id": str(r["turn_id"]),
            "conversation_id": r["conversation_id"],
            "user_id": r["user_id"],
            "action_type": r["action_type"],
            "payload": r["payload"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@app.get("/api/tools")
async def api_tools() -> list[dict]:
    """The live tool registry -- every @tool-decorated function
    (llm_engine.tooling.registry.TOOLS), which of them are always visible
    ("core", see pipeline.CORE_TOOLS) vs. only revealed on demand via
    discover_tools (tooling/discovery.py's TOOL_CATEGORIES), each one's
    full docstring (its actual model-facing use-case description -- the
    same text the model itself sees as the tool's schema `description`,
    not a separately maintained summary that could drift from it), and
    the real underlying Python function it dispatches to (module +
    qualname), for tracing what code actually runs when a tool fires."""
    category_by_tool = {
        name: category
        for category, names in TOOL_CATEGORIES.items()
        for name in names
    }
    core_names = {t["function"]["name"] for t in CORE_TOOLS}

    tools = []
    for t in TOOLS:
        name = t["function"]["name"]
        fn = TOOL_FUNCTIONS.get(name)
        tools.append({
            "name": name,
            "category": "core" if name in core_names else category_by_tool.get(name, "unknown"),
            "description": t["function"]["description"],
            "parameters": t["function"]["parameters"],
            "module": fn.__module__ if fn else None,
            "function": fn.__qualname__ if fn else None,
        })
    return sorted(tools, key=lambda r: (r["category"] != "core", r["category"], r["name"]))


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE


_PAGE = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<title>ALiRA2 Live Activity</title>
<style>
  :root {
    --bg: #0f1115; --surface: #171a21; --surface-alt: #1f232c;
    --line: #2a2f3a; --ink: #e4e7ec; --ink-muted: #8b93a1;
    --accent: #5b9dff;
    --turn: #5b9dff; --llm: #b07ef0; --tool: #4fc08d;
    --pipeline: #e0a156; --success: #4fc08d; --error: #e06c75;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: "SF Mono", "Consolas", "Menlo", monospace;
    font-size: 13px; line-height: 1.5;
  }
  header {
    position: sticky; top: 0; z-index: 10;
    display: flex; align-items: center; gap: 0.75rem;
    padding: 0.85rem 1.25rem; background: var(--surface);
    border-bottom: 1px solid var(--line);
  }
  header h1 { font-size: 14px; font-weight: 600; margin: 0; letter-spacing: 0.02em; }
  #status { font-size: 12px; color: var(--ink-muted); }
  #status.live::before { content: "● "; color: var(--success); }
  #status.stale::before { content: "● "; color: var(--error); }
  #filters {
    position: sticky; top: 49px; z-index: 9;
    display: flex; flex-wrap: wrap; align-items: center; gap: 0.6rem;
    padding: 0.6rem 1.25rem; background: var(--surface-alt);
    border-bottom: 1px solid var(--line); font-size: 12px;
  }
  #filters input[type="text"], #filters select {
    background: var(--bg); color: var(--ink); border: 1px solid var(--line);
    border-radius: 5px; padding: 0.3rem 0.5rem; font: inherit; font-size: 12px;
  }
  #filters input[type="text"]::placeholder { color: var(--ink-muted); }
  #f-search { width: 180px; }
  #f-user, #f-conv { width: 110px; }
  #f-types { display: flex; gap: 0.7rem; }
  #f-types label { display: flex; align-items: center; gap: 0.3rem; color: var(--ink-muted); cursor: pointer; }
  #f-types input { accent-color: var(--accent); }
  #f-clear {
    background: transparent; color: var(--ink-muted); border: 1px solid var(--line);
    border-radius: 5px; padding: 0.3rem 0.6rem; font: inherit; font-size: 12px; cursor: pointer;
  }
  #f-clear:hover { color: var(--ink); border-color: var(--ink-muted); }
  #f-count { color: var(--ink-muted); margin-left: auto; }
  main { max-width: 860px; margin: 0 auto; padding: 1.25rem; display: flex; flex-direction: column; gap: 0.9rem; }
  .turn {
    background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
    overflow: hidden;
  }
  .turn-head {
    display: flex; align-items: baseline; gap: 0.6rem; padding: 0.65rem 0.9rem;
    background: var(--surface-alt); border-bottom: 1px solid var(--line);
  }
  .turn-head .prompt { color: var(--ink); font-weight: 500; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .turn-head .tokens { color: var(--ink-muted); font-size: 11px; white-space: nowrap; cursor: help; }
  .turn-head .time { color: var(--ink-muted); font-size: 11px; }
  .badge {
    font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 0.1rem 0.45rem; border-radius: 999px; white-space: nowrap;
  }
  .badge.pending { background: #2a2f3a; color: var(--ink-muted); }
  .badge.success { background: rgba(79,192,141,0.15); color: var(--success); }
  .badge.error { background: rgba(224,108,117,0.15); color: var(--error); }
  .actions { padding: 0.3rem 0.9rem 0.6rem; display: flex; flex-direction: column; gap: 0.3rem; }
  .action { display: flex; gap: 0.5rem; align-items: flex-start; }
  .action .tag {
    font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em;
    width: 72px; flex-shrink: 0; padding-top: 0.1rem;
  }
  .action.llm_call .tag { color: var(--llm); }
  .action.tool_call .tag { color: var(--tool); }
  .action.pipeline_start .tag, .action.pipeline_step .tag, .action.pipeline_end .tag { color: var(--pipeline); }
  .action .detail { color: var(--ink); overflow-wrap: anywhere; flex: 1; min-width: 0; }
  .action .detail .muted { color: var(--ink-muted); }
  .llm-output summary {
    cursor: pointer; list-style: none; color: var(--ink);
  }
  .llm-output summary::-webkit-details-marker { display: none; }
  .llm-output summary::before { content: "▸ "; color: var(--ink-muted); }
  .llm-output[open] summary::before { content: "▾ "; }
  .llm-output .llm-content {
    margin: 0.5rem 0 0.2rem; padding: 0.65rem 0.8rem; background: var(--bg);
    border: 1px solid var(--line); border-radius: 6px; white-space: pre-wrap;
    word-break: break-word; color: var(--ink); font-size: 12px; line-height: 1.5;
    max-height: 320px; overflow-y: auto;
  }
  .llm-reasoning { display: block; margin-top: 0.4rem; }
  .llm-reasoning summary { color: var(--ink-muted); font-style: italic; }
  .llm-reasoning .llm-content { color: var(--ink-muted); font-style: italic; }
  #empty { color: var(--ink-muted); text-align: center; padding: 3rem 0; }

  #tabs { display: flex; gap: 0.35rem; margin-left: 0.5rem; }
  .tab {
    background: transparent; color: var(--ink-muted); border: 1px solid var(--line);
    border-radius: 5px; padding: 0.3rem 0.7rem; font: inherit; font-size: 12px;
    cursor: pointer;
  }
  .tab:hover { color: var(--ink); }
  .tab.active { color: var(--ink); background: var(--surface-alt); border-color: var(--accent); }

  #tool-filters {
    position: sticky; top: 49px; z-index: 9;
    display: flex; flex-wrap: wrap; align-items: center; gap: 0.6rem;
    padding: 0.6rem 1.25rem; background: var(--surface-alt);
    border-bottom: 1px solid var(--line); font-size: 12px;
  }
  #tool-filters input[type="text"] {
    background: var(--bg); color: var(--ink); border: 1px solid var(--line);
    border-radius: 5px; padding: 0.3rem 0.5rem; font: inherit; font-size: 12px; width: 200px;
  }
  #tool-filters input::placeholder { color: var(--ink-muted); }
  #t-categories { display: flex; flex-wrap: wrap; gap: 0.4rem; }
  #t-categories button {
    background: transparent; color: var(--ink-muted); border: 1px solid var(--line);
    border-radius: 999px; padding: 0.15rem 0.6rem; font: inherit; font-size: 11px; cursor: pointer;
  }
  #t-categories button.active { color: var(--bg); background: var(--accent); border-color: var(--accent); }
  #t-count { color: var(--ink-muted); margin-left: auto; }
  #tool-list { max-width: 860px; margin: 0 auto; padding: 1.25rem; display: flex; flex-direction: column; gap: 0.7rem; }
  .tool-card {
    background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 0.75rem 0.9rem;
  }
  .tool-card-head { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }
  .tool-card .name { font-weight: 600; color: var(--ink); font-size: 13px; }
  .tool-card .impl {
    color: var(--ink-muted); font-size: 11px; margin-left: auto; white-space: nowrap; cursor: help;
  }
  .cat-badge {
    font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 0.1rem 0.5rem; border-radius: 999px; white-space: nowrap;
  }
  .cat-badge.core { background: rgba(79,192,141,0.15); color: var(--success); }
  .cat-badge.gmail { background: rgba(91,157,255,0.15); color: var(--accent); }
  .cat-badge.literature { background: rgba(176,126,240,0.15); color: var(--llm); }
  .cat-badge.documents { background: rgba(224,161,86,0.15); color: var(--pipeline); }
  .cat-badge.reminders { background: rgba(79,192,141,0.15); color: var(--tool); }
  .cat-badge.database { background: rgba(224,108,117,0.15); color: var(--error); }
  .tool-card .desc {
    margin: 0.55rem 0 0; color: var(--ink); white-space: pre-wrap; font-size: 12px; line-height: 1.55;
  }
  .tool-card .params {
    margin-top: 0.5rem; padding: 0.5rem 0.7rem; background: var(--bg); border: 1px solid var(--line);
    border-radius: 6px; color: var(--ink-muted); font-size: 11px; white-space: pre-wrap; word-break: break-word;
  }
</style>
</head>
<body>
<header>
  <h1>ALiRA2 &middot; Live Activity</h1>
  <span id="status" class="live">connecting&hellip;</span>
  <nav id="tabs">
    <button class="tab active" data-view="activity" type="button">Activity</button>
    <button class="tab" data-view="tools" type="button">Tools</button>
  </nav>
</header>
<div id="view-activity">
<div id="filters">
  <input id="f-search" type="text" placeholder="Search prompt/detail text&hellip;">
  <select id="f-status">
    <option value="all">All statuses</option>
    <option value="pending">Running</option>
    <option value="success">Success</option>
    <option value="error">Error</option>
  </select>
  <input id="f-user" type="text" placeholder="User ID">
  <input id="f-conv" type="text" placeholder="Conversation ID">
  <div id="f-types">
    <label><input type="checkbox" value="llm_call" checked> LLM</label>
    <label><input type="checkbox" value="tool_call" checked> Tool</label>
    <label><input type="checkbox" value="pipeline" checked> Pipeline</label>
  </div>
  <button id="f-clear" type="button">Clear filters</button>
  <span id="f-count"></span>
</div>
<main id="feed"><div id="empty">Waiting for activity&hellip;</div></main>
</div>
<div id="view-tools" hidden>
  <div id="tool-filters">
    <input id="t-search" type="text" placeholder="Search tools&hellip;">
    <div id="t-categories"></div>
    <span id="t-count"></span>
  </div>
  <main id="tool-list"></main>
</div>
<script>
const feed = document.getElementById("feed");
const statusEl = document.getElementById("status");
const turnCards = new Map();  // turn_id -> {el, actionsEl, promptEl, badgeEl}
let sinceId = 0;
let misses = 0;

const filters = { q: "", status: "all", user: "", conv: "", types: new Set(["llm_call", "tool_call", "pipeline"]) };
const countEl = document.getElementById("f-count");
const CHARS_PER_TOKEN = 4;  // matches .env's MAX_HISTORY_CHARS comment -- same conservative heuristic

function typeGroup(actionType) {
  return actionType.startsWith("pipeline") ? "pipeline" : actionType;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function truncate(s, n) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n) + "…" : s;
}
function fmtTime(iso) {
  try { return new Date(iso).toLocaleTimeString(); } catch { return iso; }
}

function ensureTurnCard(row) {
  let card = turnCards.get(row.turn_id);
  if (card) return card;

  document.getElementById("empty")?.remove();

  const el = document.createElement("div");
  el.className = "turn";
  el.dataset.status = "pending";
  el.dataset.userId = row.user_id != null ? String(row.user_id) : "";
  el.dataset.conversationId = row.conversation_id != null ? String(row.conversation_id) : "";
  el.innerHTML = `
    <div class="turn-head">
      <span class="prompt">(turn started)</span>
      <span class="tokens" hidden></span>
      <span class="badge pending">running</span>
      <span class="time">${esc(fmtTime(row.created_at))}</span>
    </div>
    <div class="actions"></div>
  `;
  feed.insertBefore(el, feed.firstChild);

  card = {
    el,
    actionsEl: el.querySelector(".actions"),
    promptEl: el.querySelector(".prompt"),
    tokensEl: el.querySelector(".tokens"),
    badgeEl: el.querySelector(".badge"),
    promptLen: 0,  // set at turn_start -- the UNTRUNCATED user_prompt length, for the history-cost estimate
  };
  turnCards.set(row.turn_id, card);
  return card;
}

function renderAction(row) {
  const p = row.payload || {};
  let detail = "";
  let searchText = "";
  switch (row.action_type) {
    case "turn_start":
      return null;  // handled via the card header instead
    case "llm_call": {
      const requested = p.requested_tools || [];
      const toolNames = requested.map(t => t.name).join(", ");
      const kind = p.digest ? "digest"
        : (requested.length ? `requested: ${toolNames}` : (p.had_tool_calls ? "requested tool(s)" : "final answer"));
      const summary = `${esc(kind)} <span class="muted">(${p.tokens_used ?? "?"} tok, ${esc(p.finish_reason ?? "")})</span>`;
      let body = p.content ? esc(p.content) : `<span class="muted">(no text output)</span>`;
      if (requested.length) {
        const callDetail = requested.map(t => `${t.name}(${t.arguments})`).join("\\n");
        body += `\\n\\n<span class="muted">requested tool call(s):</span>\\n${esc(callDetail)}`;
      }
      detail = `<details class="llm-output"><summary>${summary}</summary><pre class="llm-content">${body}</pre></details>`;
      if (p.reasoning) {
        const reasonSummary = `&#129504; reasoning <span class="muted">(${p.reasoning.length.toLocaleString()} chars)</span>`;
        detail += `<details class="llm-output llm-reasoning"><summary>${reasonSummary}</summary><pre class="llm-content">${esc(p.reasoning)}</pre></details>`;
      }
      searchText = `${kind} ${p.finish_reason ?? ""} ${p.content ?? ""} ${toolNames} ${p.reasoning ?? ""}`;
      break;
    }
    case "tool_call": {
      const argStr = JSON.stringify(p.arguments ?? {});
      const resStr = typeof p.result === "string" ? p.result : JSON.stringify(p.result, null, 2);
      const summary = `<strong>${esc(p.tool ?? "?")}</strong>(${esc(truncate(argStr, 80))})`;
      const body = `arguments:\\n${argStr}\\n\\nresult:\\n${resStr}`;
      detail = `<details class="llm-output"><summary>${summary}</summary><pre class="llm-content">${esc(body)}</pre></details>`;
      searchText = `${p.tool ?? ""} ${argStr} ${resStr}`;
      break;
    }
    case "pipeline_start":
      detail = `started: <strong>${esc(p.label ?? "")}</strong>`;
      searchText = `started ${p.label ?? ""}`;
      break;
    case "pipeline_step":
      detail = `${esc(p.label ?? "")} <span class="muted">(${Math.round((p.fraction ?? 0) * 100)}%)</span>`;
      searchText = p.label ?? "";
      break;
    case "pipeline_end":
      detail = `finished: <strong>${esc(p.label ?? "")}</strong> <span class="muted">(${esc(p.status ?? "")})</span>`;
      searchText = `finished ${p.label ?? ""} ${p.status ?? ""}`;
      break;
    case "turn_end":
      return null;  // handled via the card header instead
    default:
      detail = esc(JSON.stringify(p));
      searchText = JSON.stringify(p);
  }
  const div = document.createElement("div");
  div.className = `action ${row.action_type}`;
  div.dataset.type = row.action_type;
  div.dataset.search = searchText.toLowerCase();
  div.innerHTML = `<span class="tag">${esc(row.action_type.replace("_", " "))}</span><span class="detail">${detail}</span>`;
  return div;
}

function cardMatchesBaseFilters(card) {
  if (filters.status !== "all" && card.el.dataset.status !== filters.status) return false;
  if (filters.user && card.el.dataset.userId !== filters.user) return false;
  if (filters.conv && card.el.dataset.conversationId !== filters.conv) return false;
  return true;
}

function cardMatchesSearch(card) {
  if (!filters.q) return true;
  if (card.promptEl.textContent.toLowerCase().includes(filters.q)) return true;
  for (const actionEl of card.actionsEl.children) {
    if (actionEl.dataset.search.includes(filters.q)) return true;
  }
  return false;
}

function cardMatchesTypes(card) {
  const total = card.actionsEl.children.length;
  if (total === 0) return true;  // nothing logged under this turn yet -- don't hide it
  for (const actionEl of card.actionsEl.children) {
    if (filters.types.has(typeGroup(actionEl.dataset.type))) return true;
  }
  return false;
}

function applyFiltersToCard(card) {
  for (const actionEl of card.actionsEl.children) {
    actionEl.hidden = !filters.types.has(typeGroup(actionEl.dataset.type));
  }
  card.el.hidden = !(cardMatchesBaseFilters(card) && cardMatchesSearch(card) && cardMatchesTypes(card));
}

function updateCount() {
  let shown = 0;
  for (const card of turnCards.values()) if (!card.el.hidden) shown++;
  countEl.textContent = `${shown} / ${turnCards.size} turns`;
}

function reapplyAllFilters() {
  for (const card of turnCards.values()) applyFiltersToCard(card);
  updateCount();
}

function applyRow(row) {
  const card = ensureTurnCard(row);
  const p = row.payload || {};

  if (row.action_type === "turn_start") {
    card.promptEl.textContent = truncate(p.user_prompt, 90);
    card.promptLen = (p.user_prompt || "").length;
  } else if (row.action_type === "turn_end") {
    card.badgeEl.textContent = p.status || "done";
    card.badgeEl.className = `badge ${p.status === "error" ? "error" : "success"}`;
    card.el.dataset.status = p.status === "error" ? "error" : "success";

    // Two different numbers, often very different: tokens_used is the
    // TRUE total across every LLM call this turn made (decide + tool +
    // digest rounds) -- what the turn actually cost. historyEst is an
    // ESTIMATE of what gets carried forward: only the final user_prompt
    // + output ever get persisted/replayed (see pipeline._turns_to_history
    // -- raw tool exchanges never survive past the turn they happened in),
    // converted at the same ~4 chars/token heuristic MAX_HISTORY_CHARS
    // itself is sized with (see .env's own comment on that constant).
    const total = p.tokens_used;
    const historyEst = Math.round((card.promptLen + (p.output || "").length) / CHARS_PER_TOKEN);
    if (total != null) {
      card.tokensEl.textContent = `Σ${total.toLocaleString()} · ~${historyEst.toLocaleString()}→hist`;
      card.tokensEl.title = `${total.toLocaleString()} tokens total across every LLM call this turn made ` +
        `(decide/tool/digest rounds). ~${historyEst.toLocaleString()} is an estimate of what this turn will ` +
        `cost as replayed history in FUTURE turns -- only the final prompt+output ever get carried forward, ` +
        `not the full total (~${CHARS_PER_TOKEN} chars/token, same heuristic MAX_HISTORY_CHARS uses).`;
      card.tokensEl.hidden = false;
    }
  } else {
    const node = renderAction(row);
    if (node) card.actionsEl.appendChild(node);
  }
  applyFiltersToCard(card);
  updateCount();
}

async function poll() {
  try {
    const res = await fetch(`/api/actions?since_id=${sinceId}&limit=500`);
    if (!res.ok) throw new Error(res.status);
    const rows = await res.json();
    for (const row of rows) {
      applyRow(row);
      sinceId = Math.max(sinceId, row.id);
    }
    statusEl.textContent = "live";
    statusEl.className = "live";
    misses = 0;
  } catch (e) {
    misses += 1;
    if (misses > 2) { statusEl.textContent = "reconnecting…"; statusEl.className = "stale"; }
  } finally {
    setTimeout(poll, 1500);
  }
}
document.getElementById("f-search").addEventListener("input", (e) => {
  filters.q = e.target.value.trim().toLowerCase();
  reapplyAllFilters();
});
document.getElementById("f-status").addEventListener("change", (e) => {
  filters.status = e.target.value;
  reapplyAllFilters();
});
document.getElementById("f-user").addEventListener("input", (e) => {
  filters.user = e.target.value.trim();
  reapplyAllFilters();
});
document.getElementById("f-conv").addEventListener("input", (e) => {
  filters.conv = e.target.value.trim();
  reapplyAllFilters();
});
document.querySelectorAll("#f-types input").forEach((cb) => {
  cb.addEventListener("change", () => {
    filters.types = new Set(
      [...document.querySelectorAll("#f-types input:checked")].map((el) => el.value)
    );
    reapplyAllFilters();
  });
});
document.getElementById("f-clear").addEventListener("click", () => {
  filters.q = ""; filters.status = "all"; filters.user = ""; filters.conv = "";
  filters.types = new Set(["llm_call", "tool_call", "pipeline"]);
  document.getElementById("f-search").value = "";
  document.getElementById("f-status").value = "all";
  document.getElementById("f-user").value = "";
  document.getElementById("f-conv").value = "";
  document.querySelectorAll("#f-types input").forEach((cb) => { cb.checked = true; });
  reapplyAllFilters();
});

poll();

// -- Tools tab --------------------------------------------------------
let toolsData = null;  // fetched lazily, once, on first visit to the tab
const toolFilters = { q: "", category: "all" };
const toolListEl = document.getElementById("tool-list");
const toolCountEl = document.getElementById("t-count");
const toolCategoriesEl = document.getElementById("t-categories");

function toolMatchesFilters(t) {
  if (toolFilters.category !== "all" && t.category !== toolFilters.category) return false;
  if (!toolFilters.q) return true;
  const haystack = `${t.name} ${t.description} ${t.category}`.toLowerCase();
  return haystack.includes(toolFilters.q);
}

function renderToolCategoryButtons() {
  const categories = ["all", ...new Set(toolsData.map(t => t.category))];
  toolCategoriesEl.innerHTML = categories.map(c =>
    `<button data-cat="${esc(c)}" class="${c === toolFilters.category ? "active" : ""}">${esc(c)}</button>`
  ).join("");
  toolCategoriesEl.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      toolFilters.category = btn.dataset.cat;
      renderToolCategoryButtons();
      renderTools();
    });
  });
}

function renderTools() {
  const shown = toolsData.filter(toolMatchesFilters);
  toolCountEl.textContent = `${shown.length} / ${toolsData.length} tools`;
  toolListEl.innerHTML = shown.map(t => `
    <div class="tool-card">
      <div class="tool-card-head">
        <span class="name">${esc(t.name)}</span>
        <span class="cat-badge ${esc(t.category)}">${esc(t.category)}</span>
        <span class="impl" title="The real Python function this tool dispatches to">${esc(t.module)}.${esc(t.function)}</span>
      </div>
      <pre class="desc">${esc(t.description)}</pre>
      <details><summary style="cursor:pointer;color:var(--ink-muted);font-size:11px;">parameters schema</summary>
        <pre class="params">${esc(JSON.stringify(t.parameters, null, 2))}</pre>
      </details>
    </div>
  `).join("") || `<div id="empty">No tools match.</div>`;
}

async function loadTools() {
  if (toolsData) { renderTools(); return; }
  toolListEl.innerHTML = `<div id="empty">Loading&hellip;</div>`;
  try {
    const res = await fetch("/api/tools");
    toolsData = await res.json();
    renderToolCategoryButtons();
    renderTools();
  } catch (e) {
    toolListEl.innerHTML = `<div id="empty">Couldn't load tool registry.</div>`;
  }
}

document.getElementById("t-search").addEventListener("input", (e) => {
  toolFilters.q = e.target.value.trim().toLowerCase();
  renderTools();
});

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    const view = btn.dataset.view;
    document.getElementById("view-activity").hidden = view !== "activity";
    document.getElementById("view-tools").hidden = view !== "tools";
    if (view === "tools") loadTools();
  });
});
</script>
</body>
</html>
"""
