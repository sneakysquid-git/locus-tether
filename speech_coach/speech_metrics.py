"""
Phase 6 (speaking-style analysis): deterministic metrics computed directly
from Whisper's transcript output — no new dependencies, no audio
re-processing. These are the "free" measurements: pace, pauses, and filler
word frequency. Qualitative coaching feedback (grounded in these numbers)
comes from an LLM call in speech_coach.py, which uses this module's output
as input.

Honest limitation up front: filler-word detection here is a simple
word/phrase matcher, not true intent detection. Words like "like", "so", and
"actually" have plenty of legitimate non-filler uses ("I like this",
"So, what's next?"), so counts for those are a rough signal, not a precise
measurement — worth reading as "worth noticing" rather than "definitely a
problem." The list below leans toward phrases that are filler far more
often than not, to keep false positives down.
"""
import json
import re
from pathlib import Path
from typing import Optional

# Ordered roughly by how reliably these actually indicate filler/hedging
# rather than legitimate use. "like" and "so" are deliberately excluded from
# the default list — both are extremely common as ordinary words, and
# counting them naively produces mostly noise rather than signal.
FILLER_PHRASES = [
    "um", "umm", "uh", "uhh", "er", "erm",
    "you know",
    "i mean",
    "sort of",
    "kind of",
    "basically",
    "literally",
    "actually",
]


def load_transcript(transcript_json_path: Path) -> dict:
    return json.loads(transcript_json_path.read_text(encoding="utf-8"))


def filter_to_main_user(transcript: dict) -> dict:
    """
    Filters a transcript down to only the main user's own segments (#37,
    voice enrollment) — critical for multi-speaker recordings, since
    computing pace/filler-word/pause metrics across the WHOLE transcript
    would blend everyone's speech patterns together, giving meaningless
    feedback about the wearer's own speaking style rather than theirs
    specifically.

    "duration" gets recomputed as just the main user's own total speaking
    time (sum of their segment durations), not the full recording's
    wall-clock length — using the wrong duration here would understate
    their actual words-per-minute pace, since they weren't necessarily
    talking the entire time.

    Falls back to the transcript UNCHANGED (the original single-speaker-
    assuming behavior from before enrollment existed) if: no main user is
    enrolled, or no segment in THIS specific recording has a speaker label
    matching them (they weren't part of this conversation, or diarization
    is disabled/failed/never ran) — deliberately the same behavior as
    before, not a new failure mode.
    """
    import speaker_profiles

    main_user = speaker_profiles.get_main_user()
    segments = transcript.get("segments", [])

    if not main_user or not any(seg.get("speaker") == main_user for seg in segments):
        return transcript

    main_user_segments = [seg for seg in segments if seg.get("speaker") == main_user]
    total_speaking_time = sum(seg["end"] - seg["start"] for seg in main_user_segments)

    filtered = dict(transcript)
    filtered["segments"] = main_user_segments
    filtered["text"] = " ".join(seg["text"] for seg in main_user_segments)
    filtered["duration"] = total_speaking_time
    return filtered


def compute_pace(transcript: dict) -> dict:
    """
    Words per minute, from total word count and audio duration. A word
    count via whitespace splitting is rough (doesn't perfectly handle
    contractions/punctuation) but is a fine approximation for a pace metric
    — small counting error doesn't meaningfully change a WPM figure.
    """
    word_count = len(transcript["text"].split())
    duration_minutes = transcript["duration"] / 60
    wpm = word_count / duration_minutes if duration_minutes > 0 else 0
    return {
        "word_count": word_count,
        "duration_seconds": round(transcript["duration"], 1),
        "words_per_minute": round(wpm, 1),
    }


def compute_pauses(transcript: dict, notable_threshold_seconds: float = 1.5) -> dict:
    """
    Gaps between consecutive Whisper segments, treated as pauses. Only
    counts gaps between segments that Whisper actually produced — VAD
    already stripped leading/trailing silence and long dead air before
    transcription even started, so this measures pauses WITHIN the speech
    that was transcribed, not silence in the original recording overall.
    """
    segments = transcript.get("segments", [])
    if len(segments) < 2:
        return {"pause_count": 0, "total_pause_seconds": 0.0, "longest_pause_seconds": 0.0, "notable_pauses": []}

    pauses = []
    for prev_seg, next_seg in zip(segments, segments[1:]):
        gap = next_seg["start"] - prev_seg["end"]
        if gap > 0:
            pauses.append(gap)

    notable = [round(p, 1) for p in pauses if p >= notable_threshold_seconds]

    return {
        "pause_count": len(pauses),
        "total_pause_seconds": round(sum(pauses), 1),
        "longest_pause_seconds": round(max(pauses), 1) if pauses else 0.0,
        "notable_pauses": notable,  # pauses >= threshold, worth specifically mentioning in feedback
    }


def compute_filler_words(transcript: dict) -> dict:
    """
    Case-insensitive, word-boundary-aware count of each filler phrase in
    FILLER_PHRASES. Returns both raw counts and a rate per 100 words, since
    "said um 8 times" means something very different in a 30-second clip
    versus a 20-minute one.
    """
    text = transcript["text"].lower()
    word_count = max(len(text.split()), 1)  # avoid division by zero on empty transcripts

    counts = {}
    for phrase in FILLER_PHRASES:
        pattern = r"\b" + re.escape(phrase) + r"\b"
        matches = re.findall(pattern, text)
        if matches:
            counts[phrase] = len(matches)

    total = sum(counts.values())
    return {
        "filler_counts": counts,
        "total_filler_count": total,
        "filler_rate_per_100_words": round((total / word_count) * 100, 1),
    }


def analyze_speech_metrics(transcript: dict) -> dict:
    """Combines all three deterministic metrics into one dict."""
    return {
        "pace": compute_pace(transcript),
        "pauses": compute_pauses(transcript),
        "fillers": compute_filler_words(transcript),
    }
