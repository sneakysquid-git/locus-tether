"""
Central config for the Omi -> Jetson Thor local pipeline.
Override any of these via environment variables of the same name.
"""
import os
from pathlib import Path

# --- Directories -------------------------------------------------------
# INBOX_DIR: where Syncthing drops new audio files synced from the phone.
# PROCESSING_DIR: files move here while being transcribed (avoids double-processing
#                 if the watcher restarts mid-job).
# ARCHIVE_DIR: original audio moves here once transcribed, kept for reference.
# TRANSCRIPTS_DIR: where .txt and .json transcript output is written.
# FAILED_DIR: anything that errors out during transcription lands here for inspection.
BASE_DIR = Path(os.environ.get("OMI_PIPELINE_BASE", str(Path.home() / "omi-pipeline")))

INBOX_DIR = Path(os.environ.get("OMI_INBOX_DIR", str(BASE_DIR / "inbox")))
PROCESSING_DIR = Path(os.environ.get("OMI_PROCESSING_DIR", str(BASE_DIR / "processing")))
ARCHIVE_DIR = Path(os.environ.get("OMI_ARCHIVE_DIR", str(BASE_DIR / "archive")))
TRANSCRIPTS_DIR = Path(os.environ.get("OMI_TRANSCRIPTS_DIR", str(BASE_DIR / "transcripts")))
FAILED_DIR = Path(os.environ.get("OMI_FAILED_DIR", str(BASE_DIR / "failed")))

# --- File stability detection -------------------------------------------
# Syncthing writes files incrementally (and uses .syncthing.<name>.tmp during
# transfer), so we don't want to grab a file the instant it appears. We poll
# its size every STABILITY_POLL_SECONDS and only treat it as "done" once the
# size hasn't changed for STABILITY_REQUIRED_SECONDS.
STABILITY_POLL_SECONDS = float(os.environ.get("OMI_STABILITY_POLL_SECONDS", "3"))
STABILITY_REQUIRED_SECONDS = float(os.environ.get("OMI_STABILITY_REQUIRED_SECONDS", "15"))

# File extensions we'll attempt to process. Syncthing temp files (.tmp, starting
# with '.syncthing.') are always ignored regardless of this list.
AUDIO_EXTENSIONS = {".wav", ".opus", ".m4a", ".mp3", ".flac", ".ogg", ".aac"}

# --- Whisper (faster-whisper / CTranslate2) -----------------------------
WHISPER_MODEL_SIZE = os.environ.get("OMI_WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.environ.get("OMI_WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE_TYPE = os.environ.get("OMI_WHISPER_COMPUTE_TYPE", "float16")
WHISPER_LANGUAGE = os.environ.get("OMI_WHISPER_LANGUAGE")  # None = auto-detect
WHISPER_VAD_FILTER = os.environ.get("OMI_WHISPER_VAD_FILTER", "true").lower() == "true"

# --- Logging -------------------------------------------------------------
LOG_LEVEL = os.environ.get("OMI_LOG_LEVEL", "INFO")
LOG_FILE = Path(os.environ.get("OMI_LOG_FILE", str(BASE_DIR / "pipeline.log")))


def ensure_dirs() -> None:
    for d in (INBOX_DIR, PROCESSING_DIR, ARCHIVE_DIR, TRANSCRIPTS_DIR, FAILED_DIR):
        d.mkdir(parents=True, exist_ok=True)
