"""
Enrolled voice profiles (#37), extended to support multiple reference
samples per person (see the headphones-vs-phone-mic case that motivated
this: a voice embedding is sensitive to recording conditions, not just the
underlying voice, so a single enrollment sample can fail to match a
recording captured under different conditions even for the same person).

Storage schema: {"name": str, "embeddings": [[float, ...], ...],
"is_main_user": bool}. Backward compatible with the older single-embedding
shape ({"embedding": [...]}) — an existing profile enrolled before this
change keeps working with zero migration needed; it just starts as a
list of one.

Matching checks a candidate embedding against EVERY reference sample
across every profile, and returns whichever person's BEST individual
sample clears the threshold — not an average across their samples, since
averaging could blur together genuinely different recording conditions
into a worse composite reference than any one of the real samples alone.
"""
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config

MATCH_THRESHOLD = 0.75


def _profiles_path() -> Path:
    # Deliberately TRANSCRIPTS_DIR, not bare BASE_DIR — see #37's real
    # host/container path bug this fixed: BASE_DIR resolves differently
    # inside the container (/app, not bind-mounted) vs on the host
    # (~/omi-data), while TRANSCRIPTS_DIR is correctly shared on both sides.
    return config.TRANSCRIPTS_DIR / "speaker_profiles.json"


def _load() -> List[dict]:
    path = _profiles_path()
    if not path.exists():
        return []
    try:
        profiles = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    # Backward compatibility: normalize any old single-embedding profiles
    # to the new list shape in memory, so every other function in this
    # module only ever needs to handle one shape.
    for p in profiles:
        if "embedding" in p and "embeddings" not in p:
            p["embeddings"] = [p.pop("embedding")]
    return profiles


def _save(profiles: List[dict]) -> None:
    _profiles_path().write_text(json.dumps(profiles, indent=2), encoding="utf-8")


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def list_profiles() -> List[dict]:
    """Returns profiles without their embeddings (large, and callers like
    the Settings UI only need name/is_main_user/sample count)."""
    return [
        {"name": p["name"], "is_main_user": p.get("is_main_user", False), "sample_count": len(p.get("embeddings", []))}
        for p in _load()
    ]


def get_main_user() -> Optional[str]:
    for p in _load():
        if p.get("is_main_user"):
            return p["name"]
    return None


def add_profile(name: str, embedding: List[float], is_main_user: bool = False) -> None:
    """
    Enrolling a NAME THAT ALREADY EXISTS appends this embedding as an
    additional reference sample for that person, rather than overwriting
    their existing one — this is what actually enables "enroll me again
    with a headphones sample" to work as a real improvement rather than
    just replacing one single-condition reference with another.
    """
    profiles = _load()
    existing = next((p for p in profiles if p["name"] == name), None)

    if is_main_user:
        for p in profiles:
            p["is_main_user"] = False

    if existing:
        existing["embeddings"].append(embedding)
        if is_main_user:
            existing["is_main_user"] = True
    else:
        profiles.append({"name": name, "embeddings": [embedding], "is_main_user": is_main_user})

    _save(profiles)


def delete_profile(name: str) -> bool:
    profiles = _load()
    remaining = [p for p in profiles if p["name"] != name]
    if len(remaining) == len(profiles):
        return False
    _save(remaining)
    return True


def set_main_user(name: str) -> bool:
    profiles = _load()
    found = False
    for p in profiles:
        if p["name"] == name:
            p["is_main_user"] = True
            found = True
        else:
            p["is_main_user"] = False
    if found:
        _save(profiles)
    return found


def match_embedding(embedding: List[float]) -> Optional[str]:
    """
    Checks against every reference sample across every enrolled profile —
    a match succeeds if ANY of a person's samples clears the threshold,
    not just their first/only one. Returns the name of whichever profile
    had the single best-scoring sample, if it cleared the threshold.
    """
    best_name, best_score = _best_match(embedding)
    if best_name is not None and best_score >= MATCH_THRESHOLD:
        return best_name
    return None


def best_match_debug(embedding: List[float]) -> Optional[Tuple[str, float]]:
    """
    Same search as match_embedding, but returns the closest candidate and
    its score REGARDLESS of whether it cleared the threshold — purely for
    diagnostic logging (see diarize.py) when a match fails and it's
    otherwise impossible to tell after the fact how close it actually was.
    Returns None only if there are no enrolled profiles at all.
    """
    best_name, best_score = _best_match(embedding)
    if best_name is None:
        return None
    return (best_name, best_score)


def _best_match(embedding: List[float]) -> Tuple[Optional[str], float]:
    best_name = None
    best_score = -1.0
    for profile in _load():
        for ref_embedding in profile.get("embeddings", []):
            score = _cosine_similarity(embedding, ref_embedding)
            if score > best_score:
                best_score = score
                best_name = profile["name"]
    return best_name, best_score
