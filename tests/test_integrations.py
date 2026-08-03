from datetime import date

import integrations

# A fixed Monday, so weekday-offset math is deterministic across runs.
MONDAY = date(2026, 8, 3)
assert MONDAY.weekday() == 0


def test_none_input_returns_none():
    assert integrations.parse_relative_date(None, MONDAY) is None


def test_empty_string_returns_none():
    assert integrations.parse_relative_date("", MONDAY) is None


def test_today():
    assert integrations.parse_relative_date("today", MONDAY) == MONDAY


def test_tomorrow():
    assert integrations.parse_relative_date("tomorrow", MONDAY) == date(2026, 8, 4)


def test_weekday_later_this_week():
    # Thursday is 3 days after a Monday.
    assert integrations.parse_relative_date("Thursday", MONDAY) == date(2026, 8, 6)


def test_next_weekday_phrasing_treated_same_as_plain_weekday():
    assert integrations.parse_relative_date("next Thursday", MONDAY) == date(2026, 8, 6)


def test_same_weekday_as_reference_rolls_to_next_week():
    """Saying 'Monday' ON a Monday almost certainly means next week's,
    not today — today would just be said as 'today'."""
    assert integrations.parse_relative_date("Monday", MONDAY) == date(2026, 8, 10)


def test_weekday_abbreviation_recognized():
    assert integrations.parse_relative_date("thu", MONDAY) == date(2026, 8, 6)


def test_iso_date_passthrough():
    assert integrations.parse_relative_date("2026-12-25", MONDAY) == date(2026, 12, 25)


def test_unrecognized_phrase_returns_none_rather_than_guessing():
    assert integrations.parse_relative_date("sometime next quarter", MONDAY) is None


def test_case_insensitive():
    assert integrations.parse_relative_date("TOMORROW", MONDAY) == date(2026, 8, 4)
