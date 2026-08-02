"""
Tracks which conversations have been "deleted" (archived) from the
webapp's browsing views. Deliberately a soft-delete: the underlying
transcript and analysis files on disk are NEVER touched or removed by
this — only hidden from active browsing. Full historical data always
stays completely intact on the Thor, exactly as requested.

Same small-overlay pattern as todo_state.py, just tracking a different
kind of per-conversation state.
"""
import json
import threading
from pathlib import Path
from typing import Set

import config

_lock = threading.Lock()


def _state_path() -> Path:
    return config.BASE_DIR / "archived_conversations.json"


def _load() -> Set[str]:
    path = _state_path()
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def _save(archived: Set[str]) -> None:
    path = _state_path()
    path.write_text(json.dumps(sorted(archived), indent=2), encoding="utf-8")


def is_archived(stem: str) -> bool:
    return stem in _load()


def archive(stem: str) -> None:
    with _lock:
        archived = _load()
        archived.add(stem)
        _save(archived)


def unarchive(stem: str) -> None:
    with _lock:
        archived = _load()
        archived.discard(stem)
        _save(archived)


def get_all_archived() -> Set[str]:
    return _load()
