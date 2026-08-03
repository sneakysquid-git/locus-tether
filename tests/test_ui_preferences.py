import ui_preferences


def test_defaults_when_nothing_saved(isolated_data_dir):
    prefs = ui_preferences.get_preferences()
    assert prefs == {"theme": "dark", "text_size": "medium"}


def test_set_one_preference_does_not_clobber_the_other(isolated_data_dir):
    ui_preferences.set_preferences(text_size="large")
    ui_preferences.set_preferences(theme="light")

    prefs = ui_preferences.get_preferences()
    assert prefs["theme"] == "light"
    assert prefs["text_size"] == "large"  # must survive the later theme-only update


def test_persists_across_reads(isolated_data_dir):
    ui_preferences.set_preferences(theme="light", text_size="small")
    # A fresh call re-reads from disk rather than relying on in-memory state
    prefs = ui_preferences.get_preferences()
    assert prefs == {"theme": "light", "text_size": "small"}
