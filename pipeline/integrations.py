"""
Date-parsing utility, originally built for the Things 3 export (removed —
see git history and issue #1 if curious why) but kept since webapp.py's
"due soon" detection on the Today tab also depends on turning the LLM's
casual due-date phrases ("tomorrow", "Thursday") into an actual date.
"""
import re
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
    date. Deliberately conservative: returns None rather than guessing wrong
    when the phrase isn't recognized.

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
