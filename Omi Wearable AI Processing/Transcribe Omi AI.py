"""
Thin wrapper around faster-whisper. Loads the model once (expensive) and
exposes a single transcribe_file() call used by the watcher.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict

import config

log = logging.getLogger("omi.transcribe")

_model = None  # lazy-loaded singleton


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        log.info(
            "Loading faster-whisper model=%s device=%s compute_type=%s (first call, this can take a bit)",
            config.WHISPER_MODEL_SIZE,
            config.WHISPER_DEVICE,
            config.WHISPER_COMPUTE_TYPE,
        )
        _model = WhisperModel(
            config.WHISPER_MODEL_SIZE,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
        )
        log.info("Model loaded.")
    return _model


def transcribe_file(audio_path: Path) -> Dict[str, Any]:
    """
    Runs faster-whisper on audio_path and returns a dict with:
      - text: full transcript as a single string
      - segments: list of {start, end, text, speaker (always None here — no diarization yet)}
      - language: detected/used language code
      - duration: audio duration in seconds
    Raises whatever faster-whisper raises on decode/inference failure — the
    caller (watcher) is responsible for catching and routing to FAILED_DIR.
    """
    model = get_model()

    segments_iter, info = model.transcribe(
        str(audio_path),
        language=config.WHISPER_LANGUAGE,
        vad_filter=config.WHISPER_VAD_FILTER,
    )

    segments = []
    full_text_parts = []
    for seg in segments_iter:
        segments.append(
            {
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
                "speaker": None,  # placeholder — add pyannote diarization later if wanted
            }
        )
        full_text_parts.append(seg.text.strip())

    result = {
        "source_file": audio_path.name,
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "duration": round(info.duration, 2),
        "text": " ".join(full_text_parts).strip(),
        "segments": segments,
    }
    return result


def write_transcript(result: Dict[str, Any], stem: str) -> None:
    """Writes both a human-readable .txt and a structured .json transcript."""
    json_path = config.TRANSCRIPTS_DIR / f"{stem}.json"
    txt_path = config.TRANSCRIPTS_DIR / f"{stem}.txt"

    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [f"# Transcript: {result['source_file']}"]
    lines.append(f"# Duration: {result['duration']}s | Language: {result['language']}")
    lines.append("")
    for seg in result["segments"]:
        lines.append(f"[{seg['start']:>7.2f} - {seg['end']:>7.2f}] {seg['text']}")
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    log.info("Wrote transcript: %s / %s", json_path.name, txt_path.name)
