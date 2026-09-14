from browserbuddy.config import Settings, get_settings


def test_defaults():
    s = Settings()
    assert s.host == "127.0.0.1"
    assert s.port == 8765
    assert s.data_dir.name == ".browserbuddy"
    assert s.db_path == s.data_dir / "buddy.db"


def test_env_override_and_dir_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSERBUDDY_DATA_DIR", str(tmp_path / "bb"))
    monkeypatch.setenv("BROWSERBUDDY_PORT", "9999")
    s = get_settings()
    assert s.port == 9999
    assert s.data_dir == tmp_path / "bb"
    assert s.data_dir.is_dir()  # get_settings creates it


def test_screenshot_dedup_threshold_default_and_override(monkeypatch):
    assert Settings().screenshot_dedup_threshold == 8
    monkeypatch.setenv("BROWSERBUDDY_SCREENSHOT_DEDUP_THRESHOLD", "3")
    assert Settings().screenshot_dedup_threshold == 3
