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


def save_analysis(stem: str, data: dict) -> None:
    """
    Overwrites a conversation's analysis.json with user-edited content —
    unlike todo_state.json's separate-overlay design, a full content edit
    is meant to become the new source of truth going forward, not sit
    alongside the original LLM output. Strips internal _stem/_date keys
    (those are computed at load time from the file itself, never meant to
    be persisted back into it).
    """
    path = config.TRANSCRIPTS_DIR / f"{stem}.analysis.json"
    to_write = {k: v for k, v in data.items() if not k.startswith("_")}
    path.write_text(json.dumps(to_write, indent=2, ensure_ascii=False), encoding="utf-8")


def get_speaker_count(stem: str) -> Optional[int]:
    """
    Number of distinct speakers diarization detected for this conversation's
    raw transcript, or None if the transcript doesn't exist, has no
    segments, or diarization never ran/failed (no segment has a real
    speaker label) — deliberately distinct from 0, since "diarization
    wasn't run" and "diarization ran and found 1 speaker" are different
    things worth being able to tell apart later if needed.
    """
    path = config.TRANSCRIPTS_DIR / f"{stem}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    speakers = {seg.get("speaker") for seg in data.get("segments", []) if seg.get("speaker")}
    return len(speakers) if speakers else None


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


def aggregate_lists() -> list[dict]:
    """
    Groups mentioned_lists items across ALL analyses by list name (matched
    case/whitespace-insensitively — the LLM is also given existing list
    names as context to encourage reusing them consistently, but this
    normalization is the safety net regardless of how well it listens).

    Returns [{"list_name": <display name, first-seen casing>, "items": [...]}],
    where each item is {"id", "text", "source_stem", "source_title", "date"}.

    Does NOT filter by completed state — same separation as action items:
    that's a display-layer concern (see webapp.py), this module only reads
    raw file content.
    """
    groups: dict[str, dict] = {}  # normalized name -> {"display_name", "items"}

    def _looks_better_capitalized(candidate: str, current: str) -> bool:
        # Simple heuristic: prefer the variant with more uppercase letters,
        # since that's usually the properly title-cased one (e.g. prefer
        # "Restaurants to Try" over "restaurants to try"). Not perfect, but
        # avoids the display name being arbitrarily whichever variant
        # happened to be processed first.
        return sum(c.isupper() for c in candidate) > sum(c.isupper() for c in current)

    for a in load_all_analyses():
        stem = a.get("_stem", "")
        title = a.get("title", stem)
        date_str = a.get("_date", "")
        for list_idx, mlist in enumerate(a.get("mentioned_lists", [])):
            name = mlist.get("list_name", "Misc").strip()
            normalized = name.lower()
            if normalized not in groups:
                groups[normalized] = {"display_name": name, "items": []}
            elif _looks_better_capitalized(name, groups[normalized]["display_name"]):
                groups[normalized]["display_name"] = name
            for item_idx, item_text in enumerate(mlist.get("items", [])):
                groups[normalized]["items"].append(
                    {
                        "id": f"{stem}:mlist:{list_idx}:{item_idx}",
                        "text": item_text,
                        "source_stem": stem,
                        "source_title": title,
                        "date": date_str,
                    }
                )

    return [{"list_name": g["display_name"], "items": g["items"]} for g in groups.values()]


def get_all_list_names() -> list[str]:
    """Distinct list names seen so far, for prompt-time consistency context."""
    return sorted({g["list_name"] for g in aggregate_lists()})


def load_day_analyses(target_date: date) -> list[dict]:
    """Just one day's worth — used by digest.py for the email digest."""
    target_str = target_date.isoformat()
    return [a for a in load_all_analyses() if a["_date"] == target_str]


def load_day_speech_coaching(target_date: date) -> list[dict]:
    """Just one day's worth — used by digest.py for the email digest."""
    target_str = target_date.isoformat()
    return [sc for sc in load_all_speech_coaching() if sc["_date"] == target_str]
