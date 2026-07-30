"""
Tracks which action items have been manually checked off via the webapp.
Kept entirely separate from the *.analysis.json files themselves — those
are the LLM's read-only output and shouldn't be mutated; this is a small,
independent overlay of "done" state, keyed by a stable item ID.

Item IDs are "{conversation_stem}:{index}" — stable as long as a given
conversation's action_items list doesn't change order, which it never does
once written.
"""
import json
import threading
from pathlib import Path

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


def is_completed(item_id: str, default: bool = False) -> bool:
    state = _load()
    return state.get(item_id, default)


def toggle(item_id: str, current_default: bool = False) -> bool:
    """Flips the stored state for item_id and returns the new value."""
    with _lock:
        state = _load()
        new_value = not state.get(item_id, current_default)
        state[item_id] = new_value
        _save(state)
        return new_value
