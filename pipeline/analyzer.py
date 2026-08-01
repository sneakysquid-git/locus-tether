"""
Turns a transcript into Omi-style structured output (title, overview,
category, action items, key facts) via a local Ollama model.

Used automatically by watcher.py after each successful transcription.
"""
import json
import logging
import urllib.error
import urllib.request

import config
import data_store
from prompts import SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger("omi.analyzer")

EXPECTED_KEYS = {"title", "overview", "category", "action_items", "key_facts", "mentioned_lists"}


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


def analyze_and_write(transcript_text: str, stem: str) -> None:
    """
    Runs analysis and writes `<stem>.analysis.json` into TRANSCRIPTS_DIR,
    alongside the existing `<stem>.json` / `<stem>.txt` transcript files.
    Logs and returns quietly on failure — does not raise — since this is
    always called as a best-effort step after a transcript already exists.
    """
    try:
        result = analyze_transcript(transcript_text)
    except RuntimeError as e:
        log.warning("Analysis failed for %s (transcript is still kept): %s", stem, e)
        return

    out_path = config.TRANSCRIPTS_DIR / f"{stem}.analysis.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote analysis: %s", out_path.name)
