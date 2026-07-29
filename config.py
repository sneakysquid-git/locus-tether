"""
Central config for the Omi -> Jetson Thor local pipeline.
Override any of these via environment variables of the same name.
"""
import os
from pathlib import Path


def _load_dotenv_if_present(path: Path) -> None:
    """
    Tiny, dependency-free .env-style loader. Only sets a variable if it isn't
    already present in the environment — so a real env var (e.g. set by
    systemd, or exported in your shell) always takes precedence over this
    file. Silently does nothing if the file doesn't exist.

    Used for .env.digest specifically: SMTP credentials should never be
    committed to git, and this lets the same untracked local file work
    whether digest.py is run manually, via systemd, or via cron, without
    needing separate wiring for each.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv_if_present(Path(__file__).resolve().parent / ".env.digest")

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

# --- Ollama (Phase 4: transcript -> structured analysis) -----------------
# Ollama runs natively on the Thor host (not containerized), so the pipeline
# container reaches it via the host's network — see docker-compose.yml's
# network_mode: host, which is what makes "localhost" here actually resolve
# to the host's Ollama server rather than the container's own loopback.
OLLAMA_HOST = os.environ.get("OMI_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OMI_OLLAMA_MODEL", "llama3.1:8b")
# If analysis fails (Ollama down, bad JSON, etc.), should the pipeline still
# keep the transcript? Yes, always — a transcription success is valuable on
# its own; a failed analysis stage is a soft failure, not a reason to lose
# the transcript or route the audio to FAILED_DIR.

# --- Logging -------------------------------------------------------------
LOG_LEVEL = os.environ.get("OMI_LOG_LEVEL", "INFO")
LOG_FILE = Path(os.environ.get("OMI_LOG_FILE", str(BASE_DIR / "pipeline.log")))

# --- Digests (Phase 5) ---------------------------------------------------
DIGESTS_DIR = Path(os.environ.get("OMI_DIGESTS_DIR", str(BASE_DIR / "digests")))

# --- Digest email delivery (optional) -------------------------------------
# All of these come from .env.digest (see .env.digest.example), which is
# git-ignored — never commit real SMTP credentials. If DIGEST_EMAIL_ENABLED
# isn't set to "true", digest.py just writes the markdown file as before and
# skips email entirely; nothing here is required to use the digest at all.
DIGEST_EMAIL_ENABLED = os.environ.get("OMI_DIGEST_EMAIL_ENABLED", "false").lower() == "true"
DIGEST_SMTP_HOST = os.environ.get("OMI_DIGEST_SMTP_HOST", "")
DIGEST_SMTP_PORT = int(os.environ.get("OMI_DIGEST_SMTP_PORT", "587"))
DIGEST_SMTP_USER = os.environ.get("OMI_DIGEST_SMTP_USER", "")
DIGEST_SMTP_PASSWORD = os.environ.get("OMI_DIGEST_SMTP_PASSWORD", "")
DIGEST_EMAIL_FROM = os.environ.get("OMI_DIGEST_EMAIL_FROM", "")
DIGEST_EMAIL_TO = os.environ.get("OMI_DIGEST_EMAIL_TO", "")


def ensure_dirs() -> None:
    for d in (INBOX_DIR, PROCESSING_DIR, ARCHIVE_DIR, TRANSCRIPTS_DIR, FAILED_DIR, DIGESTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
