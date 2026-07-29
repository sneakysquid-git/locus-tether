"""
Export action items to Things 3 (Mac/iOS to-do app) via its documented
add-json URL scheme: https://culturedcode.com/things/support/articles/2803573/

No authentication needed — this just builds a `things:///add-json?data=...`
link. Tapping it on a device with Things 3 installed opens the app and
creates the to-dos directly. Only works on Apple devices with Things
installed; the link is harmless (does nothing) anywhere else.

Kept as its own module so a future Google Tasks integration (which will need
real OAuth, unlike this) can live alongside it without tangling the two.
"""
import json
import re
import urllib.parse
from datetime import date, timedelta
from typing import Optional

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def parse_relative_date(due_date_str: Optional[str], reference_date: date) -> Optional[date]:
    """
    Best-effort parser for the kind of casual phrases our LLM prompt tends to
    produce ("tomorrow", "Thursday", "next Tuesday at 2pm") into an actual
    date Things can use. Deliberately conservative: returns None rather than
    guessing wrong when the phrase isn't recognized, so callers can fall back
    to putting the raw text in a notes field instead of silently
    mis-scheduling something.

    Note on "next <weekday>": treated the same as plain "<weekday>" (nearest
    future occurrence) rather than trying to disambiguate "this Tuesday" vs
    "next Tuesday" — that distinction is genuinely ambiguous in casual
    speech, and guessing wrong here is worse than just picking the nearer
    occurrence consistently.
    """
    if not due_date_str:
        return None

    text = due_date_str.strip().lower()

    if text == "today":
        return reference_date
    if text == "tomorrow":
        return reference_date + timedelta(days=1)

    # Matches "thursday", "next thursday", "thu", "next tuesday at 2pm", etc.
    match = re.search(r"\b(next\s+)?(" + "|".join(_WEEKDAYS.keys()) + r")\b", text)
    if match:
        target_weekday = _WEEKDAYS[match.group(2)]
        days_ahead = (target_weekday - reference_date.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # "Thursday" said ON a Thursday almost certainly means next week's
        return reference_date + timedelta(days=days_ahead)

    # Explicit ISO date, in case a model ever produces one directly.
    iso_match = re.match(r"^\d{4}-\d{2}-\d{2}$", text)
    if iso_match:
        return date.fromisoformat(text)

    return None  # unrecognized — caller should fall back to notes text


def build_things_items(analyses: list[dict], reference_date: date) -> list[dict]:
    """
    Converts all action_items across the day's analyses into Things'
    add-json item format. Parseable due dates become a real scheduled date
    ("when"); anything we can't confidently parse keeps the original phrase
    visible in the notes instead of being silently dropped or mis-dated.
    """
    items = []
    for a in analyses:
        source_title = a.get("title", a.get("_stem", "Untitled"))
        for action in a.get("action_items", []):
            due_raw = action.get("due_date")
            parsed = parse_relative_date(due_raw, reference_date)

            attributes = {
                "title": action["description"],
                "notes": f"From: {source_title}" + (f" (due: {due_raw})" if due_raw and not parsed else ""),
            }
            if parsed:
                attributes["when"] = parsed.isoformat()

            items.append({"type": "to-do", "attributes": attributes})
    return items


def build_things_link(analyses: list[dict], reference_date: date) -> Optional[str]:
    """
    Returns a things:///add-json?... URL for all of the day's action items,
    or None if there are no action items at all (no point in a link that
    would add nothing).

    TODO (known issue, revisit later): confirmed in real testing that only
    ONE item ends up imported into Things even when this builds a JSON array
    with multiple to-do objects. Likely causes to check: Things may need
    each item as a separate top-level array element with a different
    structure than what we're sending, a URL-length/encoding truncation
    somewhere between here and Things actually parsing it, or the
    x-callback-url scheme silently dropping all but the first item on
    malformed input rather than erroring. Test by shrinking the payload to
    exactly 2 known-simple items and inspecting the raw decoded JSON Things
    receives, rather than guessing further.
    """
    items = build_things_items(analyses, reference_date)
    if not items:
        return None

    payload = urllib.parse.quote(json.dumps(items))
    return f"things:///add-json?data={payload}"
