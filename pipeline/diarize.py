"""
Phase 1 speaker diarization: labels which of N distinct speakers said each
segment of a transcript, using WhisperX's alignment + pyannote.audio's
diarization — layered on TOP of our own existing faster-whisper/CTranslate2
transcription (transcribe.py), not replacing it.

Adapted from murtaza-nasir/whisperx-asr-service's pipeline.py (MIT licensed):
https://github.com/murtaza-nasir/whisperx-asr-service
That project's clean 3-stage (transcribe/align/diarize) design and model-
caching/eviction pattern is what this is based on — we only needed the
align+diarize stages, since our own transcribe.py already handles
transcription with a working from-source CTranslate2 build for this
hardware.

Important dependency note: unlike our CTranslate2-based transcription
(deliberately chosen to avoid a PyTorch dependency), alignment (wav2vec2)
and diarization (pyannote.audio) are both PyTorch-native models. Confirmed
PyTorch's official wheels install cleanly and detect CUDA correctly on this
hardware (Jetson Thor, JetPack 7.2) — a new dependency, but not a new
from-source build problem like CTranslate2 was.

Deliberately optional and soft-failing:
- ENABLE_DIARIZATION=false skips this whole module's work — transcription
  and analysis both work completely fine without it. Matters most for
  people on smaller GPUs where the added ~4GB isn't available (see
  HARDWARE.md).
- Any stage failing (missing HF token, model download hiccup, OOM) logs a
  warning and returns the transcript unchanged for that stage, rather than
  losing the underlying transcript — same "soft failure" philosophy used
  throughout the rest of this pipeline.

Idle model eviction (MODEL_KEEP_ALIVE_SECONDS) matters here specifically
because these models would otherwise stay resident in memory indefinitely
after the first recording of the day, competing with other GPU workloads
(robotics, etc.) at rest between sporadic recordings.
"""
import gc
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import config

log = logging.getLogger("omi.diarize")

DEVICE = config.DIARIZATION_DEVICE
HF_TOKEN = config.HF_TOKEN
CACHE_DIR = config.DIARIZATION_CACHE_DIR
ENABLE_DIARIZATION = config.DIARIZATION_ENABLED

# Idle model eviction — see module docstring for why this matters here.
MODEL_KEEP_ALIVE_SECONDS = config.MODEL_KEEP_ALIVE_SECONDS
MODEL_EVICTION_INTERVAL_SECONDS = config.MODEL_EVICTION_INTERVAL_SECONDS

_model_load_lock = threading.Lock()
_align_models: Dict[str, Any] = {}
_align_models_last_used: Dict[str, float] = {}
_diarize_pipeline = None
_diarize_pipeline_last_used: float = 0.0
_eviction_thread_started = False
_eviction_thread_lock = threading.Lock()


def _clear_gpu_memory() -> None:
    import torch

    if DEVICE == "cuda":
        gc.collect()
        torch.cuda.empty_cache()


def _ensure_eviction_thread() -> None:
    global _eviction_thread_started
    if MODEL_KEEP_ALIVE_SECONDS <= 0 or _eviction_thread_started:
        return
    with _eviction_thread_lock:
        if _eviction_thread_started:
            return
        t = threading.Thread(target=_eviction_loop, daemon=True, name="diarize-model-evictor")
        t.start()
        _eviction_thread_started = True
        log.info(
            "Idle model eviction enabled: unload after %ds idle, sweep every %ds",
            MODEL_KEEP_ALIVE_SECONDS,
            MODEL_EVICTION_INTERVAL_SECONDS,
        )


def _eviction_loop() -> None:
    global _diarize_pipeline, _diarize_pipeline_last_used
    while True:
        time.sleep(MODEL_EVICTION_INTERVAL_SECONDS)
        now = time.time()
        evicted_any = False

        with _model_load_lock:
            for lang in list(_align_models.keys()):
                if now - _align_models_last_used.get(lang, 0) > MODEL_KEEP_ALIVE_SECONDS:
                    log.info("Evicting idle alignment model: %s", lang)
                    del _align_models[lang]
                    _align_models_last_used.pop(lang, None)
                    evicted_any = True

            if _diarize_pipeline is not None and now - _diarize_pipeline_last_used > MODEL_KEEP_ALIVE_SECONDS:
                log.info("Evicting idle diarization pipeline")
                _diarize_pipeline = None
                evicted_any = True

        if evicted_any:
            _clear_gpu_memory()


def _load_align_model(language_code: str):
    import whisperx

    if language_code not in _align_models:
        with _model_load_lock:
            if language_code not in _align_models:
                log.info("Loading alignment model for language: %s", language_code)
                model_a, metadata = whisperx.load_align_model(
                    language_code=language_code, device=DEVICE, model_dir=CACHE_DIR
                )
                _align_models[language_code] = (model_a, metadata)
    _align_models_last_used[language_code] = time.time()
    _ensure_eviction_thread()
    return _align_models[language_code]


def _load_diarize_pipeline():
    import torch
    from whisperx.diarize import DiarizationPipeline

    global _diarize_pipeline, _diarize_pipeline_last_used
    if _diarize_pipeline is None:
        with _model_load_lock:
            if _diarize_pipeline is None:
                log.info("Loading diarization pipeline: pyannote/speaker-diarization-community-1")
                _diarize_pipeline = DiarizationPipeline(
                    model_name="pyannote/speaker-diarization-community-1",
                    use_auth_token=HF_TOKEN,
                    device=torch.device(DEVICE),
                )
    _diarize_pipeline_last_used = time.time()
    _ensure_eviction_thread()
    return _diarize_pipeline


def align_and_diarize(audio_path: Path, result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adds word-level alignment and speaker labels to a transcript result
    already produced by our own transcribe.py. `result` must have
    "segments" (list of {start, end, text, ...}) and "language" — the same
    shape transcribe_file() already returns.

    Returns a new segments list in OUR schema (start/end/text/speaker) —
    the caller doesn't need to know anything about WhisperX's internal
    result shape. Never raises: if diarization is disabled, misconfigured,
    or any stage fails, returns `result` with segments unchanged (speaker
    stays None) rather than losing the underlying transcript.
    """
    if not ENABLE_DIARIZATION:
        log.info("Diarization disabled (ENABLE_DIARIZATION=false), skipping")
        return result

    if not HF_TOKEN:
        log.warning("ENABLE_DIARIZATION=true but HF_TOKEN not set — skipping diarization")
        return result

    try:
        import whisperx
    except ImportError:
        log.warning("whisperx not installed — skipping diarization")
        return result

    audio = whisperx.load_audio(str(audio_path))
    language = result.get("language", "en")
    working = {"segments": result["segments"], "language": language}

    # Stage: alignment (word-level timestamps) — improves diarization
    # accuracy, since speaker assignment works at the word level when
    # alignment succeeded, not just the coarser whole-segment level.
    try:
        model_a, metadata = _load_align_model(language)
        working = whisperx.align(
            working["segments"], model_a, metadata, audio, DEVICE, return_char_alignments=False
        )
        log.info("Alignment complete")
    except Exception as e:
        log.warning("Alignment failed: %s, continuing without word-level timestamps", e)
    finally:
        _clear_gpu_memory()

    # Stage: diarization (who spoke each segment)
    try:
        diarize_model = _load_diarize_pipeline()
        diarize_segments = diarize_model(audio)
        if hasattr(diarize_segments, "exclusive_speaker_diarization"):
            diarize_segments = diarize_segments.exclusive_speaker_diarization
        working = whisperx.assign_word_speakers(diarize_segments, working)
        log.info("Diarization complete")
    except Exception as e:
        log.warning("Diarization failed: %s, continuing without speaker labels", e)
        return result  # nothing to merge back — original transcript untouched
    finally:
        _clear_gpu_memory()

    # Reshape WhisperX's segment format back into our own stable schema
    # (start/end/text/speaker) — downstream code never needs to know
    # anything changed under the hood.
    new_segments = []
    for seg in working.get("segments", []):
        new_segments.append(
            {
                "start": round(seg.get("start", 0.0), 2),
                "end": round(seg.get("end", 0.0), 2),
                "text": seg.get("text", "").strip(),
                "speaker": seg.get("speaker"),
            }
        )
    result["segments"] = new_segments
    return result
