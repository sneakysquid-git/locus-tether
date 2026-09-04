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

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "key_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source_ref": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["text", "source_ref", "evidence"],
            },
        },
    },
    "required": ["key_points"],
}


def extract_chunk_summary(chunk: str) -> dict:
    prompt = f"""Analyse this automatically recorded transcript chunk.

Extract only meaningful facts, events, topics, or outcomes that would be useful
in a daily summary.

STRICT RULES:
- Every item must be directly supported by the transcript.
- Every item must cite exactly one source reference from the square brackets.
- evidence must be an exact verbatim quote copied from that same referenced line.
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
        source_ref = str(item.get("source_ref", "")).strip()
        evidence = str(item.get("evidence", "")).strip()

        if not text:
            continue

        if not evidence_is_valid(source_ref, evidence, segment_index):
            continue

        valid.append(
            {
                "text": text,
                "source_ref": source_ref,
                "evidence": evidence,
            }
        )

    return valid

DAILY_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "supporting_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["text", "supporting_ids"],
            },
        },
    },
    "required": ["overview", "highlights"],
}


def synthesise_daily_summary(key_points: list[dict]) -> dict:
    if not key_points:
        return {
            "overview": "No meaningful activity was identified for this day.",
            "highlights": [],
        }

    lines = []

    for index, item in enumerate(key_points, 1):
        point_id = f"P{index:04d}"
        lines.append(
            f"[{point_id}] {item['evidence']} "
            f"(source: {item['source_ref']})"
        )

    evidence_text = "\n".join(lines)

    prompt = f"""Create a concise daily activity summary using ONLY the validated
evidence points below.

STRICT RULES:
- Do not introduce any fact that is not present in the evidence points.
- Do not infer names, relationships, motives, locations, or context.
- Focus on meaningful activities, discussions, outcomes, and events.
- Ignore trivial repetition.
- overview should be a short high-level summary of the day.
- Each highlight must cite one or more supporting point IDs exactly as provided.
- Prefer 5-12 useful highlights rather than an exhaustive list.

VALIDATED EVIDENCE:
{evidence_text}
"""

    return call_ollama(prompt, DAILY_SUMMARY_SCHEMA)

def validate_daily_summary(
    result: dict,
    key_points: list[dict],
) -> dict:
    valid_ids = {
        f"P{index:04d}"
        for index in range(1, len(key_points) + 1)
    }

    overview = str(result.get("overview", "")).strip()
    highlights: list[dict] = []

    for item in result.get("highlights", []):
        if not isinstance(item, dict):
            continue

        text = str(item.get("text", "")).strip()
        supporting_ids = item.get("supporting_ids", [])

        if not text or not isinstance(supporting_ids, list):
            continue

        cleaned_ids = [
            str(point_id).strip()
            for point_id in supporting_ids
            if str(point_id).strip() in valid_ids
        ]

        if not cleaned_ids:
            continue

        highlights.append(
            {
                "text": text,
                "supporting_ids": cleaned_ids,
            }
        )

    return {
        "overview": overview,
        "highlights": highlights,
    }

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
    seen_points: set[tuple[str, str]] = set()

    for chunk in chunks:
        result = extract_chunk_summary(chunk)
        validated = validate_key_points(result, segment_index)

        for item in validated:
            identity = (item["source_ref"], item["evidence"])

            if identity in seen_points:
                continue

            seen_points.add(identity)
            key_points.append(item)

    synthesis = synthesise_daily_summary(key_points)
    validated_summary = validate_daily_summary(synthesis, key_points)

    recordings = len({segment.source_file for segment in segments})

    return {
        "date": target_date.isoformat(),
        "recordings": recordings,
        "segments": len(segments),
        "chunks": len(chunks),
        "overview": validated_summary["overview"],
        "highlights": validated_summary["highlights"],
        "evidence_points": key_points,
    }

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python digest/daily_summary.py YYYY-MM-DD")

    target_date = date.fromisoformat(sys.argv[1])
    result = generate_daily_summary(target_date)

    print(json.dumps(result, indent=2, ensure_ascii=False))