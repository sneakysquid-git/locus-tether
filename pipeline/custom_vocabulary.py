"""
Custom vocabulary hints (fixes recurring mis-transcriptions like "Manu" ->
"menu", "ARR" -> "error", "Gainsight" -> "gain site") — real names, tool
names, and acronyms that come up repeatedly in someone's actual
conversations, but that Whisper has no way to know are meaningful vocabulary
rather than a random word it should transcribe phonetically.

Fed to faster-whisper as an `initial_prompt` at transcription time (see
transcribe.py) — this biases the decoder toward these spellings when the
audio is ambiguous, without needing any model fine-tuning.
"""
import json
from pathlib import Path
from typing import List

import config


def _vocabulary_path() -> Path:
    # TRANSCRIPTS_DIR, not bare BASE_DIR — this needs to be read inside
    # the container (transcribe.py) and written on the host (webapp.py's
    # Settings UI). BASE_DIR resolves differently on each side (~/omi-data
    # on the host vs /app in the container, which isn't bind-mounted as a
    # whole) — this is the exact same bug already found and fixed once
    # for speaker_profiles.json (#37). TRANSCRIPTS_DIR is correctly shared
    # on both sides.
    return config.TRANSCRIPTS_DIR / "custom_vocabulary.json"


def get_vocabulary() -> List[str]:
    path = _vocabulary_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def set_vocabulary(terms: List[str]) -> None:
    # Dedup + strip, preserve order — a user re-adding the same term
    # shouldn't silently duplicate it.
    seen = set()
    cleaned = []
    for t in terms:
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            cleaned.append(t)
    _vocabulary_path().write_text(json.dumps(cleaned, indent=2, ensure_ascii=False), encoding="utf-8")


def build_initial_prompt() -> str:
    """
    faster-whisper's initial_prompt is plain text the model conditions on
    before transcribing — not a strict word list API, just context. A
    natural sentence containing the terms works better than a bare
    comma-separated dump, since it gives the model actual language context
    for how these terms get used, not just isolated tokens.
    """
    terms = get_vocabulary()
    if not terms:
        return ""
    return "Relevant names and terms that may come up: " + ", ".join(terms) + "."
