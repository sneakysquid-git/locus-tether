"""
On-demand web dashboard with four tabs:
  - Today: brief summary + actionable to-do checklist for today specifically
  - Conversations: condensed list across ALL history, drill into full detail
  - To-Dos: flat list of every open action item across all history
  - Feedback: condensed list of past speech-coaching sessions, drill into detail

Deliberately lightweight: reads existing JSON files off disk (via
data_store.py) and serves them — no GPU, no LLM calls, negligible resource
footprint at rest, safe to run continuously alongside robotics work.

Meant to run on 127.0.0.1 ONLY (see systemd/webapp.service) and be exposed to
your Tailscale network via `tailscale serve` — never bind this to 0.0.0.0 or
a LAN-reachable interface directly.

Run with:
    python3 webapp.py
or as a persistent service — see systemd/webapp.service.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, request

# pipeline/ holds config.py, data_store.py, integrations.py — shared core
# modules used by webapp.py, digest.py, and speech_coach.py alike. Added to
# sys.path explicitly (rather than converting everything to package-relative
# imports) so this script keeps working the same simple way regardless of
# working directory — matches how it's invoked both directly and via systemd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import config
import data_store
import integrations
import todo_state

app = Flask(__name__)


# --- Serialization helpers -------------------------------------------------

def _serialize_action_items(analysis: dict) -> list[dict]:
    stem = analysis.get("_stem", "")
    items = []
    for i, item in enumerate(analysis.get("action_items", [])):
        item_id = f"{stem}:{i}"
        items.append(
            {
                "id": item_id,
                "description": item["description"],
                "due_date": item.get("due_date"),
                "completed": todo_state.is_completed(item_id, item.get("completed", False)),
            }
        )
    return items


def _condensed_conversation(a: dict) -> dict:
    overview = a.get("overview", "")
    preview = overview if len(overview) <= 120 else overview[:117] + "..."
    return {
        "stem": a.get("_stem", ""),
        "date": a.get("_date", ""),
        "title": a.get("title", a.get("_stem", "")),
        "emoji": a.get("emoji", ""),
        "category": a.get("category", "uncategorized"),
        "preview": preview,
        "action_item_count": len(a.get("action_items", [])),
    }


def _full_conversation(a: dict) -> dict:
    stem = a.get("_stem", "")
    speech = data_store.load_speech_coaching_by_stem(stem)
    return {
        "stem": stem,
        "date": a.get("_date", ""),
        "title": a.get("title", stem),
        "emoji": a.get("emoji", ""),
        "category": a.get("category", "uncategorized"),
        "overview": a.get("overview", ""),
        "key_facts": a.get("key_facts", []),
        "action_items": _serialize_action_items(a),
        "speech_coaching": speech,
    }


def _condensed_feedback(sc: dict) -> dict:
    analysis = data_store.load_analysis_by_stem(sc.get("_stem", ""))
    title = analysis.get("title", sc["_stem"]) if analysis else sc.get("_stem", "")
    pace = sc["metrics"]["pace"]
    return {
        "stem": sc.get("_stem", ""),
        "date": sc.get("_date", ""),
        "title": title,
        "words_per_minute": pace["words_per_minute"],
        "overall_take_preview": sc["feedback"].get("overall_take", "")[:100],
    }


def _all_open_action_items() -> list[dict]:
    """
    Every incomplete action item across ALL history, not just today (#10:
    items should stay visible day after day until checked off, not silently
    disappear once the day changes). Completed items are excluded here
    entirely — they still show, crossed out, within that specific
    conversation's own detail view (see _full_conversation), since that's
    a historical record, not an active work list, and doesn't clutter
    anything by keeping them visible there.
    """
    items = []
    for a in data_store.load_all_analyses():
        source_stem = a.get("_stem", "")
        source_title = a.get("title", source_stem)
        source_date = a.get("_date", "")
        for item in _serialize_action_items(a):
            if item["completed"]:
                continue
            item["source_stem"] = source_stem
            item["source_title"] = source_title
            item["date"] = source_date
            items.append(item)
    return items


def _condensed_list(list_group: dict) -> dict:
    open_items = [i for i in list_group["items"] if not todo_state.is_completed(i["id"], False)]
    return {
        "list_name": list_group["list_name"],
        "item_count": len(open_items),
        "most_recent_date": max((i["date"] for i in open_items), default=""),
    }


# --- API: Today (brief, but with real value beyond just a to-do list) -----

@app.route("/api/today")
def api_today():
    today = date.today()
    tomorrow = today + timedelta(days=1)
    analyses = data_store.load_day_analyses(today)
    speech_coaching = data_store.load_day_speech_coaching(today)

    conversations = [
        {
            "stem": a.get("_stem", ""),
            "title": a.get("title", a.get("_stem", "")),
            "emoji": a.get("emoji", ""),
            "category": a.get("category", "uncategorized"),
        }
        for a in analyses
    ]

    # Due-soon/action items are scoped to TODAY's conversations specifically
    # (fixed in #14 — this used to pull from ALL open history via
    # _all_open_action_items(), duplicating the dedicated To-Dos tab and
    # making Today feel cluttered with stale items from other days). The
    # persistence model from #10 (checked-off items disappear from active
    # views, but stay visible crossed-out in their own conversation's detail
    # view) is unaffected — it now lives only in the To-Dos tab, which still
    # uses _all_open_action_items() below, unchanged.
    due_soon = []
    action_items = []
    for a in analyses:
        source_stem = a.get("_stem", "")
        source_title = a.get("title", source_stem)
        source_date = a.get("_date", "")
        for item in _serialize_action_items(a):
            if item["completed"]:
                continue
            item["source_stem"] = source_stem
            item["source_title"] = source_title
            item["date"] = source_date
            parsed = integrations.parse_relative_date(item.get("due_date"), today)
            if parsed in (today, tomorrow):
                due_soon.append(item)
            else:
                action_items.append(item)

    # Key facts stay scoped to TODAY specifically — these are tied to a
    # given conversation's context, not an open/closed work item.
    key_facts = []
    for a in analyses:
        source_title = a.get("title", a.get("_stem", ""))
        for fact in a.get("key_facts", []):
            key_facts.append({"fact": fact, "source_title": source_title})

    # Lists teaser: which named lists got new items added today specifically.
    lists_today_map = {}
    for a in analyses:
        for mlist in a.get("mentioned_lists", []):
            name = mlist.get("list_name", "Misc").strip()
            if mlist.get("items"):
                key = name.lower()
                lists_today_map.setdefault(key, {"list_name": name, "new_item_count": 0})
                lists_today_map[key]["new_item_count"] += len(mlist["items"])
    lists_today = list(lists_today_map.values())

    speech_teasers = []
    for sc in speech_coaching:
        stem = sc.get("_stem", "")
        matching_analysis = data_store.load_analysis_by_stem(stem)
        title = matching_analysis.get("title", stem) if matching_analysis else stem
        speech_teasers.append(
            {
                "stem": stem,
                "title": title,
                "overall_take_preview": sc["feedback"].get("overall_take", "")[:140],
            }
        )

    return jsonify(
        {
            "date": today.isoformat(),
            "conversation_count": len(analyses),
            "conversations": conversations,
            "due_soon": due_soon,
            "action_items": action_items,
            "key_facts": key_facts,
            "lists_today": lists_today,
            "speech_coaching": speech_teasers,
        }
    )


# --- API: Conversations -----------------------------------------------------

@app.route("/api/conversations")
def api_conversations():
    analyses = data_store.load_all_analyses()
    return jsonify([_condensed_conversation(a) for a in analyses])


@app.route("/api/conversations/<path:stem>")
def api_conversation_detail(stem: str):
    a = data_store.load_analysis_by_stem(stem)
    if a is None:
        abort(404)
    return jsonify(_full_conversation(a))


# --- API: To-Dos -------------------------------------------------------------

@app.route("/api/todos")
def api_todos():
    return jsonify(_all_open_action_items())


@app.route("/api/todos/completed")
def api_todos_completed():
    """
    Action items completed specifically TODAY — for the To-Dos tab's
    "Completed" view. Not a deletion of anything: the underlying
    todo_state.json record is untouched, so a checked-off item still shows
    crossed-out in its own conversation's detail view regardless of when it
    was completed. This is purely a today-scoped display filter, which is
    why it naturally empties out tomorrow without any cleanup job needed —
    tomorrow, get_completed_date() for these same items just won't equal
    tomorrow's date anymore.
    """
    today_str = date.today().isoformat()
    items = []
    for a in data_store.load_all_analyses():
        source_stem = a.get("_stem", "")
        source_title = a.get("title", source_stem)
        source_date = a.get("_date", "")
        for i, item in enumerate(a.get("action_items", [])):
            item_id = f"{source_stem}:{i}"
            if todo_state.get_completed_date(item_id) == today_str:
                items.append(
                    {
                        "id": item_id,
                        "description": item["description"],
                        "due_date": item.get("due_date"),
                        "source_stem": source_stem,
                        "source_title": source_title,
                        "date": source_date,
                    }
                )
    return jsonify(items)


@app.route("/api/todo/<path:item_id>/toggle", methods=["POST"])
def api_toggle_todo(item_id: str):
    default = request.json.get("current_default", False) if request.is_json else False
    new_state = todo_state.toggle(item_id, default)
    return jsonify({"id": item_id, "completed": new_state})


# --- API: Lists ----------------------------------------------------------

@app.route("/api/lists")
def api_lists():
    condensed = [_condensed_list(g) for g in data_store.aggregate_lists()]
    condensed = [c for c in condensed if c["item_count"] > 0]  # fully-checked-off lists just disappear
    condensed.sort(key=lambda c: c["most_recent_date"], reverse=True)
    return jsonify(condensed)


@app.route("/api/lists/<path:list_name>")
def api_list_detail(list_name: str):
    matching = next(
        (g for g in data_store.aggregate_lists() if g["list_name"].lower() == list_name.lower()),
        None,
    )
    if matching is None:
        abort(404)
    open_items = [i for i in matching["items"] if not todo_state.is_completed(i["id"], False)]
    return jsonify({"list_name": matching["list_name"], "items": open_items})


# --- API: Feedback -----------------------------------------------------------

@app.route("/api/feedback")
def api_feedback():
    coaching = data_store.load_all_speech_coaching()
    return jsonify([_condensed_feedback(sc) for sc in coaching])


@app.route("/api/feedback/<path:stem>")
def api_feedback_detail(stem: str):
    sc = data_store.load_speech_coaching_by_stem(stem)
    if sc is None:
        abort(404)
    analysis = data_store.load_analysis_by_stem(stem)
    sc["title"] = analysis.get("title", stem) if analysis else stem
    return jsonify(sc)


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#0d1117">
<title>LocusTether</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 600px;
         margin: 0 auto; padding: 16px 16px 76px; color: #e6edf3; background: #0d1117; }
  h1, h2 { color: #e6edf3; }
  h1 { font-size: 20px; display: flex; justify-content: space-between; align-items: center; }
  #refresh-btn { background: #1f6feb; color: #ffffff; border: none; border-radius: 6px;
                 padding: 8px 16px; font-size: 14px; }
  #refresh-btn:active { opacity: 0.75; }
  #back-btn { background: none; border: none; color: #58a6ff; font-size: 15px; padding: 6px 0;
              display: flex; align-items: center; gap: 4px; }
  #last-updated { color: #8b949e; font-size: 12px; margin-top: -8px; margin-bottom: 16px; }
  .card { border: 1px solid #30363d; border-radius: 8px; padding: 14px 16px;
          margin-bottom: 12px; background: #161b22; }
  .list-row { border: 1px solid #30363d; border-radius: 8px; padding: 12px 14px;
              margin-bottom: 8px; background: #161b22; }
  .list-row:active { background: #1c2230; }
  .badge { display: inline-block; color: #ffffff; font-size: 11px; padding: 2px 8px;
           border-radius: 10px; margin: 6px 0; }
  .todo-row { display: flex; align-items: baseline; padding: 8px 0; border-bottom: 1px solid #21262d; }
  .todo-row input { margin-right: 10px; width: 18px; height: 18px; flex-shrink: 0; }
  .todo-row .desc { color: #e6edf3; }
  .todo-done { text-decoration: line-through; color: #6e7681; }
  .due { color: #ff9662; font-size: 12px; }
  .empty { color: #8b949e; }
  .source-label { color: #6e7681; font-size: 12px; }
  .date-label { color: #6e7681; font-size: 11px; }

  #tabbar { position: fixed; bottom: 0; left: 0; right: 0; background: #161b22;
            border-top: 1px solid #30363d; display: flex; max-width: 600px; margin: 0 auto; }
  #tabbar button { flex: 1; background: none; border: none; color: #6e7681; padding: 12px 4px;
                   font-size: 12px; display: flex; flex-direction: column; align-items: center; gap: 2px; }
  #tabbar button.active { color: #58a6ff; }
  #tabbar .icon { display: flex; }
  #tabbar svg { width: 20px; height: 20px; }
</style>
</head>
<body>
  <div id="header"></div>
  <div id="last-updated"></div>
  <div id="content">Loading...</div>

  <div id="tabbar">
    <button data-tab="today" onclick="goTab('today')"><span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg></span>Today</button>
    <button data-tab="conversations" onclick="goTab('conversations')"><span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg></span>Conversations</button>
    <button data-tab="todos" onclick="goTab('todos')"><span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><polyline points="8 12 11 15 16 9"/></svg></span>To-Dos</button>
    <button data-tab="feedback" onclick="goTab('feedback')"><span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg></span>Feedback</button>
    <button data-tab="lists" onclick="goTab('lists')"><span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg></span>Lists</button>
  </div>

<script>
const CATEGORY_COLORS = {
  personal: "#725ff4", work: "#0073e3", education: "#1b8569",
  health: "#e02f00", finance: "#158482", social: "#e7006f", other: "#6b7580"
};

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : s;
  return d.innerHTML;
}

// --- Simple client-side router using the History API, so both the
// on-page back button AND the phone's actual back gesture behave correctly. ---
function currentState() {
  return history.state || { tab: 'today', detail: null };
}

function navigate(state, push) {
  const hashDetail = state.detail ? '/' + encodeURIComponent(state.detail) : '';
  if (push) history.pushState(state, '', '#' + state.tab + hashDetail);
  render(state);
}

function goTab(tab) {
  navigate({ tab, detail: null }, true);
}

function goDetail(tab, stem) {
  navigate({ tab, detail: stem }, true);
}

window.addEventListener('popstate', () => render(currentState()));

// --- Rendering ---

async function render(state) {
  document.querySelectorAll('#tabbar button').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === state.tab));

  if (state.detail) {
    if (state.tab === 'conversations') return renderConversationDetail(state.detail);
    if (state.tab === 'feedback') return renderFeedbackDetail(state.detail);
    if (state.tab === 'lists') return renderListDetail(state.detail);
    if (state.tab === 'todos') return renderCompletedTodos();
  }
  if (state.tab === 'today') return renderToday();
  if (state.tab === 'conversations') return renderConversationsList();
  if (state.tab === 'todos') return renderTodos();
  if (state.tab === 'feedback') return renderFeedbackList();
  if (state.tab === 'lists') return renderListsList();
}

function setHeader(title, showRefresh, showBack) {
  const backHtml = showBack
    ? `<button id="back-btn" onclick="history.back()">&#8592; Back</button>` : '';
  const refreshHtml = showRefresh
    ? `<button id="refresh-btn" onclick="render(currentState())">&#8635; Refresh</button>` : '';
  document.getElementById('header').innerHTML =
    showBack ? backHtml : `<h1>${esc(title)} ${refreshHtml}</h1>`;
  document.getElementById('last-updated').textContent = showRefresh
    ? 'Last refreshed: ' + new Date().toLocaleTimeString() : '';
}

async function renderToday() {
  setHeader('LocusTether', true, false);
  document.getElementById('content').innerHTML = 'Loading...';
  const data = await (await fetch('/api/today')).json();

  let html = `<p style="color:#8b949e;font-size:14px;">
    ${data.conversation_count} conversation(s) today</p>`;

  if (!data.conversation_count && !data.action_items.length && !data.due_soon.length) {
    html += '<p class="empty">Nothing recorded yet today. Pull up the Conversations or To-Dos tabs for older history.</p>';
    document.getElementById('content').innerHTML = html;
    return;
  }

  // --- Due soon: pulled out from the general list since "due tomorrow"
  // deserves more attention than "no deadline at all". ---
  if (data.due_soon.length) {
    html += '<h2 style="font-size:16px;">Due soon</h2>';
    data.due_soon.forEach(item => {
      html += `<div class="todo-row" style="border-left:3px solid #ff9662;padding-left:8px;"
        data-todo-id="${esc(item.id)}">
        <input type="checkbox" ${item.completed ? 'checked' : ''}
          onclick="toggleTodo('${item.id}', ${item.completed})">
        <span class="desc ${item.completed ? 'todo-done' : ''}">${esc(item.description)}
        <span class="due">(due: ${esc(item.due_date)})</span>
        <span class="source-label"> — ${esc(item.source_title)}</span></span>
      </div>`;
    });
  }

  // --- Today's conversations: compact, tappable rows into full detail ---
  if (data.conversations.length) {
    html += '<h2 style="font-size:16px;margin-top:20px;">Conversations today</h2>';
    data.conversations.forEach(c => {
      const color = CATEGORY_COLORS[c.category] || CATEGORY_COLORS.other;
      html += `<div class="list-row" style="padding:10px 14px;" onclick="goDetail('conversations', '${esc(c.stem)}')">
        <span style="font-weight:600;">${c.emoji || ''} ${esc(c.title)}</span>
        <span class="badge" style="background:${color};margin-left:8px;">${esc(c.category)}</span>
      </div>`;
    });
  }

  // --- Remaining action items (due-soon ones already shown above) ---
  if (data.action_items.length) {
    html += '<h2 style="font-size:16px;margin-top:20px;">Action items</h2>';
    data.action_items.forEach(item => {
      const dueHtml = item.due_date ? ` <span class="due">(due: ${esc(item.due_date)})</span>` : '';
      html += `<div class="todo-row" data-todo-id="${esc(item.id)}">
        <input type="checkbox" ${item.completed ? 'checked' : ''}
          onclick="toggleTodo('${item.id}', ${item.completed})">
        <span class="desc ${item.completed ? 'todo-done' : ''}">${esc(item.description)}${dueHtml}
        <span class="source-label"> — ${esc(item.source_title)}</span></span>
      </div>`;
    });
  }

  // --- Key facts rollup across today's conversations ---
  if (data.key_facts.length) {
    html += '<h2 style="font-size:16px;margin-top:20px;">Key facts</h2><ul style="font-size:14px;padding-left:20px;">';
    data.key_facts.forEach(kf => {
      html += `<li>${esc(kf.fact)} <span class="source-label">— ${esc(kf.source_title)}</span></li>`;
    });
    html += '</ul>';
  }

  // --- Speaking style teasers: one line + tap-through to full detail ---
  if (data.speech_coaching.length) {
    html += '<h2 style="font-size:16px;margin-top:20px;">Speaking Style Feedback</h2>';
    data.speech_coaching.forEach(sc => {
      html += `<div class="list-row" onclick="goDetail('feedback', '${esc(sc.stem)}')">
        <div style="font-weight:600;">${esc(sc.title)}</div>
        <p style="font-size:13px;color:#8b949e;margin:4px 0 0;">${esc(sc.overall_take_preview)}</p>
      </div>`;
    });
  }

  // --- Lists teaser: named lists that got new items added today ---
  if (data.lists_today.length) {
    html += '<h2 style="font-size:16px;margin-top:20px;">Added to lists today</h2>';
    data.lists_today.forEach(l => {
      html += `<div class="list-row" onclick="goDetail('lists', '${esc(l.list_name)}')">
        <div style="display:flex;justify-content:space-between;">
          <div style="font-weight:600;">${esc(l.list_name)}</div>
          <div class="date-label">+${l.new_item_count}</div>
        </div>
      </div>`;
    });
  }

  document.getElementById('content').innerHTML = html;
}

async function renderConversationsList() {
  setHeader('Conversations', true, false);
  document.getElementById('content').innerHTML = 'Loading...';
  const data = await (await fetch('/api/conversations')).json();

  if (!data.length) {
    document.getElementById('content').innerHTML = '<p class="empty">No conversations yet.</p>';
    return;
  }

  let html = '';
  data.forEach(c => {
    const color = CATEGORY_COLORS[c.category] || CATEGORY_COLORS.other;
    html += `<div class="list-row" onclick="goDetail('conversations', '${esc(c.stem)}')">
      <div style="display:flex;justify-content:space-between;">
        <div style="font-weight:600;">${c.emoji || ''} ${esc(c.title)}</div>
        <div class="date-label">${esc(c.date)}</div>
      </div>
      <span class="badge" style="background:${color};">${esc(c.category)}</span>
      <p style="font-size:13px;color:#8b949e;margin:6px 0 0;">${esc(c.preview)}</p>
    </div>`;
  });
  document.getElementById('content').innerHTML = html;
}

async function renderConversationDetail(stem) {
  setHeader('', false, true);
  document.getElementById('content').innerHTML = 'Loading...';
  const res = await fetch(`/api/conversations/${encodeURIComponent(stem)}`);
  if (!res.ok) {
    document.getElementById('content').innerHTML = '<p class="empty">Not found.</p>';
    return;
  }
  const c = await res.json();
  const color = CATEGORY_COLORS[c.category] || CATEGORY_COLORS.other;

  let html = `<h1 style="margin-top:8px;">${c.emoji || ''} ${esc(c.title)}</h1>
    <div class="date-label" style="margin-bottom:6px;">${esc(c.date)}</div>
    <span class="badge" style="background:${color};">${esc(c.category)}</span>
    <p style="font-size:15px;line-height:1.6;margin-top:12px;">${esc(c.overview)}</p>`;

  if (c.key_facts.length) {
    html += '<h2 style="font-size:16px;">Key facts</h2><ul style="font-size:14px;">';
    c.key_facts.forEach(f => { html += `<li>${esc(f)}</li>`; });
    html += '</ul>';
  }

  if (c.action_items.length) {
    html += '<h2 style="font-size:16px;">Action items</h2>';
    c.action_items.forEach(item => {
      const dueHtml = item.due_date ? ` <span class="due">(due: ${esc(item.due_date)})</span>` : '';
      html += `<div class="todo-row" data-todo-id="${esc(item.id)}">
        <input type="checkbox" ${item.completed ? 'checked' : ''}
          onclick="toggleTodo('${item.id}', ${item.completed})">
        <span class="desc ${item.completed ? 'todo-done' : ''}">${esc(item.description)}${dueHtml}</span>
      </div>`;
    });
  }

  if (c.speech_coaching) {
    html += renderFeedbackCardHtml(c.speech_coaching, c.title);
  }

  document.getElementById('content').innerHTML = html;
}

async function renderTodos() {
  setHeader('To-Dos', true, false);
  document.getElementById('content').innerHTML = 'Loading...';
  const items = await (await fetch('/api/todos')).json();

  let html = `<div style="margin-bottom:12px;">
    <button onclick="goDetail('todos', 'completed')"
      style="background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:6px 12px;font-size:13px;">
      View Completed Today
    </button>
  </div>`;

  if (!items.length) {
    html += '<p class="empty">No open action items.</p>';
    document.getElementById('content').innerHTML = html;
    return;
  }

  items.forEach(item => {
    const dueHtml = item.due_date ? ` <span class="due">(due: ${esc(item.due_date)})</span>` : '';
    html += `<div class="todo-row" data-todo-id="${esc(item.id)}">
      <input type="checkbox" ${item.completed ? 'checked' : ''}
        onclick="toggleTodo('${item.id}', ${item.completed})">
      <span class="desc ${item.completed ? 'todo-done' : ''}">${esc(item.description)}${dueHtml}
      <span class="source-label" onclick="event.stopPropagation(); goDetail('conversations','${esc(item.source_stem)}')">
        — ${esc(item.source_title)} (${esc(item.date)})</span></span>
    </div>`;
  });
  document.getElementById('content').innerHTML = html;
}

async function renderCompletedTodos() {
  setHeader('', false, true);
  document.getElementById('content').innerHTML = 'Loading...';
  const items = await (await fetch('/api/todos/completed')).json();

  let html = '<h1 style="margin-top:8px;">Completed Today</h1>';

  if (!items.length) {
    html += '<p class="empty">Nothing checked off yet today.</p>';
    document.getElementById('content').innerHTML = html;
    return;
  }

  html += '<p style="color:#8b949e;font-size:13px;">Tap a checkbox to undo — this list clears itself at midnight, but nothing is ever actually deleted (it still shows in its original conversation).</p>';

  items.forEach(item => {
    const dueHtml = item.due_date ? ` <span class="due">(due: ${esc(item.due_date)})</span>` : '';
    html += `<div class="todo-row" data-todo-id="${esc(item.id)}">
      <input type="checkbox" checked onclick="toggleTodo('${item.id}', true)">
      <span class="desc todo-done">${esc(item.description)}${dueHtml}
      <span class="source-label" onclick="event.stopPropagation(); goDetail('conversations','${esc(item.source_stem)}')">
        — ${esc(item.source_title)} (${esc(item.date)})</span></span>
    </div>`;
  });
  document.getElementById('content').innerHTML = html;
}

async function renderFeedbackList() {
  setHeader('Speaking Style Feedback', true, false);
  document.getElementById('content').innerHTML = 'Loading...';
  const data = await (await fetch('/api/feedback')).json();

  if (!data.length) {
    document.getElementById('content').innerHTML =
      '<p class="empty">No speaking-style coaching run yet. Use speech_coach.py on a recording to generate one.</p>';
    return;
  }

  let html = '';
  data.forEach(sc => {
    html += `<div class="list-row" onclick="goDetail('feedback', '${esc(sc.stem)}')">
      <div style="display:flex;justify-content:space-between;">
        <div style="font-weight:600;">${esc(sc.title)}</div>
        <div class="date-label">${esc(sc.date)}</div>
      </div>
      <div style="font-size:12px;color:#8b949e;margin:4px 0;">${sc.words_per_minute} WPM</div>
      <p style="font-size:13px;color:#8b949e;margin:0;">${esc(sc.overall_take_preview)}</p>
    </div>`;
  });
  document.getElementById('content').innerHTML = html;
}

function renderFeedbackCardHtml(sc, title) {
  const pace = sc.metrics.pace;
  const fillers = sc.metrics.fillers;
  const fillerNote = fillers.total_filler_count ? `, ${fillers.total_filler_count} filler words` : '';
  const fb = sc.feedback;

  let html = `<h2 style="font-size:16px;margin-top:24px;">Speaking Style Feedback</h2>
    <div class="card">
      <div style="font-size:12px;color:#8b949e;margin-bottom:8px;">
        ${pace.words_per_minute} WPM, ${pace.duration_seconds}s${fillerNote}
      </div>`;

  if (fb.strengths && fb.strengths.length) {
    html += '<div style="font-size:13px;"><strong>Strengths:</strong></div><ul style="font-size:13px;margin:4px 0 10px;padding-left:20px;">';
    fb.strengths.forEach(s => { html += `<li>${esc(s)}</li>`; });
    html += '</ul>';
  }

  if (fb.areas_to_improve && fb.areas_to_improve.length) {
    html += '<div style="font-size:13px;"><strong>Areas to improve:</strong></div>';
    fb.areas_to_improve.forEach(area => {
      html += `<div style="font-size:13px;margin:6px 0 10px;">
        ${esc(area.observation)}<br>
        <span style="color:#8b949e;">Example: "${esc(area.example)}"</span><br>
        <span style="color:#3fb950;">Try instead: ${esc(area.suggestion)}</span>
      </div>`;
    });
  }

  if (fb.pace_feedback) html += `<p style="font-size:13px;"><strong>Pace:</strong> ${esc(fb.pace_feedback)}</p>`;
  if (fb.overall_take) html += `<p style="font-size:13px;"><strong>Overall:</strong> ${esc(fb.overall_take)}</p>`;

  html += '</div>';
  return html;
}

async function renderFeedbackDetail(stem) {
  setHeader('', false, true);
  document.getElementById('content').innerHTML = 'Loading...';
  const res = await fetch(`/api/feedback/${encodeURIComponent(stem)}`);
  if (!res.ok) {
    document.getElementById('content').innerHTML = '<p class="empty">Not found.</p>';
    return;
  }
  const sc = await res.json();
  document.getElementById('content').innerHTML =
    `<h1 style="margin-top:8px;">${esc(sc.title)}</h1>` + renderFeedbackCardHtml(sc, sc.title);
}

async function renderListsList() {
  setHeader('Lists', true, false);
  document.getElementById('content').innerHTML = 'Loading...';
  const data = await (await fetch('/api/lists')).json();

  if (!data.length) {
    document.getElementById('content').innerHTML =
      '<p class="empty">Nothing yet. Mention wanting to check something out (a movie, restaurant, etc.) and it will show up here.</p>';
    return;
  }

  let html = '';
  data.forEach(l => {
    html += `<div class="list-row" onclick="goDetail('lists', '${esc(l.list_name)}')">
      <div style="display:flex;justify-content:space-between;align-items:baseline;">
        <div style="font-weight:600;">${esc(l.list_name)}</div>
        <div class="date-label">${l.item_count} item${l.item_count === 1 ? '' : 's'}</div>
      </div>
    </div>`;
  });
  document.getElementById('content').innerHTML = html;
}

async function renderListDetail(listName) {
  setHeader('', false, true);
  document.getElementById('content').innerHTML = 'Loading...';
  const res = await fetch(`/api/lists/${encodeURIComponent(listName)}`);
  if (!res.ok) {
    document.getElementById('content').innerHTML = '<p class="empty">Not found.</p>';
    return;
  }
  const data = await res.json();

  let html = `<h1 style="margin-top:8px;">${esc(data.list_name)}</h1>`;
  if (!data.items.length) {
    html += '<p class="empty">Nothing left on this list.</p>';
  } else {
    data.items.forEach(item => {
      html += `<div class="todo-row" data-todo-id="${esc(item.id)}">
        <input type="checkbox" onclick="toggleTodo('${item.id}', false)">
        <span class="desc">${esc(item.text)}
        <span class="source-label" onclick="event.stopPropagation(); goDetail('conversations','${esc(item.source_stem)}')">
          — ${esc(item.source_title)} (${esc(item.date)})</span></span>
      </div>`;
    });
  }
  document.getElementById('content').innerHTML = html;
}

async function toggleTodo(id, currentlyDone) {
  await fetch(`/api/todo/${encodeURIComponent(id)}/toggle`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({current_default: currentlyDone})
  });
  // Re-render (not just patch in place) so an item that should now disappear
  // from view — completed items leaving the open list, un-completed items
  // leaving the Completed view — actually does so immediately, rather than
  // sitting there crossed-out until the next manual refresh.
  render(currentState());
}

// Initial load: parse the URL hash if present (e.g. a bookmark or restored
// session), rather than always defaulting to Today regardless of the URL.
function parseInitialState() {
  const hash = window.location.hash.replace(/^#/, '');
  if (!hash) return { tab: 'today', detail: null };
  const [tab, rawDetail] = hash.split('/');
  const validTabs = ['today', 'conversations', 'todos', 'feedback', 'lists'];
  if (!validTabs.includes(tab)) return { tab: 'today', detail: null };
  return { tab, detail: rawDetail ? decodeURIComponent(rawDetail) : null };
}

if (!history.state) history.replaceState(parseInitialState(), '');
render(currentState());
</script>
</body>
</html>"""


@app.route("/")
def index():
    return _PAGE_TEMPLATE


if __name__ == "__main__":
    config.ensure_dirs()
    # 127.0.0.1 ONLY — see module docstring. Exposed to your Tailscale
    # network via `tailscale serve`, never bound to 0.0.0.0 directly.
    app.run(host="127.0.0.1", port=5001)
