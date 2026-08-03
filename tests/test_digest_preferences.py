import digest_preferences


def test_returns_none_when_never_configured(isolated_data_dir):
    """None specifically (not defaults) signals callers to fall back to
    the env-var-based config — a real, load-bearing distinction, not an
    implementation detail."""
    assert digest_preferences.get_preferences() is None


def test_set_and_get_roundtrip(isolated_data_dir):
    digest_preferences.set_preferences(enabled=True, email="test@example.com")
    prefs = digest_preferences.get_preferences()
    assert prefs == {"enabled": True, "email": "test@example.com"}


def test_disabled_state_persists(isolated_data_dir):
    digest_preferences.set_preferences(enabled=False, email="")
    prefs = digest_preferences.get_preferences()
    assert prefs["enabled"] is False


def test_corrupted_file_falls_back_to_none(isolated_data_dir):
    """A corrupted preferences file should behave like 'never configured'
    (fall back to env-var config) rather than crash the caller."""
    path = digest_preferences._preferences_path()
    path.write_text("{not valid json", encoding="utf-8")
    assert digest_preferences.get_preferences() is None
