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
CLASSIFY_MAX_CHARS = int(os.environ.get("OMI_CLASSIFY_MAX_CHARS", "24000"))
MAX_EVIDENCE_SEGMENT_SPAN = int(
    os.environ.get("OMI_EVIDENCE_MAX_SEGMENT_SPAN", "12")
)
MAX_SELECTED_POINTS = int(os.environ.get("OMI_SUMMARY_MAX_POINTS", "12"))

_FILENAME_RE = re.compile(r"^omi-(\d{8})_(\d{6})\.json$")
_REF_RE = re.compile(r"^(omi-\d{8}_\d{6}):(\d+):(-?\d+(?:\.\d+)?)$")

CATEGORIES = (
    "action_or_task",
    "decision_or_commitment",
    "work_activity",
    "important_conversation",
    "personal_planning",
    "practical_life",
    "leisure_conversation",
    "ambient_media",
    "noise_or_fragment",
)

ACTIVITY_MODES = (
    "active_or_interactive",
    "passive_media",
    "unclear",
)

CATEGORY_BASE_SCORE = {
    "decision_or_commitment": 90,
    "action_or_task": 85,
    "work_activity": 80,
    "important_conversation": 75,
    "personal_planning": 70,
    "practical_life": 60,
    "leisure_conversation": 25,
    "ambient_media": -1000,
    "noise_or_fragment": -1000,
}

ACTIVITY_MODE_SCORE = {
    "active_or_interactive": 15,
    "unclear": 0,
    "passive_media": -1000,
}


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


CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point_id": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": list(CATEGORIES),
                    },
                    "activity_mode": {
                        "type": "string",
                        "enum": list(ACTIVITY_MODES),
                    },
                    "relevance": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3,
                    },
                },
                "required": [
                    "point_id",
                    "category",
                    "activity_mode",
                    "relevance",
                ],
            },
        },
    },
    "required": ["classifications"],
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
        line = json.dumps(
            {"source_ref": segment.ref, "text": segment.text},
            ensure_ascii=False,
        )
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
    if parsed is not None:
        recording_stem, _ = parsed
        matches = [
            segment.ref
            for segment in segment_index.values()
            if segment.ref.startswith(f"{recording_stem}:")
            and quote in segment.text
        ]

        if len(matches) == 1:
            return matches[0]

    # Some local models occasionally put the quote itself in source_ref.
    # Repair only when the exact quote identifies exactly one transcript
    # segment in the whole day. Ambiguous matches are rejected.
    exact_matches = [
        segment.ref
        for segment in segment_index.values()
        if quote == segment.text
    ]

    if len(exact_matches) == 1:
        return exact_matches[0]

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

Extract only coherent events, activities, discussions, decisions, commitments,
plans, or practical matters that could potentially be useful in a daily
activity summary.

Each transcript line is a JSON object with separate "source_ref" and "text"
fields. Copy those values into evidence; do not swap them.

The "text" field in each key point is only an internal scratch label to help
you keep separate events separate. It will not be trusted or stored as factual
output.

STRICT RULES:
- Every key point must represent one coherent event or topic.
- Evidence for one key point should normally come from the same recording and nearby segments.
- A key point may use multiple adjacent transcript segments when needed.
- evidence must be an array of source references and exact verbatim quotes.
- Copy source_ref exactly from the transcript object's "source_ref" field.
- Copy quote exactly from that transcript object's "text" field.
- Never merge unrelated events into one key point.
- Do not invent names, relationships, motives, locations, dates, or conclusions.
- Do not decide whether something is passive media here; later processing will classify it.
- Ignore obvious filler and fragments unless they are needed to understand a useful event.
- Prefer a small number of coherent points over exhaustive transcription.
- If nothing potentially useful is present, return an empty key_points array.

TRANSCRIPT JSON LINES:
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

            resolved_ref = resolve_evidence_ref(
                requested_ref,
                quote,
                segment_index,
            )
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
    seen_evidence: set[tuple[tuple[str, str], ...]] = set()
    seen_ref_sets: set[tuple[str, ...]] = set()

    for item in sorted(key_points, key=key_point_sort_key):
        evidence_identity = tuple(
            (evidence["source_ref"], evidence["quote"])
            for evidence in item["evidence"]
        )
        ref_identity = tuple(
            evidence["source_ref"]
            for evidence in item["evidence"]
        )

        if evidence_identity in seen_evidence or ref_identity in seen_ref_sets:
            continue

        seen_evidence.add(evidence_identity)
        seen_ref_sets.add(ref_identity)
        deduplicated.append(item)

    return deduplicated


def make_point_records(key_points: list[dict]) -> list[dict]:
    return [
        {
            "id": f"P{index:04d}",
            "evidence": point["evidence"],
        }
        for index, point in enumerate(key_points, 1)
    ]


def point_for_classification(point: dict) -> dict:
    return {
        "point_id": point["id"],
        "evidence": [
            {
                "source_ref": item["source_ref"],
                "quote": item["quote"],
            }
            for item in point["evidence"]
        ],
    }


def chunk_points_for_classification(
    points: list[dict],
    max_chars: int = CLASSIFY_MAX_CHARS,
) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_size = 0

    for point in points:
        compact = point_for_classification(point)
        encoded = json.dumps(compact, ensure_ascii=False)
        size = len(encoded) + 1

        if current and current_size + size > max_chars:
            chunks.append(current)
            current = []
            current_size = 0

        current.append(compact)
        current_size += size

    if current:
        chunks.append(current)

    return chunks


def classify_point_chunk(points: list[dict]) -> dict:
    payload = "\n".join(
        json.dumps(point, ensure_ascii=False)
        for point in points
    )

    prompt = f"""Classify each validated evidence group by how useful it is for
summarising the wearer's actual day.

This is a wearable microphone transcript. It can capture:
- things the wearer actively did or discussed;
- conversations around the wearer;
- television, YouTube, podcasts, music commentary, games, or other passive media;
- random fragments and background speech.

CATEGORIES:
- action_or_task: an explicit task, instruction, follow-up, or thing to do.
- decision_or_commitment: a clear decision, promise, agreement, or commitment.
- work_activity: active work, troubleshooting, building, configuring, meetings, or business activity.
- important_conversation: a substantive interpersonal discussion worth remembering.
- personal_planning: plans, arrangements, travel, events, or preparation.
- practical_life: useful household, family, shopping, clothing, transport, or day-to-day logistics.
- leisure_conversation: genuine interactive discussion about hobbies, entertainment, music, games, etc.
- ambient_media: passive recorded media or a one-way review/podcast/video/TV-like monologue that is not evidence of what the wearer did.
- noise_or_fragment: too fragmentary, trivial, or contextless to be useful.

ACTIVITY MODE:
- active_or_interactive: evidence looks like the wearer was doing something or participating in an interaction.
- passive_media: evidence looks primarily like captured media, playback, broadcast, review, podcast, video, TV, or other passive content.
- unclear: there is not enough evidence to tell.

RELEVANCE:
- 3: materially useful for recalling the day or future action.
- 2: useful supporting detail.
- 1: minor but potentially relevant.
- 0: not useful for a daily activity summary.

STRICT RULES:
- Return one classification for every point_id supplied.
- Do not create new point IDs.
- Judge only from the evidence shown.
- Passive media must be activity_mode=passive_media and normally category=ambient_media.
- A long fluent monologue about a product, game, film, music, news, or technical topic is likely ambient_media unless the evidence clearly shows an interactive conversation or active work.
- Explicit tasks, commitments, plans, purchases, troubleshooting, work progress, and meaningful interpersonal discussions should rank above general chatter.
- Do not turn an interesting topic into a high-relevance event merely because the content itself is interesting.
- Do not infer hidden context.

VALIDATED EVIDENCE GROUPS:
{payload}
"""

    return call_ollama(prompt, CLASSIFICATION_SCHEMA)


def validate_classifications(
    result: dict,
    points: list[dict],
) -> dict[str, dict]:
    valid_ids = {point["id"] for point in points}
    validated: dict[str, dict] = {}

    raw = result.get("classifications", [])
    if not isinstance(raw, list):
        return validated

    for item in raw:
        if not isinstance(item, dict):
            continue

        point_id = str(item.get("point_id", "")).strip()
        category = str(item.get("category", "")).strip()
        activity_mode = str(item.get("activity_mode", "")).strip()

        try:
            relevance = int(item.get("relevance", -1))
        except (TypeError, ValueError):
            continue

        if point_id not in valid_ids:
            continue
        if category not in CATEGORIES:
            continue
        if activity_mode not in ACTIVITY_MODES:
            continue
        if relevance < 0 or relevance > 3:
            continue

        validated[point_id] = {
            "category": category,
            "activity_mode": activity_mode,
            "relevance": relevance,
        }

    return validated


def classify_points(points: list[dict]) -> dict[str, dict]:
    classifications: dict[str, dict] = {}

    for point_chunk in chunk_points_for_classification(points):
        raw = classify_point_chunk(point_chunk)
        chunk_ids = {item["point_id"] for item in point_chunk}
        expected_points = [
            point
            for point in points
            if point["id"] in chunk_ids
        ]
        classifications.update(
            validate_classifications(raw, expected_points)
        )

    return classifications


def classification_score(classification: dict) -> int:
    category = classification["category"]
    activity_mode = classification["activity_mode"]
    relevance = classification["relevance"]

    return (
        CATEGORY_BASE_SCORE.get(category, -1000)
        + ACTIVITY_MODE_SCORE.get(activity_mode, -1000)
        + (relevance * 10)
    )


def point_ref_set(point: dict) -> set[str]:
    return {
        evidence["source_ref"]
        for evidence in point["evidence"]
    }


def points_overlap(first: dict, second: dict) -> bool:
    first_refs = point_ref_set(first)
    second_refs = point_ref_set(second)

    if not first_refs or not second_refs:
        return False

    intersection = len(first_refs & second_refs)
    smaller = min(len(first_refs), len(second_refs))

    return intersection / smaller >= 0.6


def should_select(classification: dict) -> bool:
    if classification["activity_mode"] == "passive_media":
        return False

    if classification["category"] in {"ambient_media", "noise_or_fragment"}:
        return False

    if classification["relevance"] <= 0:
        return False

    if classification["category"] == "leisure_conversation":
        return classification["relevance"] >= 3

    if classification["category"] in {
        "action_or_task",
        "decision_or_commitment",
    }:
        return classification["relevance"] >= 1

    return classification["relevance"] >= 2


def select_daily_points(
    points: list[dict],
    classifications: dict[str, dict],
) -> list[dict]:
    candidates: list[dict] = []

    for point in points:
        classification = classifications.get(point["id"])
        if classification is None:
            continue

        if not should_select(classification):
            continue

        candidates.append(
            {
                **point,
                **classification,
                "score": classification_score(classification),
            }
        )

    candidates.sort(
        key=lambda item: (
            -item["score"],
            key_point_sort_key(item),
        )
    )

    selected: list[dict] = []

    for candidate in candidates:
        if any(points_overlap(candidate, existing) for existing in selected):
            continue

        selected.append(candidate)

        if len(selected) >= MAX_SELECTED_POINTS:
            break

    selected.sort(key=key_point_sort_key)

    for item in selected:
        item.pop("score", None)

    return selected


def generate_daily_summary(target_date: date) -> dict:
    segments = load_day_segments(target_date)

    if not segments:
        return {
            "date": target_date.isoformat(),
            "model": OLLAMA_MODEL,
            "recordings": 0,
            "segments": 0,
            "chunks": 0,
            "classification_chunks": 0,
            "selected_points": [],
            "evidence_points": [],
            "classified_points": [],
        }

    segment_index = build_segment_index(segments)
    chunks = chunk_segments(segments)
    extracted_points: list[dict] = []

    for chunk in chunks:
        raw_result = extract_chunk_summary(chunk)
        extracted_points.extend(
            validate_key_points(raw_result, segment_index)
        )

    key_points = deduplicate_key_points(extracted_points)
    point_records = make_point_records(key_points)
    classification_chunks = chunk_points_for_classification(point_records)
    classifications = classify_points(point_records)
    selected_points = select_daily_points(point_records, classifications)

    classified_points = [
        {
            **point,
            **classifications[point["id"]],
        }
        for point in point_records
        if point["id"] in classifications
    ]

    recordings = len({segment.source_file for segment in segments})

    return {
        "date": target_date.isoformat(),
        "model": OLLAMA_MODEL,
        "recordings": recordings,
        "segments": len(segments),
        "chunks": len(chunks),
        "classification_chunks": len(classification_chunks),
        "selected_points": selected_points,
        "evidence_points": point_records,
        "classified_points": classified_points,
    }


def category_heading(category: str) -> str:
    return {
        "action_or_task": "Tasks and follow-ups",
        "decision_or_commitment": "Decisions and commitments",
        "work_activity": "Work activity",
        "important_conversation": "Important conversations",
        "personal_planning": "Plans",
        "practical_life": "Practical",
        "leisure_conversation": "Leisure",
    }.get(category, category.replace("_", " ").title())


def render_markdown(result: dict) -> str:
    lines = [
        f"# Omi Daily Summary - {result['date']}",
        "",
        (
            f"{result['recordings']} recordings | "
            f"{result['segments']} transcript segments | "
            f"{result['chunks']} extraction chunks"
        ),
        "",
    ]

    selected_points = result.get("selected_points", [])

    if not selected_points:
        lines.append("No meaningful activity was identified.")
        lines.append("")
        return "\n".join(lines)

    grouped: dict[str, list[dict]] = {}

    for point in selected_points:
        grouped.setdefault(point["category"], []).append(point)

    category_order = [
        "action_or_task",
        "decision_or_commitment",
        "work_activity",
        "important_conversation",
        "personal_planning",
        "practical_life",
        "leisure_conversation",
    ]

    for category in category_order:
        points = grouped.get(category, [])
        if not points:
            continue

        lines.append(f"## {category_heading(category)}")
        lines.append("")

        for point in points:
            evidence = point.get("evidence", [])
            if not evidence:
                continue

            lines.append(f"- {evidence[0]['quote']}")
            lines.append(
                f"  Source: `{evidence[0]['source_ref']}` "
                f"(relevance {point['relevance']}/3)"
            )

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
    markdown_path.write_text(
        render_markdown(result),
        encoding="utf-8",
    )

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
