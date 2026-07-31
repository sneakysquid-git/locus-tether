"""
Manual CLI for testing the analysis prompt against a transcript, without
waiting for the full watcher pipeline. Thin wrapper around analyzer.py (the
same module watcher.py uses automatically) — kept here for quick manual
iteration on prompt wording against real transcripts.

Runs on the HOST directly (not in the container) since it talks to Ollama
at localhost:11434 natively. Requires pipeline/ (holding config.py,
prompts.py, analyzer.py, data_store.py) on the Python path, handled below.

Usage:
    cd analysis
    python3 analyze.py ~/omi-data/transcripts/testrec.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import analyzer  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 analyze.py <path-to-transcript.json>")
        sys.exit(1)

    transcript_path = Path(sys.argv[1])
    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript_text = transcript_data["text"]

    print(f"Analyzing: {transcript_path.name}")
    print(f"Transcript length: {len(transcript_text)} chars")
    print("-" * 60)

    result = analyzer.analyze_transcript(transcript_text)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    out_path = transcript_path.with_suffix(".analysis.json")
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print("-" * 60)
    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    main()
