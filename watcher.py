"""
Folder-watcher daemon for the Omi -> Jetson Thor pipeline.

Flow per file:
  1. watchdog notices a new/modified file in INBOX_DIR.
  2. We wait until its size has stopped changing (handles Syncthing writing
     incrementally, and its .syncthing.*.tmp / partial-file conventions).
  3. Move it into PROCESSING_DIR (so a crash mid-transcription doesn't leave
     it sitting in INBOX_DIR to be silently skipped or double-queued).
  4. Transcribe with faster-whisper.
  5. On success: write transcript, move audio to ARCHIVE_DIR.
     On failure: move audio to FAILED_DIR, log the exception, keep going.

Run with:
    python watcher.py
or install as a systemd service (see omi-watcher.service in this folder).
"""
import logging
import queue
import shutil
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import config
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

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        observer.stop()
        observer.join()
        stop_event.set()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
