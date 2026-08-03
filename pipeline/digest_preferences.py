"""
User-configurable daily digest email preferences (opt-in + destination
email), settable from the webapp's Settings page rather than requiring a
direct edit to .env.digest.

Both webapp.py and digest.py are native host processes — unlike
speaker_profiles.json, there's no host/container boundary to worry about
here (see #37's path bug for why that mattered elsewhere).

Falls back to the original env-var-based config (OMI_DIGEST_EMAIL_ENABLED/
OMI_DIGEST_EMAIL_TO) when this file doesn't exist yet — preserves backward
compatibility for anyone who prefers just editing .env.digest directly
without ever touching the webapp.
"""
import json
from pathlib import Path
from typing import Optional

import config


def _preferences_path() -> Path:
    return config.BASE_DIR / "digest_preferences.json"


def get_preferences() -> Optional[dict]:
    """
    Returns {"enabled": bool, "email": str} if explicitly saved via the
    webapp, or None if never configured there — callers should fall back
    to the original env-var config (config.DIGEST_EMAIL_ENABLED /
    config.DIGEST_EMAIL_TO) in that case.
    """
    path = _preferences_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"enabled": bool(data.get("enabled", False)), "email": data.get("email", "")}
    except (json.JSONDecodeError, OSError):
        return None


def set_preferences(enabled: bool, email: str) -> None:
    path = _preferences_path()
    path.write_text(
        json.dumps({"enabled": enabled, "email": email}, indent=2),
        encoding="utf-8",
    )
