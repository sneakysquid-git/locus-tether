"""
Thin wrapper around faster-whisper. Loads the model once (expensive) and
exposes a single transcribe_file() call used by the watcher.

Diarization (Phase 1, see diarize.py) runs as an additional step after
transcription, populating the "speaker" field that used to always be None
— kept as a fully separate module/optional step so this file's own
CTranslate2-based transcription stays untouched regardless of whether
diarization is enabled, working, or even installed.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict

import config
import diarize

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
      - segments: list of {start, end, text, speaker}
      - language: detected/used language code
      - duration: audio duration in seconds
    Raises whatever faster-whisper raises on decode/inference failure — the
    caller (watcher) is responsible for catching and routing to FAILED_DIR.

    Speaker labels come from diarize.py's align_and_diarize() (Phase 1),
    run after transcription completes — a soft-failing, optional step (see
    that module's docstring), so segments["speaker"] stays None if
    diarization is disabled, unconfigured, or fails for any reason.
    """
    model = get_model()

    segments_iter, info = model.transcribe(
        str(audio_path),
        language=config.WHISPER_LANGUAGE,
        vad_filter=config.WHISPER_VAD_FILTER,
        condition_on_previous_text=config.WHISPER_CONDITION_ON_PREVIOUS_TEXT,
    )

    segments = []
    full_text_parts = []
    for seg in segments_iter:
        segments.append(
            {
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
                "speaker": None,  # populated below by diarize.align_and_diarize, if enabled
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

    result = diarize.align_and_diarize(audio_path, result)

    return result


def build_analysis_text(result: Dict[str, Any]) -> str:
    """
    Builds the text actually sent to the LLM for analysis — NOT necessarily
    the same as result["text"]. If diarization succeeded (segments have
    real speaker labels), formats as "SPEAKER_00: ...\\nSPEAKER_01: ..."
    with consecutive same-speaker segments merged into one turn. This is
    what lets the model actually recognize a multi-person conversation and
    write about it that way (multiple participants, a group discussion)
    instead of always defaulting to "the speaker" as if it were a single
    person talking, regardless of how many distinct voices were actually
    detected — which is exactly what it did before this existed, since it
    never had any way to know otherwise.

    Falls back to the flat, speaker-agnostic result["text"] when no segment
    has a real speaker label (diarization disabled, or failed and fell back
    softly) — same behavior as before this function existed.
    """
    segments = result.get("segments", [])
    if not any(seg.get("speaker") for seg in segments):
        return result.get("text", "")

    lines = []
    current_speaker = None
    current_texts: list[str] = []
    for seg in segments:
        speaker = seg.get("speaker") or "UNKNOWN"
        if speaker != current_speaker:
            if current_texts:
                lines.append(f"{current_speaker}: {' '.join(current_texts)}")
            current_speaker = speaker
            current_texts = [seg["text"]]
        else:
            current_texts.append(seg["text"])
    if current_texts:
        lines.append(f"{current_speaker}: {' '.join(current_texts)}")
    return "\n".join(lines)


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
