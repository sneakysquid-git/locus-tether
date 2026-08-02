"""
Persistent voice enrollment (#37): stores a name + voice "fingerprint"
(embedding vector) for each enrolled person, and matches newly-detected
speakers against these fingerprints so conversations can show real names
instead of anonymous SPEAKER_NN labels.

One enrolled profile can be marked as the "main user" (the wearer) — this
is what lets speech coaching correctly analyze only the wearer's own
speech in a multi-person recording, rather than blending everyone's
speaking patterns together (a real, previously-unaddressed gap: before
this, speech coaching metrics were computed across the WHOLE transcript
regardless of how many people were actually talking).

Deliberately conservative matching: an unrecognized speaker just stays
labeled SPEAKER_NN (same as before enrollment existed) rather than risk
mislabeling a stranger as a known person — a wrong match is worse than no
match at all.
"""
import json
import threading
from pathlib import Path
from typing import Optional

import config

_lock = threading.Lock()

# Cosine similarity threshold for accepting a match — deliberately
# conservative (see module docstring). Not yet tuned against real
# enrollment data; a real-hardware first pass may reveal this needs
# adjustment, same as several other constants in this project have.
MATCH_THRESHOLD = 0.75


def _profiles_path() -> Path:
    return config.BASE_DIR / "speaker_profiles.json"


def _load() -> list:
    path = _profiles_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(profiles: list) -> None:
    path = _profiles_path()
    path.write_text(json.dumps(profiles, indent=2), encoding="utf-8")


def list_profiles() -> list:
    """Profiles WITHOUT their embedding vectors — those are large and not
    useful for display, only for matching."""
    return [{"name": p["name"], "is_main_user": p.get("is_main_user", False)} for p in _load()]


def get_main_user() -> Optional[str]:
    for p in _load():
        if p.get("is_main_user"):
            return p["name"]
    return None


def set_main_user(name: str) -> None:
    with _lock:
        profiles = _load()
        if not any(p["name"] == name for p in profiles):
            raise ValueError(f"No enrolled profile named {name!r}")
        for p in profiles:
            p["is_main_user"] = p["name"] == name
        _save(profiles)


def add_profile(name: str, embedding: list, is_main_user: bool = False) -> None:
    """Adds or replaces (if re-enrolling the same name) a profile."""
    with _lock:
        profiles = [p for p in _load() if p["name"] != name]
        if is_main_user:
            for p in profiles:
                p["is_main_user"] = False
        profiles.append({"name": name, "embedding": embedding, "is_main_user": is_main_user})
        _save(profiles)


def delete_profile(name: str) -> None:
    with _lock:
        _save([p for p in _load() if p["name"] != name])


def match_embedding(embedding: list, threshold: float = MATCH_THRESHOLD) -> Optional[str]:
    """
    Cosine similarity match against all enrolled profiles. Returns the
    best-matching name if its similarity clears the threshold, else None.

    numpy is imported LOCALLY (not at module level) deliberately — this
    module is imported by webapp.py too (for listing profiles / setting
    the main user, neither of which touches embeddings at all), and
    webapp.py runs natively on the host without numpy/torch installed.
    Only the container-side diarization pipeline ever actually calls this
    function, so the import only needs to succeed there.
    """
    profiles = _load()
    if not profiles:
        return None

    import numpy as np

    query = np.array(embedding, dtype=float)
    query = query / (np.linalg.norm(query) + 1e-9)

    best_name, best_score = None, -1.0
    for p in profiles:
        candidate = np.array(p["embedding"], dtype=float)
        candidate = candidate / (np.linalg.norm(candidate) + 1e-9)
        score = float(np.dot(query, candidate))
        if score > best_score:
            best_score, best_name = score, p["name"]

    return best_name if best_score >= threshold else None
