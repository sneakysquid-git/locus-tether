"""
Phase 4: turn a transcript (produced by the watcher/transcribe pipeline) into
Omi-style structured output using a local Ollama model.

Standalone for now, deliberately — run it by hand against real transcripts
while we dial in the prompt and pick a model, before wiring it into
watcher.py as an automatic post-transcription step.

Usage:
    python3 analyze.py /home/efranklin/omi-data/transcripts/testrec.json

Requires Ollama running locally (default http://localhost:11434) with the
model already pulled (`ollama pull <model>`).
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from prompts import SYSTEM_PROMPT, build_user_prompt

OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "llama3.1:8b"  # swap once you've picked a model based on benchmarking

EXPECTED_KEYS = {"title", "overview", "category", "emoji", "action_items", "key_facts"}


def call_ollama(transcript_text: str, model: str = OLLAMA_MODEL) -> dict:
    payload = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "prompt": build_user_prompt(transcript_text),
        "format": "json",  # Ollama enforces valid JSON output when this is set
        "stream": False,
        "options": {
            "temperature": 0.2,  # low temperature: we want consistent, grounded extraction, not creativity
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST} — is `ollama serve` running? ({e})"
        ) from e

    raw_response = body.get("response", "")
    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Ollama did not return valid JSON despite format='json'. Raw output:\n{raw_response}"
        ) from e

    missing = EXPECTED_KEYS - result.keys()
    if missing:
        raise RuntimeError(f"Model output is missing expected keys: {missing}\nFull output: {result}")

    return result


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 analyze.py <path-to-transcript.json>")
        sys.exit(1)

    transcript_path = Path(sys.argv[1])
    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript_text = transcript_data["text"]

    print(f"Analyzing: {transcript_path.name}")
    print(f"Transcript length: {len(transcript_text)} chars")
    print(f"Model: {OLLAMA_MODEL}")
    print("-" * 60)

    result = call_ollama(transcript_text)

    print(json.dumps(result, indent=2, ensure_ascii=False))

    out_path = transcript_path.with_suffix(".analysis.json")
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print("-" * 60)
    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    main()
