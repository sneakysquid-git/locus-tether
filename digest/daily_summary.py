import argparse
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TRANSCRIPTS_DIR = Path(
    os.environ.get("OMI_TRANSCRIPTS_DIR", "/mnt/tank2/omi/transcripts")
)
SUMMARIES_DIR = Path(
    os.environ.get("OMI_SUMMARIES_DIR", "/mnt/tank2/omi/daily-summaries")
)

OLLAMA_HOST = os.environ.get("OMI_OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OMI_SUMMARY_MODEL", "qwen2.5:7b-instruct")
OLLAMA_TIMEOUT = int(os.environ.get("OMI_OLLAMA_TIMEOUT", "300"))

CHUNK_MAX_CHARS = int(os.environ.get("OMI_SUMMARY_CHUNK_CHARS", "12000"))
MAX_EVIDENCE_SEGMENT_SPAN = int(
    os.environ.get("OMI_EVIDENCE_MAX_SEGMENT_SPAN", "12")
)
MAX_SELECTED_POINTS = int(os.environ.get("OMI_SUMMARY_MAX_POINTS", "12"))

_FILENAME_RE = re.compile(r"^omi-(\d{8})_(\d{6})\.json$")
_REF_RE = re.compile(r"^(omi-\d{8}_\d{6}):(\d+):(-?\d+(?:\.\d+)?)$")


@dataclass(frozen=True)
class Segment:
    ref: str
    source_file: str
    recording_time: str
    start: float
    end: float
    text: str


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

        raw_segments = data.get("segments", [])
        if not isinstance(raw_segments, list):
            continue

        recording_time = match.group(2)

        for index, item in enumerate(raw_segments):
            if not isinstance(item, dict):
                continue

            text = str(item.get("text", "")).strip()
            if not text:
                continue

            try:
                start = float(item.get("start", 0))
                end = float(item.get("end", start))
            except (TypeError, ValueError):
                continue

            if not math.isfinite(start) or not math.isfinite(end):
                continue

            if end < start:
                end = start

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
    max_chars: int = CHUNK_MAX_CHARS,
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


def normalise_source_ref(ref: str) -> str:
    ref = ref.strip()

    if ref.startswith("[") and ref.endswith("]"):
        ref = ref[1:-1].strip()

    return ref


def parse_source_ref(ref: str) -> tuple[str, int] | None:
    match = _REF_RE.match(ref)
    if not match:
        return None

    return match.group(1), int(match.group(2))


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


def resolve_evidence_ref(
    requested_ref: str,
    quote: str,
    segment_index: dict[str, Segment],
) -> str | None:
    requested_ref = normalise_source_ref(requested_ref)
    quote = quote.strip()

    if not quote:
        return None

    if evidence_is_valid(requested_ref, quote, segment_index):
        return requested_ref

    parsed = parse_source_ref(requested_ref)
    if parsed is None:
        return None

    recording_stem, _ = parsed

    matches = [
        segment.ref
        for segment in segment_index.values()
        if segment.ref.startswith(f"{recording_stem}:") and quote in segment.text
    ]

    if len(matches) == 1:
        return matches[0]

    return None


def evidence_group_is_coherent(evidence: list[dict]) -> bool:
    parsed_refs: list[tuple[str, int]] = []

    for item in evidence:
        parsed = parse_source_ref(item["source_ref"])
        if parsed is None:
            return False
        parsed_refs.append(parsed)

    recording_stems = {recording_stem for recording_stem, _ in parsed_refs}
    if len(recording_stems) != 1:
        return False

    indices = [index for _, index in parsed_refs]
    if max(indices) - min(indices) > MAX_EVIDENCE_SEGMENT_SPAN:
        return False

    return True


def call_ollama(prompt: str, schema: dict) -> dict:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": 0,
        "options": {"temperature": 0},
        "format": schema,
    }

    request = Request(
        f"{OLLAMA_HOST}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=OLLAMA_TIMEOUT) as response:
            outer = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP error {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Ollama at {OLLAMA_HOST}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Ollama returned invalid outer JSON") from exc

    if outer.get("error"):
        raise RuntimeError(f"Ollama error: {outer['error']}")

    raw = outer.get("response", "")
    if not raw:
        raise RuntimeError("Ollama returned an empty response")

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Ollama response field was not valid JSON") from exc

    if not isinstance(result, dict):
        raise RuntimeError("Ollama response was not a JSON object")

    return result


def extract_chunk_summary(chunk: str) -> dict:
    prompt = f"""Analyse this automatically recorded transcript chunk.

Extract only meaningful facts, events, topics, outcomes, decisions, or commitments
that would be useful in a daily activity summary.

The "text" field is only an internal scratch label to help you keep separate events
separate. It will not be trusted or stored as factual output.

STRICT RULES:
- Every key point must represent one coherent event or topic.
- Evidence for one key point should normally come from the same recording and nearby segments.
- A key point may use multiple adjacent transcript segments when needed.
- evidence must be an array of source references and exact verbatim quotes.
- Copy source_ref exactly from the transcript reference, without square brackets.
- Copy every quote exactly from its referenced transcript line.
- Never merge unrelated events into one key point.
- Do not invent names, relationships, motives, locations, dates, or conclusions.
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

    raw_points = result.get("key_points", [])
    if not isinstance(raw_points, list):
        return valid

    for item in raw_points:
        if not isinstance(item, dict):
            continue

        evidence_items = item.get("evidence", [])
        if not isinstance(evidence_items, list) or not evidence_items:
            continue

        validated_evidence: list[dict] = []
        seen_evidence: set[tuple[str, str]] = set()
        item_is_valid = True

        for evidence in evidence_items:
            if not isinstance(evidence, dict):
                item_is_valid = False
                break

            requested_ref = str(evidence.get("source_ref", "")).strip()
            quote = str(evidence.get("quote", "")).strip()

            resolved_ref = resolve_evidence_ref(requested_ref, quote, segment_index)
            if resolved_ref is None:
                item_is_valid = False
                break

            identity = (resolved_ref, quote)
            if identity in seen_evidence:
                continue

            seen_evidence.add(identity)
            validated_evidence.append(
                {
                    "source_ref": resolved_ref,
                    "quote": quote,
                }
            )

        if not item_is_valid or not validated_evidence:
            continue

        if not evidence_group_is_coherent(validated_evidence):
            continue

        valid.append({"evidence": validated_evidence})

    return valid


def key_point_sort_key(point: dict) -> tuple[str, int]:
    parsed_refs = [
        parsed
        for parsed in (
            parse_source_ref(item["source_ref"])
            for item in point["evidence"]
        )
        if parsed is not None
    ]

    if not parsed_refs:
        return ("", 0)

    return min(parsed_refs)


def deduplicate_key_points(key_points: list[dict]) -> list[dict]:
    deduplicated: list[dict] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    for item in sorted(key_points, key=key_point_sort_key):
        identity = tuple(
            (evidence["source_ref"], evidence["quote"])
            for evidence in item["evidence"]
        )

        if identity in seen:
            continue

        seen.add(identity)
        deduplicated.append(item)

    return deduplicated


def synthesise_daily_summary(key_points: list[dict]) -> dict:
    if not key_points:
        return {"selected_ids": []}

    lines: list[str] = []

    for index, item in enumerate(key_points, 1):
        point_id = f"P{index:04d}"
        quotes = " ".join(evidence["quote"] for evidence in item["evidence"])
        lines.append(f"[{point_id}] Evidence: {quotes}")

    evidence_text = "\n\n".join(lines)

    prompt = f"""Select the most useful validated evidence groups for a daily activity summary.

STRICT RULES:
- You may ONLY select point IDs from the list below.
- Do not create, rewrite, or infer any factual content.
- Select points representing meaningful activities, discussions, decisions,
  commitments, or events.
- Ignore trivial chatter, repetition, incomplete fragments, and low-value observations.
- Prefer a concise selection rather than including everything.
- Return selected_ids in chronological order.
- Select no more than {MAX_SELECTED_POINTS} points.
- If none are useful, return an empty selected_ids array.

VALIDATED EVIDENCE GROUPS:
{evidence_text}
"""

    return call_ollama(prompt, DAILY_SUMMARY_SCHEMA)


def validate_selected_ids(result: dict, key_points: list[dict]) -> list[str]:
    valid_ids = {
        f"P{index:04d}"
        for index in range(1, len(key_points) + 1)
    }

    selected_ids = result.get("selected_ids", [])
    if not isinstance(selected_ids, list):
        return []

    validated: list[str] = []
    seen: set[str] = set()

    for raw_point_id in selected_ids:
        point_id = str(raw_point_id).strip()

        if point_id not in valid_ids or point_id in seen:
            continue

        seen.add(point_id)
        validated.append(point_id)

    validated.sort(key=lambda point_id: int(point_id[1:]))
    return validated[:MAX_SELECTED_POINTS]


def generate_daily_summary(target_date: date) -> dict:
    segments = load_day_segments(target_date)

    if not segments:
        return {
            "date": target_date.isoformat(),
            "model": OLLAMA_MODEL,
            "recordings": 0,
            "segments": 0,
            "chunks": 0,
            "selected_points": [],
            "evidence_points": [],
        }

    segment_index = build_segment_index(segments)
    chunks = chunk_segments(segments)
    extracted_points: list[dict] = []

    for chunk in chunks:
        raw_result = extract_chunk_summary(chunk)
        extracted_points.extend(validate_key_points(raw_result, segment_index))

    key_points = deduplicate_key_points(extracted_points)
    selection = synthesise_daily_summary(key_points)
    selected_ids = validate_selected_ids(selection, key_points)

    selected_points = [
        {
            "id": point_id,
            "evidence": key_points[int(point_id[1:]) - 1]["evidence"],
        }
        for point_id in selected_ids
    ]

    recordings = len({segment.source_file for segment in segments})

    return {
        "date": target_date.isoformat(),
        "model": OLLAMA_MODEL,
        "recordings": recordings,
        "segments": len(segments),
        "chunks": len(chunks),
        "selected_points": selected_points,
        "evidence_points": key_points,
    }


def render_markdown(result: dict) -> str:
    lines = [
        f"# Omi Daily Summary - {result['date']}",
        "",
        (
            f"{result['recordings']} recordings | "
            f"{result['segments']} transcript segments | "
            f"{result['chunks']} processing chunks"
        ),
        "",
        "## Highlights",
        "",
    ]

    selected_points = result.get("selected_points", [])

    if not selected_points:
        lines.append("No meaningful activity was identified.")
        lines.append("")
        return "\n".join(lines)

    for point in selected_points:
        evidence = point.get("evidence", [])
        if not evidence:
            continue

        first = evidence[0]
        lines.append(f"- {first['quote']}")
        lines.append(f"  Source: `{first['source_ref']}`")

        for extra in evidence[1:]:
            lines.append(f"  - {extra['quote']}")
            lines.append(f"    Source: `{extra['source_ref']}`")

    lines.append("")
    return "\n".join(lines)


def write_daily_summary(result: dict) -> tuple[Path, Path]:
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

    day_token = result["date"]
    json_path = SUMMARIES_DIR / f"{day_token}.json"
    markdown_path = SUMMARIES_DIR / f"{day_token}.md"

    json_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(result), encoding="utf-8")

    return json_path, markdown_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an evidence-backed Omi daily summary."
    )
    parser.add_argument(
        "date",
        nargs="?",
        help="Date to summarise as YYYY-MM-DD. Defaults to yesterday.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write JSON and Markdown files to OMI_SUMMARIES_DIR.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.date:
        target_date = date.fromisoformat(args.date)
    else:
        target_date = date.today() - timedelta(days=1)

    result = generate_daily_summary(target_date)

    if args.write:
        json_path, markdown_path = write_daily_summary(result)
        print(f"json={json_path}")
        print(f"markdown={markdown_path}")
        return

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
