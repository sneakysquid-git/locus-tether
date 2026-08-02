"""
Folder-watcher daemon for the Omi -> Jetson Thor pipeline.

Flow per file:
  1. watchdog notices a new/modified file in INBOX_DIR.
  2. We wait until its size has stopped changing (handles Syncthing writing
     incrementally, and its .syncthing.*.tmp / partial-file conventions).
  3. Move it into PROCESSING_DIR (so a crash mid-transcription doesn't leave
     it sitting in INBOX_DIR to be silently skipped or double-queued).
  4. Transcribe with faster-whisper.
  5. On success: write transcript, run Phase 4 analysis (Ollama), move audio
     to ARCHIVE_DIR. Analysis failure is a soft failure — logged, but the
     transcript is kept and the file still archives normally, since a
     successful transcription is valuable on its own even if the LLM
     analysis step has trouble (Ollama down, etc.).
     On transcription failure: move audio to FAILED_DIR, log the exception,
     keep going.

Also runs a second, independent watcher on ENROLLMENT_DIR (#37) — voice
enrollment samples webapp.py hands off here get an embedding extracted and
stored via speaker_profiles.py. Same file-stability-then-process pattern,
just a separate directory/handler running in parallel.

Run with:
    python watcher.py
or install as a systemd service (see omi-watcher.service in this folder).
"""
import json
import logging
import queue
import shutil
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import analyzer
import config
import diarize
import speaker_profiles
import transcribe

log = logging.getLogger("omi.watcher")


def is_candidate_file(path: Path) -> bool:
    if not path.is_file():
        return False
    name = path.name
    # Syncthing temp/partial file conventions — never touch these.
    if name.startswith(".syncthing.") or name.endswith(".tmp"):
        return False
    if name.startswith("."):
        return False
    return path.suffix.lower() in config.AUDIO_EXTENSIONS


class NewFileHandler(FileSystemEventHandler):
    """Just funnels filesystem events into a queue — no work happens here.
    Keeping the watchdog callback fast/non-blocking avoids missing events."""

    def __init__(self, pending_q: "queue.Queue[Path]"):
        self.pending_q = pending_q

    def on_created(self, event):
        if not event.is_directory:
            self._maybe_enqueue(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self._maybe_enqueue(Path(event.dest_path))

    def _maybe_enqueue(self, path: Path):
        if is_candidate_file(path):
            log.debug("Detected candidate file: %s", path)
            self.pending_q.put(path)


def wait_until_stable(path: Path) -> bool:
    """
    Polls file size until it stops changing for STABILITY_REQUIRED_SECONDS.
    Returns True if the file stabilized, False if it disappeared (e.g. Syncthing
    renamed/replaced it mid-check — just skip it, it'll get re-queued when the
    final version lands).
    """
    stable_since = None
    last_size = -1

    while True:
        if not path.exists():
            log.warning("File disappeared while waiting for stability: %s", path)
            return False

        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False

        now = time.monotonic()
        if size == last_size:
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= config.STABILITY_REQUIRED_SECONDS:
                return True
        else:
            stable_since = None
            last_size = size

        time.sleep(config.STABILITY_POLL_SECONDS)


def process_one(path: Path) -> None:
    if not path.exists():
        return  # got processed or removed already

    log.info("Waiting for file to stabilize: %s", path.name)
    if not wait_until_stable(path):
        return

    processing_path = config.PROCESSING_DIR / path.name
    try:
        shutil.move(str(path), str(processing_path))
    except FileNotFoundError:
        log.warning("File vanished before move: %s", path.name)
        return

    log.info("Transcribing: %s", processing_path.name)
    try:
        result = transcribe.transcribe_file(processing_path)
        stem = processing_path.stem
        transcribe.write_transcript(result, stem)
    except Exception:
        log.exception("Transcription failed for %s", processing_path.name)
        shutil.move(str(processing_path), str(config.FAILED_DIR / processing_path.name))
        return

    # Analysis is a soft-failure step: analyze_and_write() catches its own
    # errors and logs them rather than raising, so a bad/unreachable Ollama
    # call here never undoes the transcription success above.
    log.info("Analyzing: %s", processing_path.name)
    analysis_text = transcribe.build_analysis_text(result)
    analyzer.analyze_and_write(analysis_text, stem)

    shutil.move(str(processing_path), str(config.ARCHIVE_DIR / processing_path.name))
    log.info("Done: %s", processing_path.name)


def worker_loop(pending_q: "queue.Queue[Path]", stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            path = pending_q.get(timeout=1)
        except queue.Empty:
            continue
        try:
            process_one(path)
        except Exception:
            log.exception("Unexpected error processing %s", path)


def scan_existing_files(pending_q: "queue.Queue[Path]") -> None:
    """Catch anything already sitting in INBOX_DIR at startup (e.g. daemon was
    down when Syncthing delivered files)."""
    for path in config.INBOX_DIR.iterdir():
        if is_candidate_file(path):
            log.info("Found existing file at startup: %s", path.name)
            pending_q.put(path)


# --- Voice enrollment watcher (#37) — independent of the main inbox flow ---

class EnrollmentFileHandler(FileSystemEventHandler):
    """Watches ENROLLMENT_DIR for new audio samples specifically (ignores
    the .json sidecar files — those get read once the audio is stable)."""

    def __init__(self, pending_q: "queue.Queue[Path]"):
        self.pending_q = pending_q

    def on_created(self, event):
        if not event.is_directory:
            self._maybe_enqueue(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self._maybe_enqueue(Path(event.dest_path))

    def _maybe_enqueue(self, path: Path):
        if path.suffix.lower() in config.AUDIO_EXTENSIONS and not path.name.startswith("."):
            log.debug("Detected enrollment sample: %s", path)
            self.pending_q.put(path)


def process_enrollment(audio_path: Path) -> None:
    """
    Expects a JSON sidecar at the same path with a .json extension instead
    of the audio extension, containing {"name": ..., "is_main_user": ...}.
    Extracts a voice embedding and stores it via speaker_profiles.py, then
    cleans up both files — we don't need to keep the raw enrollment audio
    around once the embedding's extracted.
    """
    if not audio_path.exists():
        return

    log.info("Waiting for enrollment sample to stabilize: %s", audio_path.name)
    if not wait_until_stable(audio_path):
        return

    sidecar_path = audio_path.with_suffix(".json")
    # The sidecar is small and usually arrives first, but give it a moment
    # in case of a timing race with the (larger, slower) audio upload.
    for _ in range(10):
        if sidecar_path.exists():
            break
        time.sleep(0.5)
    else:
        log.warning("Enrollment sidecar never appeared for %s — skipping", audio_path.name)
        return

    try:
        metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
        name = metadata["name"]
        is_main_user = metadata.get("is_main_user", False)
    except (json.JSONDecodeError, KeyError, OSError) as e:
        log.warning("Malformed enrollment sidecar for %s: %s", audio_path.name, e)
        return

    log.info("Extracting voice embedding for enrollment: %s", name)
    try:
        embedding = diarize.extract_enrollment_embedding(audio_path)
    except Exception:
        log.exception("Enrollment embedding extraction failed for %s", name)
        embedding = None

    if embedding is None:
        log.warning("No embedding extracted for %s — enrollment did not complete", name)
    else:
        speaker_profiles.add_profile(name, embedding, is_main_user=is_main_user)
        log.info("Enrolled voice profile: %s (main_user=%s)", name, is_main_user)

    audio_path.unlink(missing_ok=True)
    sidecar_path.unlink(missing_ok=True)


def enrollment_worker_loop(pending_q: "queue.Queue[Path]", stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            path = pending_q.get(timeout=1)
        except queue.Empty:
            continue
        try:
            process_enrollment(path)
        except Exception:
            log.exception("Unexpected error processing enrollment %s", path)


def scan_existing_enrollments(pending_q: "queue.Queue[Path]") -> None:
    for path in config.ENROLLMENT_DIR.iterdir():
        if path.suffix.lower() in config.AUDIO_EXTENSIONS and not path.name.startswith("."):
            log.info("Found existing enrollment sample at startup: %s", path.name)
            pending_q.put(path)


def main():
    config.ensure_dirs()
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_FILE),
        ],
    )

    log.info("Starting omi-watcher. Inbox: %s", config.INBOX_DIR)

    pending_q: "queue.Queue[Path]" = queue.Queue()
    stop_event = threading.Event()

    scan_existing_files(pending_q)

    worker = threading.Thread(target=worker_loop, args=(pending_q, stop_event), daemon=True)
    worker.start()

    handler = NewFileHandler(pending_q)
    observer = Observer()
    observer.schedule(handler, str(config.INBOX_DIR), recursive=False)
    observer.start()

    # Enrollment (#37) — a fully independent queue/worker/observer, so a
    # backlog or slow embedding extraction on one never blocks the other.
    enrollment_q: "queue.Queue[Path]" = queue.Queue()
    scan_existing_enrollments(enrollment_q)
    enrollment_worker = threading.Thread(
        target=enrollment_worker_loop, args=(enrollment_q, stop_event), daemon=True
    )
    enrollment_worker.start()

    enrollment_handler = EnrollmentFileHandler(enrollment_q)
    enrollment_observer = Observer()
    enrollment_observer.schedule(enrollment_handler, str(config.ENROLLMENT_DIR), recursive=False)
    enrollment_observer.start()
    log.info("Watching for voice enrollment samples: %s", config.ENROLLMENT_DIR)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        observer.stop()
        observer.join()
        enrollment_observer.stop()
        enrollment_observer.join()
        stop_event.set()
        worker.join(timeout=5)
        enrollment_worker.join(timeout=5)


if __name__ == "__main__":
    main()
