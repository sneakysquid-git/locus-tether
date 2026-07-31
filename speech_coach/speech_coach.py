"""
Phase 6: speaking-style coaching. Combines speech_metrics.py's deterministic
numbers (pace, pauses, filler words — all free, computed from data we
already generate) with an Ollama call for qualitative feedback grounded in
those numbers.

Standalone / on-demand rather than wired into the automatic watcher
pipeline — reviewing every casual voice memo for speaking style isn't
useful the way transcribing/summarizing every one is; this is meant to be
run deliberately against a specific recording you actually want feedback on
(a practice run of a talk, a pitch, etc.).

Usage:
    python3 speech_coach.py ~/omi-data/transcripts/testrec.json
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import config
import speech_metrics
from prompts import SPEECH_COACH_SYSTEM_PROMPT, build_speech_coach_prompt

EXPECTED_KEYS = {"strengths", "areas_to_improve", "pace_feedback", "overall_take"}


def get_coaching_feedback(transcript: dict, metrics: dict) -> dict:
    payload = {
        "model": config.OLLAMA_MODEL,
        "system": SPEECH_COACH_SYSTEM_PROMPT,
        "prompt": build_speech_coach_prompt(transcript["text"], metrics),
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.3},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{config.OLLAMA_HOST}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Ollama at {config.OLLAMA_HOST}: {e}") from e

    raw_response = body.get("response", "")
    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Ollama did not return valid JSON despite format='json'. Raw output:\n{raw_response}"
        ) from e

    missing = EXPECTED_KEYS - result.keys()
    if missing:
        raise RuntimeError(f"Model output missing expected keys: {missing}\nFull output: {result}")

    return result


def print_report(metrics: dict, feedback: dict) -> None:
    pace = metrics["pace"]
    pauses = metrics["pauses"]
    fillers = metrics["fillers"]

    print("=" * 60)
    print("METRICS")
    print("=" * 60)
    print(f"Duration: {pace['duration_seconds']}s | {pace['word_count']} words | {pace['words_per_minute']} WPM")
    print(
        f"Pauses: {pauses['pause_count']} total, longest {pauses['longest_pause_seconds']}s, "
        f"notable (>1.5s): {pauses['notable_pauses']}"
    )
    if fillers["filler_counts"]:
        print(f"Filler words: {fillers['filler_counts']} ({fillers['filler_rate_per_100_words']} per 100 words)")
    else:
        print("Filler words: none detected")

    print()
    print("=" * 60)
    print("COACHING FEEDBACK")
    print("=" * 60)

    if feedback["strengths"]:
        print("\nStrengths:")
        for s in feedback["strengths"]:
            print(f"  + {s}")

    if feedback["areas_to_improve"]:
        print("\nAreas to improve:")
        for area in feedback["areas_to_improve"]:
            print(f"  - {area['observation']}")
            print(f"    Example: \"{area['example']}\"")
            print(f"    Try instead: {area['suggestion']}")

    print(f"\nPace: {feedback['pace_feedback']}")
    print(f"\nOverall: {feedback['overall_take']}")


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 speech_coach.py <path-to-transcript.json>")
        sys.exit(1)

    transcript_path = Path(sys.argv[1])
    transcript = speech_metrics.load_transcript(transcript_path)

    metrics = speech_metrics.analyze_speech_metrics(transcript)
    feedback = get_coaching_feedback(transcript, metrics)

    print_report(metrics, feedback)

    out_path = transcript_path.with_suffix(".speech_coach.json")
    out_path.write_text(
        json.dumps({"metrics": metrics, "feedback": feedback}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nWrote: {out_path}")


if __name__ == "__main__":
    main()
