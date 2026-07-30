"""
Single source of truth for reading *.analysis.json and *.speech_coach.json
files off disk. digest.py uses the date-filtered wrappers here (just one
day, for the email digest); webapp.py uses the "load_all_*" functions
directly (full history, for the Conversations/To-Dos/Feedback tabs).

Kept separate from both digest.py and webapp.py specifically so this
file-reading logic exists in exactly one place — before this refactor,
webapp.py and digest.py each had to reimplement more or less the same
thing, which is exactly the kind of duplication that quietly drifts apart
over time.
"""
import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import config


def load_all_analyses() -> list[dict]:
    """
    Every *.analysis.json in TRANSCRIPTS_DIR, most recent first. Each dict
    includes '_stem' (filename stem, for cross-referencing) and '_date'
    (the file's modification date, as an ISO string — used for display and
    for date-filtering by callers like load_day_analyses below).
    """
    results = []
    for path in sorted(config.TRANSCRIPTS_DIR.glob("*.analysis.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # skip anything unreadable rather than crash the caller
        data["_stem"] = path.stem.removesuffix(".analysis")
        data["_date"] = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
        results.append(data)
    results.sort(key=lambda d: d["_date"], reverse=True)
    return results


def load_analysis_by_stem(stem: str) -> Optional[dict]:
    """Single conversation's analysis, or None if it doesn't exist."""
    path = config.TRANSCRIPTS_DIR / f"{stem}.analysis.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    data["_stem"] = stem
    data["_date"] = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    return data


def load_all_speech_coaching() -> list[dict]:
    """
    Every *.speech_coach.json in TRANSCRIPTS_DIR, most recent first (by the
    same "matching conversation's date" logic as load_day_speech_coaching —
    see its docstring for why: coaching should stay attached to the
    conversation it's about, not whenever the script happened to run).
    """
    results = []
    for path in sorted(config.TRANSCRIPTS_DIR.glob("*.speech_coach.json")):
        stem = path.stem.removesuffix(".speech_coach")
        analysis_path = config.TRANSCRIPTS_DIR / f"{stem}.analysis.json"
        if analysis_path.exists():
            relevant_date = datetime.fromtimestamp(analysis_path.stat().st_mtime).date()
        else:
            relevant_date = datetime.fromtimestamp(path.stat().st_mtime).date()

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        data["_stem"] = stem
        data["_date"] = relevant_date.isoformat()
        results.append(data)
    results.sort(key=lambda d: d["_date"], reverse=True)
    return results


def load_speech_coaching_by_stem(stem: str) -> Optional[dict]:
    """Single conversation's speech coaching, or None if none was ever run."""
    path = config.TRANSCRIPTS_DIR / f"{stem}.speech_coach.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    data["_stem"] = stem
    return data


def load_day_analyses(target_date: date) -> list[dict]:
    """Just one day's worth — used by digest.py for the email digest."""
    target_str = target_date.isoformat()
    return [a for a in load_all_analyses() if a["_date"] == target_str]


def load_day_speech_coaching(target_date: date) -> list[dict]:
    """Just one day's worth — used by digest.py for the email digest."""
    target_str = target_date.isoformat()
    return [sc for sc in load_all_speech_coaching() if sc["_date"] == target_str]
