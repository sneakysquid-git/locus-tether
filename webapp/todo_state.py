"""
Tracks which action items (and mentioned-list items) have been manually
checked off via the webapp. Kept entirely separate from the *.analysis.json
files themselves — those are the LLM's read-only output and shouldn't be
mutated; this is a small, independent overlay of "done" state, keyed by a
stable item ID.

Item IDs are "{conversation_stem}:{index}" for action items, or
"{conversation_stem}:mlist:{list_index}:{item_index}" for mentioned-list
items — stable as long as a given conversation's own lists don't change
order, which they never do once written.

Storage format: {item_id: "YYYY-MM-DD"} — presence of a key means the item
is completed, and the value records WHICH DAY it was completed on. This
powers the To-Dos tab's "Completed" view: items completed today show up
there; items completed on an earlier day quietly stop appearing there
(without ever being deleted from this file) once the day rolls over. The
item still shows up crossed-out in its own conversation's detail view
regardless of when it was completed — that's a separate, permanent
historical record, unaffected by this day-scoping.

Backward compatible with the OLDER format ({item_id: true/false}, no date)
from before this schema existed — a bare `true` is treated as "completed,
but on an unknown day" (so it just never shows in "completed today", which
is the correct fallback for something that predates day-tracking).
"""
import json
import threading
from datetime import date
from pathlib import Path
from typing import Optional

import config

_lock = threading.Lock()  # guards read-modify-write against concurrent requests


def _state_path() -> Path:
    return config.BASE_DIR / "todo_state.json"


def _load() -> dict:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}  # corrupt/missing state file shouldn't crash the app — just start fresh


def _save(state: dict) -> None:
    path = _state_path()
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _resolve(value, default: bool) -> bool:
    """Given a raw stored value (or None if the key is absent), returns
    whether it represents a completed item — handling both the current
    date-string format and the older bare-boolean format."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return True  # any string value present is a completion date -> completed


def is_completed(item_id: str, default: bool = False) -> bool:
    state = _load()
    return _resolve(state.get(item_id), default)


def get_completed_date(item_id: str) -> Optional[str]:
    """ISO date this item was completed on, or None if it's not completed,
    or if it was completed under the old boolean-only format (predates day-
    tracking — treated as 'completed on an unknown day', so it won't show
    in a day-scoped 'completed today' view, but is still correctly `True`
    via is_completed())."""
    state = _load()
    value = state.get(item_id)
    return value if isinstance(value, str) else None


def toggle(item_id: str, current_default: bool = False) -> bool:
    """Flips the stored state for item_id and returns the new value."""
    with _lock:
        state = _load()
        currently_completed = _resolve(state.get(item_id), current_default)
        if currently_completed:
            state.pop(item_id, None)  # toggling off — remove entirely
            new_value = False
        else:
            state[item_id] = date.today().isoformat()  # toggling on — record today
            new_value = True
        _save(state)
        return new_value
