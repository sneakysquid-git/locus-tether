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
import json
import logging
import sys
from datetime import date, datetime, timedelta
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
import conversation_state
import speaker_profiles
import digest_preferences

log = logging.getLogger("omi.webapp")

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
                "owner": item.get("owner"),
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
        "time": a.get("_time", ""),
        "title": a.get("title", a.get("_stem", "")),
        "category": a.get("category", "uncategorized"),
        "preview": preview,
        "action_item_count": len(a.get("action_items", [])),
    }


def _full_conversation(a: dict) -> dict:
    stem = a.get("_stem", "")
    speech = data_store.load_speech_coaching_by_stem(stem)

    # Approximate start/end time span: "_time"/"_timestamp" reflect when
    # the file was written (roughly end-of-processing, shortly after the
    # recording itself ended) — an honest approximation, not a precisely
    # recorded wall-clock start, but genuinely useful for telling same-day
    # recordings apart and understanding roughly when something happened.
    end_time = a.get("_time", "")
    start_time = None
    duration = data_store.get_recording_duration(stem)
    if duration and a.get("_timestamp"):
        start_dt = datetime.fromtimestamp(a["_timestamp"] - duration)
        start_time = start_dt.strftime("%-I:%M %p")

    return {
        "stem": stem,
        "date": a.get("_date", ""),
        "time": end_time,
        "start_time": start_time,
        "title": a.get("title", stem),
        "category": a.get("category", "uncategorized"),
        "overview": a.get("overview", ""),
        "atmosphere": a.get("atmosphere"),
        "speaker_count": data_store.get_speaker_count(stem),
        "participants": [p for p in a.get("participants", []) if p.get("name")],
        "key_points": a.get("key_points", []),
        "decisions_made": a.get("decisions_made", []),
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

    Archived conversations are excluded too — "deleting" a conversation
    from the webapp should also stop its still-open action items from
    cluttering the active To-Dos list, consistent with archiving meaning
    "fully out of the way" everywhere, not just the Conversations tab.
    """
    archived = conversation_state.get_all_archived()
    items = []
    for a in data_store.load_all_analyses():
        source_stem = a.get("_stem", "")
        if source_stem in archived:
            continue
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
        "most_recent_timestamp": max((i.get("timestamp", 0) for i in open_items), default=0),
    }


# --- API: Today (brief, but with real value beyond just a to-do list) -----

@app.route("/api/today")
def api_today():
    today = date.today()
    tomorrow = today + timedelta(days=1)
    archived = conversation_state.get_all_archived()
    analyses = [a for a in data_store.load_day_analyses(today) if a.get("_stem") not in archived]
    speech_coaching = data_store.load_day_speech_coaching(today)

    conversations = [
        {
            "stem": a.get("_stem", ""),
            "title": a.get("title", a.get("_stem", "")),
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
    archived = conversation_state.get_all_archived()
    analyses = [a for a in data_store.load_all_analyses() if a.get("_stem") not in archived]
    return jsonify([_condensed_conversation(a) for a in analyses])


@app.route("/api/conversations/<path:stem>")
def api_conversation_detail(stem: str):
    a = data_store.load_analysis_by_stem(stem)
    if a is None:
        abort(404)
    return jsonify(_full_conversation(a))


@app.route("/api/conversations/<path:stem>", methods=["PUT"])
def api_update_conversation(stem: str):
    """
    Full-content editing — unlike todo_state's separate overlay for
    completion status, an edit here becomes the new source of truth
    directly in the analysis file. Only touches fields actually present in
    the request body, so a partial edit (e.g. just fixing the title)
    doesn't require resending everything.
    """
    a = data_store.load_analysis_by_stem(stem)
    if a is None:
        abort(404)

    try:
        body = request.get_json(force=True) or {}

        for field in ("title", "overview", "atmosphere", "category"):
            if field in body:
                a[field] = body[field]

        for field in ("key_facts", "key_points", "decisions_made"):
            if field in body:
                a[field] = [str(x).strip() for x in body[field] if str(x).strip()]

        if "participants" in body:
            a["participants"] = [
                {"name": str(p.get("name") or "").strip(), "role": str(p.get("role") or "").strip() or None}
                for p in body["participants"]
                if str(p.get("name") or "").strip()
            ]

        if "action_items" in body:
            # completed status is deliberately preserved from the existing
            # stored item (by position) rather than accepted from the edit
            # payload — that's todo_state's job exclusively, never
            # overwritten by a content edit. A brand new item added during
            # editing starts as not-completed.
            existing = a.get("action_items", [])
            new_items = []
            for i, item in enumerate(body["action_items"]):
                description = str(item.get("description") or "").strip()
                if not description:
                    continue
                completed = False
                if i < len(existing) and isinstance(existing[i], dict):
                    completed = existing[i].get("completed", False)
                new_items.append(
                    {
                        "description": description,
                        "due_date": str(item.get("due_date") or "").strip() or None,
                        "owner": str(item.get("owner") or "").strip() or None,
                        "completed": completed,
                    }
                )
            a["action_items"] = new_items

        data_store.save_analysis(stem, a)

        updated = data_store.load_analysis_by_stem(stem)
        if updated is None:
            # The save above wrote the file successfully — this would only
            # happen if reloading it immediately afterward somehow failed
            # (a real, if unlikely, race/IO edge case). Report it clearly
            # rather than crash on a bare None a line further down.
            raise RuntimeError("Save appeared to succeed, but reloading the saved file failed")
        response_body = _full_conversation(updated)
    except Exception as e:
        # Log the full traceback server-side (visible via journalctl -u
        # webapp) AND return the actual reason to the client directly —
        # a generic 500 page with no detail just means another round-trip
        # of guessing before we can actually fix the real cause.
        log.exception("Failed to save conversation edit for stem=%s", stem)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    return jsonify(response_body)


@app.route("/api/conversations/<path:stem>/archive", methods=["POST"])
def api_archive_conversation(stem: str):
    if data_store.load_analysis_by_stem(stem) is None:
        abort(404)
    conversation_state.archive(stem)
    return jsonify({"stem": stem, "archived": True})


@app.route("/api/conversations/<path:stem>/unarchive", methods=["POST"])
def api_unarchive_conversation(stem: str):
    conversation_state.unarchive(stem)
    return jsonify({"stem": stem, "archived": False})


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
    archived = conversation_state.get_all_archived()
    groups = []
    for g in data_store.aggregate_lists():
        g = {**g, "items": [i for i in g["items"] if i.get("source_stem") not in archived]}
        groups.append(g)
    condensed = [_condensed_list(g) for g in groups]
    condensed = [c for c in condensed if c["item_count"] > 0]  # fully-checked-off lists just disappear
    condensed.sort(key=lambda c: c["most_recent_timestamp"], reverse=True)
    return jsonify(condensed)


@app.route("/api/lists/<path:list_name>")
def api_list_detail(list_name: str):
    matching = next(
        (g for g in data_store.aggregate_lists() if g["list_name"].lower() == list_name.lower()),
        None,
    )
    if matching is None:
        abort(404)
    archived = conversation_state.get_all_archived()
    open_items = [
        i for i in matching["items"]
        if not todo_state.is_completed(i["id"], False) and i.get("source_stem") not in archived
    ]
    return jsonify({"list_name": matching["list_name"], "items": open_items})


# --- API: Speakers (#37, voice enrollment) --------------------------------

@app.route("/api/speakers")
def api_speakers():
    return jsonify(speaker_profiles.list_profiles())


@app.route("/api/speakers/enroll", methods=["POST"])
def api_enroll_speaker():
    """
    Accepts a name, an optional is_main_user flag, and an audio file
    (multipart form data — webapp.py has no ML dependencies itself, so it
    just hands the sample off to watcher.py via a shared directory, the
    same pattern as the main inbox). Returns immediately; enrollment
    actually completes asynchronously once watcher.py processes it (see
    /api/speakers/enroll-status below for polling).
    """
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400

    audio_file = request.files.get("audio")
    if audio_file is None or not audio_file.filename:
        return jsonify({"error": "An audio sample is required"}), 400

    is_main_user = request.form.get("is_main_user") == "true"

    ext = Path(audio_file.filename).suffix or ".wav"
    audio_path = config.ENROLLMENT_DIR / f"{name}{ext}"
    sidecar_path = config.ENROLLMENT_DIR / f"{name}.json"

    audio_file.save(str(audio_path))
    sidecar_path.write_text(
        json.dumps({"name": name, "is_main_user": is_main_user}), encoding="utf-8"
    )

    return jsonify({"status": "processing", "name": name})


@app.route("/api/speakers/enroll-status/<path:name>")
def api_enroll_status(name: str):
    """
    Lets the webapp poll after submitting an enrollment, since the actual
    embedding extraction happens asynchronously in watcher.py (which has
    the ML dependencies webapp.py deliberately doesn't).
    """
    profiles = speaker_profiles.list_profiles()
    if any(p["name"] == name for p in profiles):
        return jsonify({"status": "done"})

    still_pending = any(
        p.stem == name and p.suffix.lower() != ".json"
        for p in config.ENROLLMENT_DIR.iterdir()
    )
    return jsonify({"status": "processing" if still_pending else "failed"})


@app.route("/api/speakers/<path:name>/set-main", methods=["POST"])
def api_set_main_speaker(name: str):
    try:
        speaker_profiles.set_main_user(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"main_user": name})


@app.route("/api/speakers/<path:name>", methods=["DELETE"])
def api_delete_speaker(name: str):
    speaker_profiles.delete_profile(name)
    return jsonify({"deleted": name})


# --- API: Settings — digest email preferences -------------------------------

@app.route("/api/settings/digest")
def api_get_digest_settings():
    prefs = digest_preferences.get_preferences()
    if prefs is None:
        # Nothing explicitly saved via the webapp yet — reflect whatever
        # the env-var config currently has, so the Settings UI shows
        # accurate current state rather than defaulting to blank/off when
        # a host-level .env.digest config might already be active.
        prefs = {"enabled": config.DIGEST_EMAIL_ENABLED, "email": config.DIGEST_EMAIL_TO}
    return jsonify(prefs)


@app.route("/api/settings/digest", methods=["POST"])
def api_save_digest_settings():
    body = request.get_json(force=True) or {}
    enabled = bool(body.get("enabled", False))
    email = (body.get("email") or "").strip()
    if enabled and not email:
        return jsonify({"error": "An email address is required to enable the daily digest"}), 400
    digest_preferences.set_preferences(enabled, email)
    return jsonify({"enabled": enabled, "email": email})


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
  :root {
    /* Spacing scale (#18) — every margin/padding in this app references one
       of these instead of an ad hoc pixel value, so spacing rhythm stays
       consistent as the app grows rather than drifting further with every
       new feature added. */
    --space-0: 2px;
    --space-1: 4px;
    --space-2: 8px;
    --space-3: 12px;
    --space-4: 16px;
    --space-5: 20px;
    --space-6: 24px;
    --space-7: 32px;

    /* Type scale (#19) — 5 deliberate steps instead of the 7 ad hoc sizes
       that had accumulated (11/12/13/14/15/16/20px), so there's an actual
       visual hierarchy pulling the eye to what matters on a given screen. */
    --text-xs: 11px;   /* category badges, micro labels */
    --text-sm: 13px;   /* metadata, source labels, dates, secondary text */
    --text-base: 14px; /* body text, descriptions, list item text */
    --text-md: 16px;   /* card titles, section headers (h2) */
    --text-lg: 20px;   /* page title (h1) */

    /* Fixed bottom tab bar height clearance for scrolling content —
       a functional value, not a decorative spacing choice, so it's kept
       separate from the spacing scale above. */
    --tabbar-clearance: 76px;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 600px;
         margin: 0 auto; padding: var(--space-4) var(--space-4) var(--tabbar-clearance); color: #e6edf3; background: #0d1117; }
  h1, h2 { color: #e6edf3; }
  h1 { font-size: var(--text-lg); display: flex; justify-content: space-between; align-items: center; }
  #refresh-btn { background: #1f6feb; color: #ffffff; border: none; border-radius: 6px;
                 padding: var(--space-2) var(--space-4); font-size: var(--text-base); }
  #refresh-btn:active { opacity: 0.75; }
  #back-btn { background: none; border: none; color: #58a6ff; font-size: var(--text-md); padding: var(--space-2) 0;
              display: flex; align-items: center; gap: 4px; }
  #last-updated { color: #8b949e; font-size: var(--text-sm); margin-top: -8px; margin-bottom: var(--space-4); }
  .card { border: 1px solid #30363d; border-radius: 8px; padding: var(--space-3) var(--space-4);
          margin-bottom: var(--space-3); background: #161b22; }
  .list-row { border: 1px solid #30363d; border-radius: 8px; padding: var(--space-3) var(--space-3);
              margin-bottom: var(--space-2); background: #161b22;
              -webkit-user-select: none; user-select: none; cursor: pointer; }
  .list-row:active { background: #1c2230; }
  .badge { display: inline-block; color: #ffffff; font-size: var(--text-xs); padding: var(--space-0) var(--space-2);
           border-radius: 10px; margin: var(--space-2) 0; }
  .todo-row { display: flex; align-items: baseline; padding: var(--space-2) 0; border-bottom: 1px solid #21262d;
              -webkit-user-select: none; user-select: none; }
  .todo-row input[type="checkbox"] {
    appearance: none; -webkit-appearance: none;
    margin-right: var(--space-2); width: 20px; height: 20px; flex-shrink: 0;
    border: 2px solid #30363d; border-radius: 6px; background: #161b22;
    cursor: pointer; position: relative; top: 2px;
    transition: background-color 0.15s ease, border-color 0.15s ease;
  }
  .todo-row input[type="checkbox"]:checked {
    background: #1f6feb; border-color: #1f6feb;
  }
  .todo-row input[type="checkbox"]:checked::after {
    content: ""; position: absolute; left: 6px; top: 2px;
    width: 5px; height: 10px;
    border: solid #ffffff; border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }
  .todo-row input[type="checkbox"]:focus-visible {
    outline: 2px solid #58a6ff; outline-offset: 2px;
  }
  .todo-row .desc { color: #e6edf3; }
  .todo-done { text-decoration: line-through; color: #6e7681; transition: color 0.3s ease; }
  .due { color: #ff9662; font-size: var(--text-sm); }
  .empty { color: #8b949e; }
  .source-label { color: #6e7681; font-size: var(--text-sm);
                  -webkit-user-select: none; user-select: none; cursor: pointer; }
  .date-label { color: #6e7681; font-size: var(--text-xs); }

  #tabbar { position: fixed; bottom: 0; left: 0; right: 0; background: #161b22;
            border-top: 1px solid #30363d; display: flex; max-width: 600px; margin: 0 auto; }
  #tabbar button { flex: 1; background: none; border: none; color: #6e7681; padding: var(--space-3) var(--space-1);
                   font-size: var(--text-sm); display: flex; flex-direction: column; align-items: center; gap: 2px; }
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
// Category colors (#17): designed as an intentional set, not just
// contrast-math-driven — consistent saturation (75%) with only hue varying
// per category, and lightness tuned per-hue so all six converge on
// essentially the same contrast ratio against white text (~5.5:1), which
// reads as a consistently "bold" set rather than some categories looking
// bolder than others just because certain hues are perceptually lighter at
// the same raw lightness value (yellow/green vs. blue/red, for example).
// "other" is deliberately desaturated/neutral rather than part of the hue
// wheel, since it's a fallback category, not meant to compete visually.
const CATEGORY_COLORS = {
  work: "#1c68c4",       // blue — professional, trustworthy
  personal: "#7e43e4",   // purple — warm, distinct from work
  social: "#c61c71",     // pink/magenta — vibrant, communicative
  health: "#c82b1d",     // red/coral — vitality
  education: "#896214",  // amber/gold — achievement
  finance: "#117845",    // green — money, growth
  other: "#6b7580"        // neutral gray — deliberately not part of the hue wheel
};

// Category icons — deliberately replacing the LLM-generated per-conversation
// emoji field entirely (removed from the prompt schema too, not just hidden
// here). One consistent, controlled icon per category instead of an
// unreliable per-conversation model choice — same outline-icon style as the
// tab bar (#11/#21), inheriting color via currentColor.
const CATEGORY_ICON_PATHS = {
  personal: '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
  work: '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>',
  education: '<path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c0 1.7 2.7 3 6 3s6-1.3 6-3v-5"/>',
  health: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8z"/>',
  finance: '<line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
  social: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  other: '<path d="M20.59 13.41L11 3.83A2 2 0 0 0 9.59 3.24H4a1 1 0 0 0-1 1v5.59a2 2 0 0 0 .59 1.41l9.58 9.59a2 2 0 0 0 2.82 0l5.6-5.6a2 2 0 0 0 0-2.82z"/><circle cx="7.5" cy="7.5" r="1"/>',
};

function categoryIcon(category) {
  const path = CATEGORY_ICON_PATHS[category] || CATEGORY_ICON_PATHS.other;
  return `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ` +
    `style="vertical-align:-3px;">${path}</svg>`;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : s;
  return d.innerHTML;
}

// Small owner-attribution tag, shown next to an action item only when it
// has one (multi-person meetings with named participants) — stays absent
// entirely for personal-conversation action items, where owner is null.
function ownerLabel(item) {
  return item.owner ? ` <span style="color:#58a6ff;font-size:var(--text-sm);">(${esc(item.owner)})</span>` : '';
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
  if (state.tab === 'settings') return renderSettings();
}

function setHeader(title, showRefresh, showBack) {
  const backHtml = showBack
    ? `<button id="back-btn" onclick="history.back()">&#8592; Back</button>` : '';
  const settingsHtml = showRefresh
    ? `<button onclick="goTab('settings')" style="background:none;border:none;color:#8b949e;padding:4px 8px;margin-right:var(--space-2);" aria-label="Settings">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-5px;">
          <circle cx="12" cy="12" r="3"/>
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
        </svg>
      </button>` : '';
  const refreshHtml = showRefresh
    ? `<button id="refresh-btn" onclick="render(currentState())">&#8635; Refresh</button>` : '';
  document.getElementById('header').innerHTML =
    showBack ? backHtml : `<h1>${esc(title)} <span>${settingsHtml}${refreshHtml}</span></h1>`;
  document.getElementById('last-updated').textContent = showRefresh
    ? 'Last refreshed: ' + new Date().toLocaleTimeString() : '';
}

async function renderToday() {
  setHeader('LocusTether', true, false);
  document.getElementById('content').innerHTML = 'Loading...';
  const data = await (await fetch('/api/today')).json();

  let html = `<p style="color:#8b949e;font-size:var(--text-base);">
    ${data.conversation_count} conversation(s) today</p>`;

  if (!data.conversation_count && !data.action_items.length && !data.due_soon.length) {
    html += '<p class="empty">Nothing recorded yet today. Pull up the Conversations or To-Dos tabs for older history.</p>';
    document.getElementById('content').innerHTML = html;
    return;
  }

  // --- Due soon: pulled out from the general list since "due tomorrow"
  // deserves more attention than "no deadline at all". ---
  if (data.due_soon.length) {
    html += '<h2 style="font-size:var(--text-md);">Due soon</h2>';
    data.due_soon.forEach(item => {
      html += `<div class="todo-row" style="border-left:3px solid #ff9662;padding-left:var(--space-2);"
        data-todo-id="${esc(item.id)}" onclick="goDetail('conversations','${esc(item.source_stem)}')">
        <input type="checkbox" ${item.completed ? 'checked' : ''}
          onclick="event.stopPropagation(); toggleTodo('${item.id}', ${item.completed})">
        <span class="desc ${item.completed ? 'todo-done' : ''}">${esc(item.description)}
        <span class="due">(due: ${esc(item.due_date)})</span>${ownerLabel(item)}
        <span class="source-label"> — ${esc(item.source_title)}</span></span>
      </div>`;
    });
  }

  // --- Today's conversations: compact, tappable rows into full detail ---
  if (data.conversations.length) {
    html += '<h2 style="font-size:var(--text-md);margin-top:var(--space-5);">Conversations today</h2>';
    data.conversations.forEach(c => {
      const color = CATEGORY_COLORS[c.category] || CATEGORY_COLORS.other;
      html += `<div class="list-row" style="padding:var(--space-2) var(--space-3);" onclick="goDetail('conversations', '${esc(c.stem)}')">
        <span style="font-weight:600;">${categoryIcon(c.category)} ${esc(c.title)}</span>
        <span class="badge" style="background:${color};margin-left:var(--space-2);">${esc(c.category)}</span>
      </div>`;
    });
  }

  // --- Remaining action items (due-soon ones already shown above) ---
  if (data.action_items.length) {
    html += '<h2 style="font-size:var(--text-md);margin-top:var(--space-5);">Action items</h2>';
    data.action_items.forEach(item => {
      const dueHtml = item.due_date ? ` <span class="due">(due: ${esc(item.due_date)})</span>` : '';
      html += `<div class="todo-row" data-todo-id="${esc(item.id)}" onclick="goDetail('conversations','${esc(item.source_stem)}')">
        <input type="checkbox" ${item.completed ? 'checked' : ''}
          onclick="event.stopPropagation(); toggleTodo('${item.id}', ${item.completed})">
        <span class="desc ${item.completed ? 'todo-done' : ''}">${esc(item.description)}${dueHtml}${ownerLabel(item)}
        <span class="source-label"> — ${esc(item.source_title)}</span></span>
      </div>`;
    });
  }

  // --- Key facts rollup across today's conversations ---
  if (data.key_facts.length) {
    html += '<h2 style="font-size:var(--text-md);margin-top:var(--space-5);">Key facts</h2><ul style="font-size:var(--text-base);padding-left:var(--space-5);">';
    data.key_facts.forEach(kf => {
      html += `<li>${esc(kf.fact)} <span class="source-label">— ${esc(kf.source_title)}</span></li>`;
    });
    html += '</ul>';
  }

  // --- Speaking style teasers: one line + tap-through to full detail ---
  if (data.speech_coaching.length) {
    html += '<h2 style="font-size:var(--text-md);margin-top:var(--space-5);">Speaking Style Feedback</h2>';
    data.speech_coaching.forEach(sc => {
      html += `<div class="list-row" onclick="goDetail('feedback', '${esc(sc.stem)}')">
        <div style="font-weight:600;">${esc(sc.title)}</div>
        <p style="font-size:var(--text-sm);color:#8b949e;margin:var(--space-1) 0 0;">${esc(sc.overall_take_preview)}</p>
      </div>`;
    });
  }

  // --- Lists teaser: named lists that got new items added today ---
  if (data.lists_today.length) {
    html += '<h2 style="font-size:var(--text-md);margin-top:var(--space-5);">Added to lists today</h2>';
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
        <div style="font-weight:600;">${categoryIcon(c.category)} ${esc(c.title)}</div>
        <div class="date-label">${esc(c.date)} ${esc(c.time)}</div>
      </div>
      <span class="badge" style="background:${color};">${esc(c.category)}</span>
      <p style="font-size:var(--text-sm);color:#8b949e;margin:var(--space-2) 0 0;">${esc(c.preview)}</p>
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
  window._currentConversation = c;
  const color = CATEGORY_COLORS[c.category] || CATEGORY_COLORS.other;

  let html = `<h1 style="margin-top:var(--space-2);justify-content:flex-start;gap:var(--space-2);">${categoryIcon(c.category)} ${esc(c.title)}</h1>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--space-2);">
      <div class="date-label">${esc(c.date)} — ${c.start_time ? esc(c.start_time) + ' to ' + esc(c.time) : esc(c.time)}</div>
      <div>
        <button onclick="renderConversationEditForm('${stem}')"
          style="background:#21262d;color:#58a6ff;border:1px solid #30363d;border-radius:6px;padding:4px 10px;font-size:var(--text-sm);margin-right:var(--space-1);">Edit</button>
        <button onclick="deleteConversation('${stem}')"
          style="background:#21262d;color:#f85149;border:1px solid #30363d;border-radius:6px;padding:4px 10px;font-size:var(--text-sm);">Delete</button>
      </div>
    </div>
    <span class="badge" style="background:${color};">${esc(c.category)}</span>`;

  if (c.speaker_count) {
    const label = c.speaker_count === 1 ? '1 speaker detected' : `${c.speaker_count} speakers detected`;
    html += ` <span style="color:#8b949e;font-size:var(--text-sm);">${label}</span>`;
  }

  html += `<p style="font-size:var(--text-md);line-height:1.6;margin-top:var(--space-3);">${esc(c.overview)}</p>`;

  if (c.atmosphere) {
    html += `<p style="font-size:var(--text-sm);color:#8b949e;font-style:italic;margin-top:calc(-1 * var(--space-2));">${esc(c.atmosphere)}</p>`;
  }

  if (c.participants && c.participants.length) {
    html += '<div style="margin-top:var(--space-2);">';
    c.participants.forEach(p => {
      const roleText = p.role ? ` — ${esc(p.role)}` : '';
      html += `<span style="display:inline-block;background:#21262d;border:1px solid #30363d;border-radius:12px;
        padding:2px 10px;margin:0 var(--space-1) var(--space-1) 0;font-size:var(--text-sm);">${esc(p.name)}${roleText}</span>`;
    });
    html += '</div>';
  }

  if (c.key_points && c.key_points.length) {
    html += '<h2 style="font-size:var(--text-md);">Key points</h2><ol style="font-size:var(--text-base);">';
    c.key_points.forEach(kp => { html += `<li style="margin-bottom:var(--space-2);">${esc(kp)}</li>`; });
    html += '</ol>';
  }

  if (c.decisions_made && c.decisions_made.length) {
    html += '<h2 style="font-size:var(--text-md);">Decisions made</h2><ul style="font-size:var(--text-base);">';
    c.decisions_made.forEach(d => { html += `<li>${esc(d)}</li>`; });
    html += '</ul>';
  }

  if (c.key_facts.length) {
    html += '<h2 style="font-size:var(--text-md);">Key facts</h2><ul style="font-size:var(--text-base);">';
    c.key_facts.forEach(f => { html += `<li>${esc(f)}</li>`; });
    html += '</ul>';
  }

  if (c.action_items.length) {
    html += '<h2 style="font-size:var(--text-md);">Action items</h2>';
    c.action_items.forEach(item => {
      const dueHtml = item.due_date ? ` <span class="due">(due: ${esc(item.due_date)})</span>` : '';
      html += `<div class="todo-row" data-todo-id="${esc(item.id)}">
        <input type="checkbox" ${item.completed ? 'checked' : ''}
          onclick="toggleTodo('${item.id}', ${item.completed})">
        <span class="desc ${item.completed ? 'todo-done' : ''}">${esc(item.description)}${dueHtml}${ownerLabel(item)}</span>
      </div>`;
    });
  }

  if (c.speech_coaching) {
    html += renderFeedbackCardHtml(c.speech_coaching, c.title);
  }

  document.getElementById('content').innerHTML = html;
}

// --- Conversation editing (#41: correct anything the LLM got wrong) ---
// Generic helpers for the repeated "list of rows, each with 1-3 text
// fields, add/remove buttons" pattern — reused for participants,
// key_points, decisions_made, key_facts, and action_items rather than
// writing near-duplicate code five times.

function renderEditRows(rowsData, placeholders) {
  let html = '';
  rowsData.forEach(values => {
    const inputs = values.map((v, i) =>
      `<input type="text" placeholder="${esc(placeholders[i])}" value="${esc(v)}"
        style="flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:6px 8px;margin-right:var(--space-2);">`
    ).join('');
    html += `<div class="edit-list-row" style="display:flex;margin-bottom:var(--space-2);">${inputs}
      <button type="button" onclick="this.parentElement.remove()"
        style="background:#3d1f1f;color:#f85149;border:none;border-radius:6px;padding:0 12px;flex-shrink:0;">×</button></div>`;
  });
  return html;
}

function addEditRow(containerId, placeholders) {
  const container = document.getElementById(containerId);
  const div = document.createElement('div');
  div.className = 'edit-list-row';
  div.style.cssText = 'display:flex;margin-bottom:var(--space-2);';
  placeholders.forEach(ph => {
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = ph;
    input.style.cssText = 'flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:6px 8px;margin-right:8px;';
    div.appendChild(input);
  });
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = '×';
  btn.style.cssText = 'background:#3d1f1f;color:#f85149;border:none;border-radius:6px;padding:0 12px;flex-shrink:0;';
  btn.onclick = () => div.remove();
  div.appendChild(btn);
  container.appendChild(div);
}

function gatherEditRows(containerId) {
  const container = document.getElementById(containerId);
  const rows = [];
  container.querySelectorAll('.edit-list-row').forEach(row => {
    rows.push(Array.from(row.querySelectorAll('input')).map(i => i.value.trim()));
  });
  return rows;
}

function renderConversationEditForm(stem) {
  const c = window._currentConversation;
  setHeader('', false, true);

  const inputStyle = 'width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:8px;font-size:var(--text-base);margin-bottom:var(--space-3);box-sizing:border-box;';
  const labelStyle = 'font-size:var(--text-sm);color:#8b949e;display:block;margin-bottom:var(--space-1);';
  const addBtnStyle = 'background:#21262d;color:#58a6ff;border:1px solid #30363d;border-radius:6px;padding:6px 12px;font-size:var(--text-sm);margin-bottom:var(--space-4);';

  const html = `
    <h1 style="margin-top:var(--space-2);">Edit Conversation</h1>

    <label style="${labelStyle}">Title</label>
    <input type="text" id="edit-title" value="${esc(c.title)}" style="${inputStyle}">

    <label style="${labelStyle}">Overview</label>
    <textarea id="edit-overview" rows="4" style="${inputStyle}">${esc(c.overview)}</textarea>

    <label style="${labelStyle}">Atmosphere</label>
    <input type="text" id="edit-atmosphere" value="${esc(c.atmosphere || '')}" style="${inputStyle}">

    <h2 style="font-size:var(--text-md);">Participants</h2>
    <div id="edit-participants">${renderEditRows((c.participants || []).map(p => [p.name, p.role || '']), ['Name', 'Role'])}</div>
    <button type="button" onclick="addEditRow('edit-participants', ['Name', 'Role'])" style="${addBtnStyle}">+ Add participant</button>

    <h2 style="font-size:var(--text-md);">Key points</h2>
    <div id="edit-key_points">${renderEditRows((c.key_points || []).map(k => [k]), ['Key point'])}</div>
    <button type="button" onclick="addEditRow('edit-key_points', ['Key point'])" style="${addBtnStyle}">+ Add key point</button>

    <h2 style="font-size:var(--text-md);">Decisions made</h2>
    <div id="edit-decisions_made">${renderEditRows((c.decisions_made || []).map(d => [d]), ['Decision'])}</div>
    <button type="button" onclick="addEditRow('edit-decisions_made', ['Decision'])" style="${addBtnStyle}">+ Add decision</button>

    <h2 style="font-size:var(--text-md);">Key facts</h2>
    <div id="edit-key_facts">${renderEditRows((c.key_facts || []).map(f => [f]), ['Fact'])}</div>
    <button type="button" onclick="addEditRow('edit-key_facts', ['Fact'])" style="${addBtnStyle}">+ Add fact</button>

    <h2 style="font-size:var(--text-md);">Action items</h2>
    <div id="edit-action_items">${renderEditRows((c.action_items || []).map(a => [a.description, a.due_date || '', a.owner || '']), ['Description', 'Due date', 'Owner'])}</div>
    <button type="button" onclick="addEditRow('edit-action_items', ['Description', 'Due date', 'Owner'])" style="${addBtnStyle}">+ Add action item</button>

    <div style="display:flex;gap:var(--space-2);margin-top:var(--space-2);">
      <button onclick="saveConversationEdits('${stem}')" style="flex:1;background:#1f6feb;color:#fff;border:none;border-radius:6px;padding:12px;font-size:var(--text-base);">Save</button>
      <button onclick="renderConversationDetail('${stem}')" style="flex:1;background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:12px;font-size:var(--text-base);">Cancel</button>
    </div>
  `;
  document.getElementById('content').innerHTML = html;
}

// Native alert() boxes can't be resized or scrolled — that's entirely
// controlled by the OS/browser, not something CSS/JS can touch. This is a
// real in-page panel instead, so a long error is actually fully readable.
function showErrorPanel(title, fullText) {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:1000;display:flex;align-items:center;justify-content:center;padding:var(--space-4);';
  overlay.innerHTML = `
    <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;max-width:100%;max-height:80vh;
      display:flex;flex-direction:column;padding:var(--space-4);">
      <h2 style="font-size:var(--text-md);color:#f85149;margin-top:0;">${esc(title)}</h2>
      <pre style="overflow-y:auto;white-space:pre-wrap;word-break:break-word;background:#0d1117;border:1px solid #30363d;
        border-radius:6px;padding:var(--space-2);font-size:var(--text-sm);color:#e6edf3;flex:1;margin:0 0 var(--space-3) 0;">${esc(fullText)}</pre>
      <button id="error-panel-dismiss" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:10px;font-size:var(--text-base);">Dismiss</button>
    </div>`;
  document.body.appendChild(overlay);
  document.getElementById('error-panel-dismiss').onclick = () => overlay.remove();
}

async function saveConversationEdits(stem) {
  const payload = {
    title: document.getElementById('edit-title').value.trim(),
    overview: document.getElementById('edit-overview').value.trim(),
    atmosphere: document.getElementById('edit-atmosphere').value.trim() || null,
    participants: gatherEditRows('edit-participants')
      .filter(([name]) => name)
      .map(([name, role]) => ({ name, role: role || null })),
    key_points: gatherEditRows('edit-key_points').map(([v]) => v).filter(v => v),
    decisions_made: gatherEditRows('edit-decisions_made').map(([v]) => v).filter(v => v),
    key_facts: gatherEditRows('edit-key_facts').map(([v]) => v).filter(v => v),
    action_items: gatherEditRows('edit-action_items')
      .filter(([description]) => description)
      .map(([description, due_date, owner]) => ({
        description, due_date: due_date || null, owner: owner || null
      })),
  };

  const saveBtn = document.querySelector('button[onclick^="saveConversationEdits"]');
  if (saveBtn) { saveBtn.textContent = 'Saving...'; saveBtn.disabled = true; }

  try {
    const res = await fetch(`/api/conversations/${encodeURIComponent(stem)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const errorText = await res.text();
      showErrorPanel('Save failed (server said: ' + res.status + ') — your edits are still here, nothing was lost', errorText);
      if (saveBtn) { saveBtn.textContent = 'Save'; saveBtn.disabled = false; }
      return;
    }
  } catch (err) {
    showErrorPanel('Save failed — could not reach the server', `${err.message}

Your edits are still here, nothing was lost.`);
    if (saveBtn) { saveBtn.textContent = 'Save'; saveBtn.disabled = false; }
    return;
  }

  renderConversationDetail(stem);
}

async function deleteConversation(stem) {
  if (!confirm('Remove this conversation from your browsing views? The recording and transcript stay fully preserved on the Thor — this only hides it here, and can be undone.')) {
    return;
  }
  await fetch(`/api/conversations/${encodeURIComponent(stem)}/archive`, { method: 'POST' });
  goTab('conversations');
}

async function renderTodos() {
  setHeader('To-Dos', true, false);
  document.getElementById('content').innerHTML = 'Loading...';
  const items = await (await fetch('/api/todos')).json();

  let html = `<div style="margin-bottom:var(--space-3);">
    <button onclick="goDetail('todos', 'completed')"
      style="background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:var(--space-2) var(--space-3);font-size:var(--text-sm);">
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
    html += `<div class="todo-row" data-todo-id="${esc(item.id)}" onclick="goDetail('conversations','${esc(item.source_stem)}')">
      <input type="checkbox" ${item.completed ? 'checked' : ''}
        onclick="event.stopPropagation(); toggleTodo('${item.id}', ${item.completed})">
      <span class="desc ${item.completed ? 'todo-done' : ''}">${esc(item.description)}${dueHtml}${ownerLabel(item)}
      <span class="source-label"> — ${esc(item.source_title)} (${esc(item.date)})</span></span>
    </div>`;
  });
  document.getElementById('content').innerHTML = html;
}

async function renderCompletedTodos() {
  setHeader('', false, true);
  document.getElementById('content').innerHTML = 'Loading...';
  const items = await (await fetch('/api/todos/completed')).json();

  let html = '<h1 style="margin-top:var(--space-2);">Completed Today</h1>';

  if (!items.length) {
    html += '<p class="empty">Nothing checked off yet today.</p>';
    document.getElementById('content').innerHTML = html;
    return;
  }

  html += '<p style="color:#8b949e;font-size:var(--text-sm);">Tap a checkbox to undo — this list clears itself at midnight, but nothing is ever actually deleted (it still shows in its original conversation).</p>';

  items.forEach(item => {
    const dueHtml = item.due_date ? ` <span class="due">(due: ${esc(item.due_date)})</span>` : '';
    html += `<div class="todo-row" data-todo-id="${esc(item.id)}">
      <input type="checkbox" checked onclick="toggleTodo('${item.id}', true)">
      <span class="desc todo-done">${esc(item.description)}${dueHtml}${ownerLabel(item)}
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
      <div style="font-size:var(--text-sm);color:#8b949e;margin:var(--space-1) 0;">${sc.words_per_minute} WPM</div>
      <p style="font-size:var(--text-sm);color:#8b949e;margin:0;">${esc(sc.overall_take_preview)}</p>
    </div>`;
  });
  document.getElementById('content').innerHTML = html;
}

function renderFeedbackCardHtml(sc, title) {
  const pace = sc.metrics.pace;
  const fillers = sc.metrics.fillers;
  const fillerNote = fillers.total_filler_count ? `, ${fillers.total_filler_count} filler words` : '';
  const fb = sc.feedback;

  let html = `<h2 style="font-size:var(--text-md);margin-top:var(--space-6);">Speaking Style Feedback</h2>
    <div class="card">
      <div style="font-size:var(--text-sm);color:#8b949e;margin-bottom:var(--space-2);">
        ${pace.words_per_minute} WPM, ${pace.duration_seconds}s${fillerNote}
      </div>`;

  if (fb.strengths && fb.strengths.length) {
    html += '<div style="font-size:var(--text-sm);"><strong>Strengths:</strong></div><ul style="font-size:var(--text-sm);margin:var(--space-1) 0 10px;padding-left:var(--space-5);">';
    fb.strengths.forEach(s => { html += `<li>${esc(s)}</li>`; });
    html += '</ul>';
  }

  if (fb.areas_to_improve && fb.areas_to_improve.length) {
    html += '<div style="font-size:var(--text-sm);"><strong>Areas to improve:</strong></div>';
    fb.areas_to_improve.forEach(area => {
      html += `<div style="font-size:var(--text-sm);margin:var(--space-2) 0 10px;">
        ${esc(area.observation)}<br>
        <span style="color:#8b949e;">Example: "${esc(area.example)}"</span><br>
        <span style="color:#3fb950;">Try instead: ${esc(area.suggestion)}</span>
      </div>`;
    });
  }

  if (fb.pace_feedback) html += `<p style="font-size:var(--text-sm);"><strong>Pace:</strong> ${esc(fb.pace_feedback)}</p>`;
  if (fb.overall_take) html += `<p style="font-size:var(--text-sm);"><strong>Overall:</strong> ${esc(fb.overall_take)}</p>`;

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
    `<h1 style="margin-top:var(--space-2);">${esc(sc.title)}</h1>` + renderFeedbackCardHtml(sc, sc.title);
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

  let html = `<h1 style="margin-top:var(--space-2);">${esc(data.list_name)}</h1>`;
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

// --- Settings (#37: voice enrollment/recognition, more settings likely
// to live here over time as the project grows) ---

async function renderSettings() {
  setHeader('', false, true);
  document.getElementById('content').innerHTML = 'Loading...';
  const speakers = await (await fetch('/api/speakers')).json();
  const digestSettings = await (await fetch('/api/settings/digest')).json();

  let html = '<h1 style="margin-top:var(--space-2);">Settings</h1>';
  html += '<h2 style="font-size:var(--text-md);">Voice Recognition</h2>';
  html += `<p style="font-size:var(--text-sm);color:#8b949e;">Enroll a voice once, and future conversations will show that
    person's real name instead of an anonymous speaker label. Mark yourself as the
    main user so speaking-style coaching analyzes only your own speech, not whoever
    else is in the room.</p>`;

  if (speakers.length) {
    speakers.forEach(s => {
      html += `<div class="list-row" style="display:flex;justify-content:space-between;align-items:center;">
        <div>
          <span style="font-weight:600;">${esc(s.name)}</span>
          ${s.is_main_user ? '<span style="color:#58a6ff;font-size:var(--text-sm);"> (main user)</span>' : ''}
        </div>
        <div>
          ${s.is_main_user ? '' : `<button onclick="setMainSpeaker('${esc(s.name)}')"
              style="background:#21262d;color:#58a6ff;border:1px solid #30363d;border-radius:6px;padding:4px 10px;font-size:var(--text-sm);margin-right:var(--space-1);">Set as main</button>`}
          <button onclick="deleteSpeaker('${esc(s.name)}')"
            style="background:#21262d;color:#f85149;border:1px solid #30363d;border-radius:6px;padding:4px 10px;font-size:var(--text-sm);">Delete</button>
        </div>
      </div>`;
    });
  } else {
    html += '<p class="empty">No voices enrolled yet.</p>';
  }

  html += `
    <h2 style="font-size:var(--text-md);margin-top:var(--space-5);">Enroll a New Voice</h2>
    <p style="font-size:var(--text-sm);color:#8b949e;">Upload a short (10-30 second), clean recording of just this
      person talking — background noise or other voices in the sample will make matching less reliable.</p>
    <label style="font-size:var(--text-sm);color:#8b949e;display:block;margin-bottom:var(--space-1);">Name</label>
    <input type="text" id="enroll-name" placeholder="e.g. Eric"
      style="width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:8px;font-size:var(--text-base);margin-bottom:var(--space-3);box-sizing:border-box;">
    <label style="display:flex;align-items:center;gap:var(--space-2);margin-bottom:var(--space-3);font-size:var(--text-base);">
      <input type="checkbox" id="enroll-is-main-user" style="width:auto;">
      This is me (main user for speaking-style coaching)
    </label>
    <input type="file" id="enroll-audio" accept="audio/*"
      style="width:100%;margin-bottom:var(--space-3);color:#e6edf3;">
    <button onclick="submitEnrollment()"
      style="width:100%;background:#1f6feb;color:#fff;border:none;border-radius:6px;padding:12px;font-size:var(--text-base);">Enroll</button>
    <div id="enroll-status" style="margin-top:var(--space-3);font-size:var(--text-sm);color:#8b949e;"></div>

    <h2 style="font-size:var(--text-md);margin-top:var(--space-5);">Daily Digest Email</h2>
    <p style="font-size:var(--text-sm);color:#8b949e;">Get an email summarizing each day's conversations and to-dos.</p>
    <label style="display:flex;align-items:center;gap:var(--space-2);margin-bottom:var(--space-3);font-size:var(--text-base);">
      <input type="checkbox" id="digest-enabled" ${digestSettings.enabled ? 'checked' : ''} style="width:auto;">
      Send me a daily digest email
    </label>
    <label style="font-size:var(--text-sm);color:#8b949e;display:block;margin-bottom:var(--space-1);">Email address</label>
    <input type="email" id="digest-email" placeholder="you@example.com" value="${esc(digestSettings.email || '')}"
      style="width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:8px;font-size:var(--text-base);margin-bottom:var(--space-3);box-sizing:border-box;">
    <button onclick="submitDigestSettings()"
      style="width:100%;background:#1f6feb;color:#fff;border:none;border-radius:6px;padding:12px;font-size:var(--text-base);">Save</button>
    <div id="digest-status" style="margin-top:var(--space-3);font-size:var(--text-sm);color:#8b949e;"></div>
  `;
  document.getElementById('content').innerHTML = html;
}

async function submitDigestSettings() {
  const enabled = document.getElementById('digest-enabled').checked;
  const email = document.getElementById('digest-email').value.trim();
  const statusEl = document.getElementById('digest-status');

  if (enabled && !email) {
    statusEl.textContent = 'Please enter an email address, or uncheck the box to opt out.';
    return;
  }

  statusEl.textContent = 'Saving...';
  try {
    const res = await fetch('/api/settings/digest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled, email })
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showErrorPanel('Could not save digest settings', body.error || `Server returned ${res.status}`);
      statusEl.textContent = '';
      return;
    }
    statusEl.textContent = enabled ? 'Saved — daily digest emails are on.' : 'Saved — digest emails are off.';
  } catch (err) {
    showErrorPanel('Could not save digest settings — could not reach the server', err.message);
    statusEl.textContent = '';
  }
}

async function submitEnrollment() {
  const name = document.getElementById('enroll-name').value.trim();
  const isMainUser = document.getElementById('enroll-is-main-user').checked;
  const audioInput = document.getElementById('enroll-audio');
  const statusEl = document.getElementById('enroll-status');

  if (!name) { statusEl.textContent = 'Please enter a name.'; return; }
  if (!audioInput.files.length) { statusEl.textContent = 'Please choose an audio file.'; return; }

  const formData = new FormData();
  formData.append('name', name);
  formData.append('is_main_user', isMainUser ? 'true' : 'false');
  formData.append('audio', audioInput.files[0]);

  statusEl.textContent = 'Uploading...';
  try {
    const res = await fetch('/api/speakers/enroll', { method: 'POST', body: formData });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showErrorPanel('Enrollment failed', body.error || `Server returned ${res.status}`);
      statusEl.textContent = '';
      return;
    }
  } catch (err) {
    showErrorPanel('Enrollment failed — could not reach the server', err.message);
    statusEl.textContent = '';
    return;
  }

  statusEl.textContent = 'Processing — extracting voice fingerprint, this can take a moment...';
  pollEnrollmentStatus(name, statusEl);
}

async function pollEnrollmentStatus(name, statusEl, failedOnce) {
  const res = await fetch(`/api/speakers/enroll-status/${encodeURIComponent(name)}`);
  const data = await res.json();

  if (data.status === 'done') {
    statusEl.textContent = `${name} enrolled successfully.`;
    setTimeout(() => renderSettings(), 1000);
  } else if (data.status === 'failed') {
    if (!failedOnce) {
      // Don't trust a single "failed" reading — there's a narrow window
      // where the enrollment file was just cleaned up but the profile
      // write isn't visible on this exact poll yet. Recheck once more
      // after a short delay before actually declaring failure.
      setTimeout(() => pollEnrollmentStatus(name, statusEl, true), 1500);
      return;
    }
    showErrorPanel(
      'Enrollment did not complete',
      `Could not extract a voice fingerprint for ${name}. This can happen if diarization is disabled, ` +
      'the Hugging Face token is missing, or the installed pyannote/whisperx version does not support ' +
      'the embeddings feature this relies on — check the pipeline logs (journalctl / docker compose logs) for specifics.'
    );
    statusEl.textContent = '';
  } else {
    setTimeout(() => pollEnrollmentStatus(name, statusEl), 2000);
  }
}

async function setMainSpeaker(name) {
  await fetch(`/api/speakers/${encodeURIComponent(name)}/set-main`, { method: 'POST' });
  renderSettings();
}

async function deleteSpeaker(name) {
  if (!confirm(`Remove the enrolled voice profile for ${name}? Their past conversations won't change, but future recordings will no longer recognize this voice.`)) {
    return;
  }
  await fetch(`/api/speakers/${encodeURIComponent(name)}`, { method: 'DELETE' });
  renderSettings();
}

async function toggleTodo(id, currentlyDone) {
  const isCheckingOn = !currentlyDone;
  const row = document.querySelector(`[data-todo-id="${CSS.escape(id)}"]`);

  if (isCheckingOn && row) {
    // Instant visual confirmation the tap registered, before the API call
    // even resolves — the item then sits struck-through for a moment
    // (it now genuinely lives in Completed) rather than vanishing the
    // instant you tap it.
    const desc = row.querySelector('.desc');
    if (desc) desc.classList.add('todo-done');
    const checkbox = row.querySelector('input[type="checkbox"]');
    if (checkbox) checkbox.checked = true;
  }

  await fetch(`/api/todo/${encodeURIComponent(id)}/toggle`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({current_default: currentlyDone})
  });

  if (isCheckingOn) {
    setTimeout(() => render(currentState()), 1500);
  } else {
    // Unchecking (e.g. from the Completed view) still re-renders
    // immediately — no reason to delay something reappearing.
    render(currentState());
  }
}

// Initial load: parse the URL hash if present (e.g. a bookmark or restored
// session), rather than always defaulting to Today regardless of the URL.
function parseInitialState() {
  const hash = window.location.hash.replace(/^#/, '');
  if (!hash) return { tab: 'today', detail: null };
  const [tab, rawDetail] = hash.split('/');
  const validTabs = ['today', 'conversations', 'todos', 'feedback', 'lists', 'settings'];
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
