import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen


import os

TRANSCRIPTS_DIR = Path(
    os.environ.get("OMI_TRANSCRIPTS_DIR", "/mnt/tank2/omi/transcripts")
)

OLLAMA_HOST = os.environ.get("OMI_OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OMI_SUMMARY_MODEL", "qwen2.5:7b-instruct")

_FILENAME_RE = re.compile(r"^omi-(\d{8})_(\d{6})\.json$")


@dataclass(frozen=True)
class Segment:
    ref: str
    source_file: str
    recording_time: str
    start: float
    end: float
    text: str


def load_day_segments(target_date: date) -> list[Segment]:
    day_token = target_date.strftime("%Y%m%d")
    segments: list[Segment] = []

    for path in sorted(TRANSCRIPTS_DIR.glob(f"omi-{day_token}_*.json")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        recording_time = match.group(2)

        for index, item in enumerate(data.get("segments", [])):
            text = str(item.get("text", "")).strip()
            if not text:
                continue

            try:
                start = float(item.get("start", 0))
                end = float(item.get("end", start))
            except (TypeError, ValueError):
                continue

            ref = f"{path.stem}:{index:04d}:{start:.2f}"

            segments.append(
                Segment(
                    ref=ref,
                    source_file=path.name,
                    recording_time=recording_time,
                    start=start,
                    end=end,
                    text=text,
                )
            )

    return segments

def chunk_segments(
    segments: list[Segment],
    max_chars: int = 12000,
) -> list[str]:
    chunks: list[str] = []
    current_lines: list[str] = []
    current_size = 0

    for segment in segments:
        line = f"[{segment.ref}] {segment.text}"
        line_size = len(line) + 1

        if current_lines and current_size + line_size > max_chars:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_size = 0

        current_lines.append(line)
        current_size += line_size

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks

def build_segment_index(segments: list[Segment]) -> dict[str, Segment]:
    return {segment.ref: segment for segment in segments}


def evidence_is_valid(
    ref: str,
    quote: str,
    segment_index: dict[str, Segment],
) -> bool:
    segment = segment_index.get(ref)
    if segment is None:
        return False

    quote = quote.strip()
    if not quote:
        return False

    return quote in segment.text

def call_ollama(prompt: str, schema: dict) -> dict:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": 0,
        "options": {
            "temperature": 0,
        },
        "format": schema,
    }

    request = Request(
        f"{OLLAMA_HOST}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urlopen(request, timeout=300) as response:
        outer = json.loads(response.read().decode("utf-8"))

    if outer.get("error"):
        raise RuntimeError(f"Ollama error: {outer['error']}")

    raw = outer.get("response", "")
    if not raw:
        raise RuntimeError("Ollama returned an empty response")

    result = json.loads(raw)

    if not isinstance(result, dict):
        raise RuntimeError("Ollama response was not a JSON object")

    return result

DAILY_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["selected_ids"],
}


def extract_chunk_summary(chunk: str) -> dict:
    prompt = f"""Analyse this automatically recorded transcript chunk.

Extract only meaningful facts, events, topics, or outcomes that would be useful
in a daily summary.

STRICT RULES:
- Every item must be directly supported by the transcript.
- A key point may use one or more adjacent transcript segments when needed.
- evidence must be an array of source references and exact verbatim quotes.
- Every source_ref must match a reference shown in square brackets.
- Every quote must be copied exactly from its referenced transcript line.
- Do not combine separate unrelated events into one key point.
- Do not invent names, relationships, motives, context, dates, or conclusions.
- Ignore greetings, filler, fragments, and trivial chatter unless genuinely significant.
- Prefer a small number of useful points over exhaustive transcription.
- If nothing meaningful is present, return an empty key_points array.

TRANSCRIPT:
{chunk}
"""

    return call_ollama(prompt, SUMMARY_SCHEMA)

def validate_key_points(
    result: dict,
    segment_index: dict[str, Segment],
) -> list[dict]:
    valid: list[dict] = []

    for item in result.get("key_points", []):
        if not isinstance(item, dict):
            continue

        text = str(item.get("text", "")).strip()
        evidence_items = item.get("evidence", [])

        if not text or not isinstance(evidence_items, list) or not evidence_items:
            continue

        validated_evidence: list[dict] = []
        item_is_valid = True

        for evidence in evidence_items:
            if not isinstance(evidence, dict):
                item_is_valid = False
                break

            source_ref = str(evidence.get("source_ref", "")).strip()
            quote = str(evidence.get("quote", "")).strip()

            if source_ref.startswith("[") and source_ref.endswith("]"):
                source_ref = source_ref[1:-1].strip()

            if not evidence_is_valid(source_ref, quote, segment_index):
                item_is_valid = False
                break

            validated_evidence.append(
                {
                    "source_ref": source_ref,
                    "quote": quote,
                }
            )

        if not item_is_valid:
            continue

        valid.append(
            {
                "text": text,
                "evidence": validated_evidence,
            }
        )

    return valid

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "key_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_ref": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["source_ref", "quote"],
                        },
                    },
                },
                "required": ["text", "evidence"],
            },
        },
    },
    "required": ["key_points"],
}


def synthesise_daily_summary(key_points: list[dict]) -> dict:
    if not key_points:
        return {"selected_ids": []}

    lines: list[str] = []

    for index, item in enumerate(key_points, 1):
        point_id = f"P{index:04d}"
        quotes = " ".join(
            evidence["quote"]
            for evidence in item["evidence"]
        )

        lines.append(
            f"[{point_id}] Evidence: {quotes}"
        )

    evidence_text = "\n\n".join(lines)

    prompt = f"""Select the most useful validated points for a daily activity summary.

STRICT RULES:
- You may ONLY select point IDs from the list below.
- Do not create or rewrite any factual content.
- Select points representing meaningful activities, discussions, decisions, commitments, or events.
- Ignore trivial chatter, repetition, incomplete fragments, and low-value observations.
- Prefer a concise selection rather than including everything.
- Return selected_ids in chronological order.
- If none are useful, return an empty selected_ids array.

VALIDATED POINTS:
{evidence_text}
"""

    return call_ollama(prompt, DAILY_SUMMARY_SCHEMA)

def validate_daily_summary(
    result: dict,
    key_points: list[dict],
) -> list[str]:
    valid_ids = {
        f"P{index:04d}"
        for index in range(1, len(key_points) + 1)
    }

    selected_ids = result.get("selected_ids", [])

    if not isinstance(selected_ids, list):
        return []

    validated: list[str] = []
    seen: set[str] = set()

    for point_id in selected_ids:
        point_id = str(point_id).strip()

        if point_id not in valid_ids:
            continue

        if point_id in seen:
            continue

        seen.add(point_id)
        validated.append(point_id)

    return validated

def generate_daily_summary(target_date: date) -> dict:
    segments = load_day_segments(target_date)

    if not segments:
        return {
            "date": target_date.isoformat(),
            "recordings": 0,
            "segments": 0,
            "overview": "No recordings were found for this day.",
            "highlights": [],
            "evidence_points": [],
        }

    segment_index = build_segment_index(segments)
    chunks = chunk_segments(segments)

    key_points: list[dict] = []
    seen_points: set[tuple] = set()

    for chunk in chunks:
        result = extract_chunk_summary(chunk)
        validated = validate_key_points(result, segment_index)

        for item in validated:
            identity = (
                item["text"],
                tuple(
                    (evidence["source_ref"], evidence["quote"])
                    for evidence in item["evidence"]
                ),
            )

            if identity in seen_points:
                continue

            seen_points.add(identity)
            key_points.append(item)

    synthesis = synthesise_daily_summary(key_points)
    selected_ids = validate_daily_summary(synthesis, key_points)

    selected_points = [
        {
            "id": point_id,
            "text": key_points[int(point_id[1:]) - 1]["text"],
            "evidence": key_points[int(point_id[1:]) - 1]["evidence"],
        }
        for point_id in selected_ids
    ]

    recordings = len({segment.source_file for segment in segments})

    return {
        "date": target_date.isoformat(),
        "recordings": recordings,
        "segments": len(segments),
        "chunks": len(chunks),
        "selected_points": selected_points,
        "evidence_points": key_points,
    }

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python digest/daily_summary.py YYYY-MM-DD")

    target_date = date.fromisoformat(sys.argv[1])
    result = generate_daily_summary(target_date)

    print(json.dumps(result, indent=2, ensure_ascii=False))