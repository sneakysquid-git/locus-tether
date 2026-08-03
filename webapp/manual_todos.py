"""
Manually-added to-do items (#41 follow-up: "use the webapp as the one-stop
shop for to-dos") — dedicated storage, deliberately separate from
conversation-derived action items rather than forcing every to-do to
pretend it came from a real recording.

Reuses todo_state.py for completion tracking (same mechanism conversation-
derived items already use) — a manual item's ID just needs to be distinct
enough to never collide with a real "{stem}:{index}" ID, which the
"manual:" prefix guarantees regardless of what any real conversation
happens to be named.
"""
import json
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

import config


def _items_path() -> Path:
    return config.BASE_DIR / "manual_todos.json"


def _load() -> list:
    path = _items_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(items: list) -> None:
    _items_path().write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def list_items() -> list:
    return _load()


def add_item(description: str, due_date: Optional[str] = None, owner: Optional[str] = None) -> dict:
    items = _load()
    item = {
        "id": f"manual:{uuid.uuid4().hex[:12]}",
        "description": description,
        "due_date": due_date,
        "owner": owner,
        # Needed so the existing "today only" filter toggle (built for
        # conversation-derived items) also works correctly for manual
        # ones — without this, a manual item added today would have no
        # date to match against and would incorrectly vanish under that
        # filter the moment it's created.
        "created_date": date.today().isoformat(),
    }
    items.append(item)
    _save(items)
    return item


def delete_item(item_id: str) -> bool:
    items = _load()
    remaining = [i for i in items if i["id"] != item_id]
    if len(remaining) == len(items):
        return False
    _save(remaining)
    return True
