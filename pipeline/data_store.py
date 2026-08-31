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
from typing import Optional

import config


def load_all_analyses() -> list[dict]:
    """
    Every *.analysis.json in TRANSCRIPTS_DIR, most recent first — genuinely
    chronological, not just alphabetical-by-filename. Each dict includes:
      - '_stem': filename stem, for cross-referencing
      - '_date': the file's modification date, ISO string (date only — used
        for display and for date-filtering by callers like load_day_analyses)
      - '_time': the file's modification time, formatted for display
        (e.g. "2:47 PM")
      - '_timestamp': full-precision mtime (float, unix epoch) — the ACTUAL
        sort key. Sorting by '_date' alone was a real bug: multiple same-day
        conversations (the common case during any real testing session)
        would tie on date, and Python's stable sort would then fall back to
        whatever order glob() happened to return — alphabetical by
        filename, not chronological — which is exactly what surfaced this.
    """
    results = []
    for path in config.TRANSCRIPTS_DIR.glob("*.analysis.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # skip anything unreadable rather than crash the caller
        mtime = path.stat().st_mtime
        dt = datetime.fromtimestamp(mtime)
        data["_stem"] = path.stem.removesuffix(".analysis")
        data["_date"] = dt.date().isoformat()
        data["_time"] = dt.strftime("%-I:%M %p")
        data["_timestamp"] = mtime
        results.append(data)
    results.sort(key=lambda d: d["_timestamp"], reverse=True)
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
    mtime = path.stat().st_mtime
    dt = datetime.fromtimestamp(mtime)
    data["_stem"] = stem
    data["_date"] = dt.date().isoformat()
    data["_time"] = dt.strftime("%-I:%M %p")
    data["_timestamp"] = mtime
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


def get_recording_duration(stem: str) -> Optional[float]:
    """Seconds, from the raw transcript's own duration field — used to
    compute an approximate start time (end time minus duration) for
    display, since we don't separately record the true wall-clock start
    of a recording anywhere."""
    path = config.TRANSCRIPTS_DIR / f"{stem}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("duration")
    except (json.JSONDecodeError, OSError):
        return None


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


def get_unnamed_speakers(stem: str) -> list[dict]:
    """
    Every anonymous SPEAKER_NN label still present in this conversation's
    transcript, each with a representative snippet — enough for a person
    to actually recognize who's who and assign a real name, rather than
    the app just saying "unidentified" indefinitely. Already-named
    speakers (a real name, not a SPEAKER_NN code) are deliberately
    excluded — there's nothing to label there.

    The snippet is that speaker's single LONGEST segment, not their
    first — a short "yeah" or "mm-hm" early on identifies no one, while
    a longer stretch of actual talking almost always does.
    """
    path = config.TRANSCRIPTS_DIR / f"{stem}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    longest_by_speaker: dict[str, dict] = {}
    for seg in data.get("segments", []):
        speaker = seg.get("speaker")
        if not speaker or not speaker.startswith("SPEAKER_"):
            continue
        text = seg.get("text", "").strip()
        current = longest_by_speaker.get(speaker)
        if current is None or len(text) > len(current["text"]):
            longest_by_speaker[speaker] = {"text": text}

    return [
        {"speaker_id": speaker_id, "snippet": info["text"][:200]}
        for speaker_id, info in sorted(longest_by_speaker.items())
    ]


def load_all_speech_coaching() -> list[dict]:
    """
    Every *.speech_coach.json in TRANSCRIPTS_DIR, most recent first (by the
    same "matching conversation's date" logic as load_day_speech_coaching —
    see its docstring for why: coaching should stay attached to the
    conversation it's about, not whenever the script happened to run).
    """
    results = []
    for path in config.TRANSCRIPTS_DIR.glob("*.speech_coach.json"):
        stem = path.stem.removesuffix(".speech_coach")
        analysis_path = config.TRANSCRIPTS_DIR / f"{stem}.analysis.json"
        if analysis_path.exists():
            relevant_mtime = analysis_path.stat().st_mtime
        else:
            relevant_mtime = path.stat().st_mtime
        relevant_date = datetime.fromtimestamp(relevant_mtime).date()

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        data["_stem"] = stem
        data["_date"] = relevant_date.isoformat()
        data["_timestamp"] = relevant_mtime
        results.append(data)
    results.sort(key=lambda d: d["_timestamp"], reverse=True)
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
        timestamp = a.get("_timestamp", 0)
        for list_idx, mlist in enumerate(a.get("mentioned_lists", [])):
            # `.get(key, default)` only falls back to the default when the
            # key is entirely ABSENT - if a past analysis has this field
            # explicitly set to null (not missing, just empty), .get()
            # still returns None here, and .strip()/enumerate() on None
            # crashes. `or` catches both cases (missing key AND explicit
            # null), since None is falsy either way.
            name = (mlist.get("list_name") or "Misc").strip()
            normalized = name.lower()
            if normalized not in groups:
                groups[normalized] = {"display_name": name, "items": []}
            elif _looks_better_capitalized(name, groups[normalized]["display_name"]):
                groups[normalized]["display_name"] = name
            for item_idx, item_text in enumerate(mlist.get("items") or []):
                groups[normalized]["items"].append(
                    {
                        "id": f"{stem}:mlist:{list_idx}:{item_idx}",
                        "text": item_text,
                        "source_stem": stem,
                        "source_title": title,
                        "date": date_str,
                        "timestamp": timestamp,
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


def get_open_action_item_descriptions_for_date(target_date: date, exclude_stem: str | None = None) -> list[str]:
    """
    #61: plain-text descriptions of action items from conversations dated
    target_date (matching load_day_analyses' own date-matching), used as
    same-day duplicate-matching context fed to the analyzer prompt. Lets
    the model flag (never silently drop) a new action item that looks like
    it describes the same real task as one already logged earlier today.

    Deliberately same-day only, not full history — duplicates across
    multiple separate recordings on the SAME day (e.g. a task mentioned
    once in the morning and again that evening) are the case that's
    actually caused real pain; matching against a task from three weeks
    ago risks far more false positives (an old, since-resolved task vs. a
    genuinely new recurrence of a similar one) for comparatively little
    benefit. Widening this scope is a reasonable future iteration once
    this same-day version has real usage behind it.

    Also deliberately does NOT cross-reference todo_state.json's
    completion overlay — that state lives in webapp/, which pipeline/
    never imports from (see the README's directory-layout note, and
    todo_state.json's own already-completed items would need that same-
    process boundary crossed to check). Accepted trade-off: a same-day
    item that's already been checked off by the time a later recording
    repeats it still gets included here, which just means an occasional
    duplicate flag against an already-resolved item — a visible, harmless
    false flag, not a silently dropped new task.
    """
    exclude_stem = exclude_stem or ""
    descriptions = []
    for a in load_day_analyses(target_date):
        if a.get("_stem") == exclude_stem:
            continue
        for item in a.get("action_items", []):
            desc = item.get("description")
            if desc:
                descriptions.append(desc)
    return descriptions
