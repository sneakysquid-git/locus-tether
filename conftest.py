"""
Shared pytest setup. This project's modules live across several top-level
directories (pipeline/, webapp/, speech_coach/, digest/) rather than one
installable package — each module normally handles its own sys.path setup
at runtime (see e.g. webapp.py's own sys.path.insert calls), but pytest
needs this done once, up front, before collection, for imports across
these directories to resolve correctly in tests.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
for subdir in ("pipeline", "webapp", "speech_coach", "digest"):
    sys.path.insert(0, str(ROOT / subdir))


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """
    Points OMI_PIPELINE_BASE at a fresh temp directory and reloads config
    so tests never touch real data or depend on state left over from a
    previous test. Most tests that read/write via config.BASE_DIR (or
    TRANSCRIPTS_DIR, etc.) should use this fixture.
    """
    monkeypatch.setenv("OMI_PIPELINE_BASE", str(tmp_path))
    (tmp_path / "transcripts").mkdir(exist_ok=True)

    import config
    import importlib
    importlib.reload(config)

    yield tmp_path
