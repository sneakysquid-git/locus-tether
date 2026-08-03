"""
User-configurable UI preferences (#49) — currently just light/dark theme,
designed to hold future additions (text size, etc.) in the same small
file rather than needing a new module per preference.

Purely a webapp.py concern — digest.py/watcher.py never need this, unlike
digest_preferences.py which both webapp.py and digest.py read.
"""
import json
from pathlib import Path
from typing import Any, Dict

import config

DEFAULTS: Dict[str, Any] = {"theme": "dark", "text_size": "medium"}


def _preferences_path() -> Path:
    return config.BASE_DIR / "ui_preferences.json"


def get_preferences() -> Dict[str, Any]:
    path = _preferences_path()
    if not path.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def set_preferences(**updates: Any) -> Dict[str, Any]:
    """Merges updates into whatever's already saved, so setting one
    preference (e.g. theme) never clobbers others (e.g. a future text
    size setting) that weren't part of this particular update."""
    current = get_preferences()
    current.update(updates)
    _preferences_path().write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current
