#!/usr/bin/env python3
"""
Adds a real recording's already-computed speaker embedding as an
additional enrollment reference for a named person — for exactly the case
that motivated this: a near-miss match (a real voice, just under the
similarity threshold) on a specific recording, where using that actual
recording's own embedding as a new reference sample is more targeted than
recording a fresh, separate enrollment sample.

Requires diarize.py's embeddings-persistence change to have already run on
the target recording — i.e. the recording must have been processed (or
reprocessed with full diarization, not just the Reprocess button, which
reuses the existing transcript without re-running diarization) AFTER that
change was deployed. Look for `<stem>.embeddings.json` in transcripts/ to
confirm one exists before running this.

Usage:
    python3 add_embedding_as_profile.py "Zoom Recording Aug 11 2026 (retest)" SPEAKER_01 Eric --main-user

Run this on the HOST (not inside the container) — it only touches small
JSON files, no GPU/ML dependencies needed, same as webapp.py's own
speaker-management code.
"""
import argparse
import json
import sys

import config
import speaker_profiles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stem", help="Recording filename without extension, e.g. 'Zoom Recording Aug 11 2026 (retest)'")
    parser.add_argument("speaker_id", help="Which detected speaker to use, e.g. SPEAKER_01 (check the transcript JSON or logs to confirm which one)")
    parser.add_argument("name", help="Name to enroll this embedding under, e.g. Eric")
    parser.add_argument("--main-user", action="store_true", help="Mark this person as the main user (for speech coaching)")
    args = parser.parse_args()

    embeddings_path = config.TRANSCRIPTS_DIR / f"{args.stem}.embeddings.json"
    if not embeddings_path.exists():
        print(f"No saved embeddings file found at: {embeddings_path}", file=sys.stderr)
        print(
            "This recording may have been processed before the embeddings-persistence "
            "change was deployed, or diarization failed for it. Check the pipeline logs "
            "for that recording, or reprocess it with a fresh copy (a plain Reprocess "
            "button click reuses the existing transcript and won't generate this file).",
            file=sys.stderr,
        )
        return 1

    embeddings = json.loads(embeddings_path.read_text(encoding="utf-8"))
    if args.speaker_id not in embeddings:
        print(f"'{args.speaker_id}' not found in {embeddings_path.name}. Available: {list(embeddings.keys())}", file=sys.stderr)
        return 1

    embedding = embeddings[args.speaker_id]
    speaker_profiles.add_profile(args.name, embedding, is_main_user=args.main_user)

    profiles = speaker_profiles.list_profiles()
    matched = next((p for p in profiles if p["name"] == args.name), None)
    print(f"Added. '{args.name}' now has {matched['sample_count']} reference sample(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
