"""
Tracks which anonymous speakers a person has explicitly chosen NOT to
label, per conversation — e.g. a one-off caller or vendor rep who isn't
worth naming or remembering. Without this, the "Who said this?" prompt
would keep reappearing on that same conversation indefinitely, since
there'd be no way to distinguish "hasn't been asked yet" from "was asked
and deliberately declined."

Same small-overlay pattern as conversation_state.py/todo_state.py, just
keyed by (stem, speaker_id) pairs rather than a flat stem set — one
conversation can have multiple distinct anonymous speakers, and skipping
one shouldn't affect whether others in that same conversation still get
prompted.
"""
import json
import threading
from pathlib import Path
from typing import Dict, List

import config

_lock = threading.Lock()


def _state_path() -> Path:
    return config.BASE_DIR / "skipped_speakers.json"


def _load() -> Dict[str, List[str]]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: Dict[str, List[str]]) -> None:
    _state_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_skipped(stem: str, speaker_id: str) -> bool:
    return speaker_id in _load().get(stem, [])


def skip(stem: str, speaker_id: str) -> None:
    with _lock:
        data = _load()
        skipped_for_stem = data.setdefault(stem, [])
        if speaker_id not in skipped_for_stem:
            skipped_for_stem.append(speaker_id)
        _save(data)
