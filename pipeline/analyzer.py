"""
Turns a transcript into Omi-style structured output (title, overview,
category, action items, key facts) via a local Ollama model.

Used automatically by watcher.py after each successful transcription.
"""
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Optional

import config
import data_store
from prompts import (
    SYSTEM_PROMPT,
    build_user_prompt,
    SEGMENT_DETECTION_SYSTEM_PROMPT,
    build_segment_detection_prompt,
)

log = logging.getLogger("omi.analyzer")

EXPECTED_KEYS = {
    "title", "overview", "category", "atmosphere", "participants", "topics",
    "decisions_made", "action_items", "key_facts", "mentioned_lists",
}

# Common placeholder phrasings the model uses instead of genuinely returning
# an empty list when no real decision was reached — same anti-pattern as
# the participants-padding bug, just showing up in a different field.
# Deliberately a curated set of exact (normalized) matches rather than a
# broad "starts with no/none" pattern, since a real decision could
# legitimately start with a similar word (e.g. "No, they decided not to
# renew...") and a blanket pattern would risk dropping real content.
_PLACEHOLDER_NON_DECISION_PHRASES = {
    "none", "none explicitly stated", "none stated", "none mentioned",
    "no decisions made", "no decisions were made", "no decision was made",
    "n/a", "not applicable", "nothing was decided", "no clear decisions",
    "no decisions", "not stated", "no decisions were reached",
    "no decision was reached",
}

# #69: below this word count, a transcript is treated as noise/silence
# rather than a real conversation — VAD can trigger a save (something
# crossed the speech threshold) while faster-whisper then transcribes
# little or nothing intelligible from it (a stray sound, a trailing
# half-word, or speech quiet/whispered enough that transcription can't
# resolve real words). Confirmed real via a whispered conversation that
# got fully analyzed and shown as an "Empty Transcript" conversation in
# the UI — the model handled it honestly per the empty-transcript prompt
# rule, but nothing upstream of that decided it shouldn't become a
# conversation entry at all.
MIN_WORDS_FOR_ANALYSIS = 4

# #60 Phase 1: below this duration, skip the merged-conversation detection
# pass entirely — a short recording is both unlikely to have actually
# captured multiple genuinely separate, unrelated moments, and gives the
# model less content to work with either way (more room to misjudge on too
# little material). Tune freely once this has run against real data —
# there's no principled reason 90s is exactly right, just a reasonable
# starting point.
MIN_DURATION_FOR_SEGMENT_CHECK_SECONDS = 90


def _is_trivial_transcript(transcript_text: str) -> bool:
    """
    True when there's essentially nothing here to analyze. Strips the
    optional "[This conversation had N distinct speakers.]" note that
    transcribe.build_analysis_text() prepends before counting, so that
    note's own handful of words never mask an otherwise-empty transcript.
    """
    stripped = re.sub(r"^\[.*?\]\s*", "", transcript_text.strip())
    return len(stripped.split()) < MIN_WORDS_FOR_ANALYSIS


def _is_placeholder_non_decision(text: str) -> bool:
    normalized = text.strip().lower().rstrip(".")
    return normalized in _PLACEHOLDER_NON_DECISION_PHRASES


def _find_unsupported_numbers(facts: list, source_text: str) -> list:
    """
    Flags any key_facts entry containing a number that doesn't appear in
    the source transcript in the same shape (a plain count vs. a
    percentage) — the caller actively removes flagged facts (see #40:
    this was logging-only at first, then upgraded to real filtering once
    the exact fabrication pattern recurred on a second real recording with
    zero false positives observed). Deliberately checks shape, not just
    raw substring presence: a naive substring check can't tell "100" (a
    container count) from "100%" (a coverage percentage) — since "100"
    trivially appears inside "100%" too — which is exactly the fabrication
    pattern confirmed in a real test: the model took "100% coverage" and
    turned it into a fabricated "100 containers."

    Still a real limitation worth knowing: a number phrased differently in
    the fact than in the transcript (e.g. "100" vs "one hundred") would
    still be a false positive here, silently dropping a legitimate fact.
    Accepted trade-off given the alternative (letting the confirmed
    fabrication pattern through) is worse.
    """
    flagged = []
    source_plain = source_text.replace(",", "")
    for fact in facts:
        fact_plain = fact.replace(",", "")
        for match in re.finditer(r"\d+", fact_plain):
            num = match.group()
            is_percentage_in_fact = fact_plain[match.end():match.end() + 1] == "%"
            if is_percentage_in_fact:
                found = re.search(rf"(?<!\d){num}%", source_plain)
            else:
                found = re.search(rf"(?<!\d){num}(?!\d)(?!%)", source_plain)
            if not found:
                flagged.append(fact)
                break
    return flagged


def analyze_transcript(transcript_text: str) -> dict:
    """
    Calls Ollama to produce structured JSON output for the given transcript
    text. Raises RuntimeError on any failure (unreachable Ollama, invalid
    JSON, missing expected fields) — callers should catch this and treat it
    as a soft failure, not a reason to lose the underlying transcript.
    """
    existing_list_names = data_store.get_all_list_names()

    payload = {
        "model": config.OLLAMA_MODEL,
        "system": SYSTEM_PROMPT,
        "prompt": build_user_prompt(transcript_text, existing_list_names),
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.2},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{config.OLLAMA_HOST}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 — scheme validated in config.py
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

    # Defensive cleanup, not just prompt engineering: despite explicit
    # instructions to return an empty list when nobody is named, this model
    # persistently pads "participants" with one null-name placeholder per
    # detected speaker instead. Enforced here so the STORED analysis is
    # clean, not just whatever the API happens to filter on the way out.
    result["participants"] = [p for p in result.get("participants", []) if p.get("name")]
    result["decisions_made"] = [
        d for d in result.get("decisions_made", []) if not _is_placeholder_non_decision(d)
    ]

    # Upgraded from logging-only to active filtering (see #40) — this
    # exact fabrication pattern (a percentage repurposed into a fabricated
    # count, "100% coverage" -> "100 containers") recurred on a SECOND
    # real recording, confirming it's a real, repeating failure mode, not
    # a one-off. The shape-matching check has shown zero false positives
    # across both real occurrences and unit testing (correctly leaves
    # alone facts whose numbers genuinely match the source), so removing
    # rather than just flagging is justified by actual evidence now.
    key_facts = result.get("key_facts", [])
    suspicious = _find_unsupported_numbers(key_facts, transcript_text)
    if suspicious:
        log.warning("Removed key_facts with a number not found in the source transcript: %s", suspicious)
        result["key_facts"] = [f for f in key_facts if f not in suspicious]

    # Same fabrication risk, same check, applied to topic details too —
    # a number can be repurposed into a fabricated claim there just as
    # easily as in key_facts, and topics/details is new enough (see the
    # restructuring this was added alongside) that it hasn't had the
    # benefit of real-world testing key_facts already had when this
    # check was first added.
    for topic in result.get("topics", []):
        details = topic.get("details", [])
        suspicious_details = _find_unsupported_numbers(details, transcript_text)
        if suspicious_details:
            log.warning(
                "Removed topic details with a number not found in the source transcript (topic=%r): %s",
                topic.get("topic"), suspicious_details,
            )
            topic["details"] = [d for d in details if d not in suspicious_details]

    return result


def _maybe_detect_possible_segments(stem: str) -> Optional[dict]:
    """
    #60 Phase 1 (detect + flag only — nothing is actually split yet): a
    best-effort, soft-failing check for whether this recording might
    actually have captured multiple genuinely distinct, unrelated
    conversations merged into one file (e.g. moving between rooms, or an
    unrelated moment following a long-but-real pause). Returns None (no
    warning shown) on any failure, on short recordings, or when detection
    itself concludes it's one real conversation — never raises, and never
    affects whether the main analysis above succeeds or what it contains.

    Deliberately a SEPARATE Ollama call using segment-level timestamps
    read directly from the raw <stem>.json transcript, rather than folding
    this into analyze_transcript() or reusing build_analysis_text()'s
    output — that text is deliberately timestamp-free flowing prose (see
    its own docstring: reformatting it previously caused a confirmed
    regression in key_points quality and participant hallucination),
    which is exactly the opposite of what this specific check needs.
    Keeping this fully separate means it can never regress the main,
    already-tuned analysis path, even if this detection pass itself turns
    out to need more tuning later.
    """
    transcript_path = config.TRANSCRIPTS_DIR / f"{stem}.json"
    if not transcript_path.exists():
        return None
    try:
        raw = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if raw.get("duration", 0) < MIN_DURATION_FOR_SEGMENT_CHECK_SECONDS:
        return None

    segments = [s for s in raw.get("segments", []) if s.get("text", "").strip()]
    if len(segments) < 2:
        return None

    payload = {
        "model": config.OLLAMA_MODEL,
        "system": SEGMENT_DETECTION_SYSTEM_PROMPT,
        "prompt": build_segment_detection_prompt(segments),
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.2},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{config.OLLAMA_HOST}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 — scheme validated in config.py
            body = json.loads(resp.read().decode("utf-8"))
        detection = json.loads(body.get("response", ""))
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        log.warning("Segment-boundary detection failed for %s (main analysis unaffected): %s", stem, e)
        return None

    if detection.get("is_single_conversation", True):
        return None

    flagged_segments = detection.get("segments") or []
    if len(flagged_segments) < 2:
        # A "split" that isn't actually multiple pieces is a degenerate
        # signal, not a real finding — don't surface a confusing warning
        # over what amounts to noise.
        return None

    log.info("Possible multi-conversation split detected for %s: %d segments", stem, len(flagged_segments))
    return {"segments": flagged_segments}


def analyze_and_write(transcript_text: str, stem: str) -> None:
    """
    Runs analysis and writes `<stem>.analysis.json` into TRANSCRIPTS_DIR,
    alongside the existing `<stem>.json` / `<stem>.txt` transcript files.
    Logs and returns quietly on failure — does not raise — since this is
    always called as a best-effort step after a transcript already exists.

    #69: Also returns quietly (no LLM call made, no file written) when the
    transcript itself is trivial — see _is_trivial_transcript(). The raw
    transcript/audio stay on disk and archive normally either way; this
    only decides whether it gets promoted into a full "conversation" shown
    in the UI.

    #60 Phase 1: also runs a separate, best-effort check for whether this
    recording might actually be multiple distinct, unrelated conversations
    merged into one — if so, attaches a non-authoritative
    "possible_distinct_conversations" field to the written analysis for
    the UI to surface as a warning. Detection-only: nothing is actually
    split, no new conversation entries are created, and a failure/negative
    result here never blocks the real analysis above from being written.
    """
    if _is_trivial_transcript(transcript_text):
        log.info(
            "Skipping analysis for %s: transcript has essentially no content (< %d words)",
            stem, MIN_WORDS_FOR_ANALYSIS,
        )
        return

    try:
        result = analyze_transcript(transcript_text)
    except RuntimeError as e:
        log.warning("Analysis failed for %s (transcript is still kept): %s", stem, e)
        return

    result["possible_distinct_conversations"] = _maybe_detect_possible_segments(stem)

    out_path = config.TRANSCRIPTS_DIR / f"{stem}.analysis.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote analysis: %s", out_path.name)
