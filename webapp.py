"""
On-demand web dashboard: today's conversations, action items (with real,
functioning checkboxes — unlike email, which can never do this), and
speaking-style feedback if any was run today.

Deliberately lightweight: this reads existing JSON files off disk (the
same ones digest.py already produces) and serves them — no GPU, no LLM
calls, negligible resource footprint at rest, safe to leave running
continuously alongside robotics work on the same Thor.

Meant to run on 127.0.0.1 ONLY (see webapp.service) and be exposed to your
Tailscale network via `tailscale serve` — never bind this to 0.0.0.0 or a
LAN-reachable interface directly; Tailscale's serve feature is what makes
it reachable from anywhere without ever exposing it publicly.

Run with:
    python3 webapp.py
or as a persistent service — see webapp.service.
"""
from datetime import date

from flask import Flask, jsonify, request

import config
import digest
import todo_state

app = Flask(__name__)


def _serialize_today() -> dict:
    today = date.today()
    analyses = digest.load_day_analyses(today)
    speech_coaching = digest.load_day_speech_coaching(today)

    conversations = []
    for a in analyses:
        stem = a.get("_stem", "")
        action_items = []
        for i, item in enumerate(a.get("action_items", [])):
            item_id = f"{stem}:{i}"
            action_items.append(
                {
                    "id": item_id,
                    "description": item["description"],
                    "due_date": item.get("due_date"),
                    "completed": todo_state.is_completed(item_id, item.get("completed", False)),
                }
            )
        conversations.append(
            {
                "stem": stem,
                "title": a.get("title", stem),
                "emoji": a.get("emoji", ""),
                "category": a.get("category", "uncategorized"),
                "overview": a.get("overview", ""),
                "key_facts": a.get("key_facts", []),
                "action_items": action_items,
            }
        )

    return {
        "date": today.isoformat(),
        "conversations": conversations,
        "speech_coaching": speech_coaching,
    }


@app.route("/api/today")
def api_today():
    return jsonify(_serialize_today())


@app.route("/api/todo/<path:item_id>/toggle", methods=["POST"])
def api_toggle_todo(item_id: str):
    # current_default lets a fresh toggle correctly flip away from whatever
    # the LLM originally set (usually false) even before any state file
    # entry exists for this item yet.
    default = request.json.get("current_default", False) if request.is_json else False
    new_state = todo_state.toggle(item_id, default)
    return jsonify({"id": item_id, "completed": new_state})


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#0d1117">
<title>Omi Daily Digest</title>
<style>
  body { font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 600px;
         margin: 0 auto; padding: 16px; color: #e6edf3; background: #0d1117; }
  h1 { font-size: 20px; display: flex; justify-content: space-between; align-items: center;
       color: #e6edf3; }
  h2 { color: #e6edf3; }
  #refresh-btn { background: #1f6feb; color: #ffffff; border: none; border-radius: 6px;
                 padding: 8px 16px; font-size: 14px; }
  #refresh-btn:active { opacity: 0.75; }
  #last-updated { color: #8b949e; font-size: 12px; margin-top: -8px; margin-bottom: 16px; }
  .card { border: 1px solid #30363d; border-radius: 8px; padding: 14px 16px;
          margin-bottom: 12px; background: #161b22; }
  .badge { display: inline-block; color: #ffffff; font-size: 11px; padding: 2px 8px;
           border-radius: 10px; margin: 6px 0; }
  .todo-row { display: flex; align-items: baseline; padding: 6px 0; border-bottom: 1px solid #21262d; }
  .todo-row input { margin-right: 10px; width: 18px; height: 18px; }
  .todo-row .desc { color: #e6edf3; }
  .todo-done { text-decoration: line-through; color: #6e7681; }
  .due { color: #ff9662; font-size: 12px; }
  .empty { color: #8b949e; }
  .source-label { color: #6e7681; font-size: 12px; }
</style>
</head>
<body>
  <h1>Omi Daily Digest <button id="refresh-btn" onclick="loadData()">&#8635; Refresh</button></h1>
  <div id="last-updated"></div>
  <div id="content">Loading...</div>

<script>
const CATEGORY_COLORS = {
  personal: "#725ff4", work: "#0073e3", education: "#1b8569",
  health: "#e02f00", finance: "#158482", social: "#e7006f", other: "#6b7580"
};

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function toggleTodo(id, currentlyDone) {
  const res = await fetch(`/api/todo/${encodeURIComponent(id)}/toggle`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({current_default: currentlyDone})
  });
  const data = await res.json();
  const row = document.querySelector(`[data-todo-id="${CSS.escape(id)}"]`);
  if (row) {
    row.querySelector('input').checked = data.completed;
    row.querySelector('.desc').classList.toggle('todo-done', data.completed);
  }
}

async function loadData() {
  document.getElementById('content').innerHTML = 'Loading...';
  const res = await fetch('/api/today');
  const data = await res.json();

  document.getElementById('last-updated').textContent =
    'Last refreshed: ' + new Date().toLocaleTimeString();

  let html = '';

  const allItems = [];
  data.conversations.forEach(c => {
    c.action_items.forEach(item => allItems.push({...item, source: c.title}));
  });

  if (allItems.length) {
    html += '<h2 style="font-size:16px;">Action items</h2>';
    allItems.forEach(item => {
      const dueHtml = item.due_date ? ` <span class="due">(due: ${esc(item.due_date)})</span>` : '';
      html += `<div class="todo-row" data-todo-id="${esc(item.id)}">
        <input type="checkbox" ${item.completed ? 'checked' : ''}
          onclick="toggleTodo('${item.id}', ${item.completed})">
        <span class="desc ${item.completed ? 'todo-done' : ''}">${esc(item.description)}${dueHtml}
        <span style="color:#b2bec3;font-size:12px;"> — ${esc(item.source)}</span></span>
      </div>`;
    });
  }

  if (!data.conversations.length) {
    html += '<p class="empty">Nothing recorded yet today.</p>';
  } else {
    html += '<h2 style="font-size:16px;margin-top:20px;">Conversations</h2>';
    data.conversations.forEach(c => {
      const color = CATEGORY_COLORS[c.category] || CATEGORY_COLORS.other;
      html += `<div class="card">
        <div style="font-size:16px;font-weight:600;">${c.emoji || ''} ${esc(c.title)}</div>
        <span class="badge" style="background:${color};">${esc(c.category)}</span>
        <p style="font-size:14px;line-height:1.5;">${esc(c.overview)}</p>
      </div>`;
    });
  }

  if (data.speech_coaching && data.speech_coaching.length) {
    html += '<h2 style="font-size:16px;margin-top:20px;">Speaking Style Feedback</h2>';
    data.speech_coaching.forEach(sc => {
      const pace = sc.metrics.pace;
      const fillers = sc.metrics.fillers;
      const fillerNote = fillers.total_filler_count
        ? `, ${fillers.total_filler_count} filler words` : '';
      const fb = sc.feedback;

      html += `<div class="card">
        <div style="font-weight:600;">${esc(sc._stem)}</div>
        <div style="font-size:12px;color:#b2bec3;margin-bottom:8px;">
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

      if (fb.pace_feedback) {
        html += `<p style="font-size:13px;"><strong>Pace:</strong> ${esc(fb.pace_feedback)}</p>`;
      }
      if (fb.overall_take) {
        html += `<p style="font-size:13px;"><strong>Overall:</strong> ${esc(fb.overall_take)}</p>`;
      }

      html += '</div>';
    });
  }

  document.getElementById('content').innerHTML = html;
}

loadData();
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
