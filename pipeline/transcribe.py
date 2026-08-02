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
    the same as result["text"] unchanged.

    Deliberately keeps the original flowing prose (result["text"]) as the
    actual body, rather than reformatting into per-line "SPEAKER_00: ...
    SPEAKER_01: ..." dialogue — real-hardware testing showed that full
    reformatting caused two regressions: key_points extraction quality
    dropped noticeably, and the model started hallucinating placeholder
    "participants" entries (null name, matching the speaker count) that
    the prompt explicitly says not to produce. The flowing-prose version
    consistently extracted richer, better content in direct comparison.

    Instead, just prepends a short, minimal context note on how many
    distinct speakers were detected when there's more than one — enough
    for the model to correctly frame the overview as a multi-person
    conversation ("the group discussed...") without restructuring the
    whole input into a format that seems to distract it from actually
    extracting content.

    Falls back to the flat text unchanged when no segment has a real
    speaker label (diarization disabled, or failed and fell back softly).
    """
    segments = result.get("segments", [])
    speakers = {seg.get("speaker") for seg in segments if seg.get("speaker")}
    text = result.get("text", "")

    if len(speakers) > 1:
        return f"[This conversation had {len(speakers)} distinct speakers.]\n\n{text}"
    return text


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
