from app.config import Settings, env


def test_kastec_settings_with_cloudtec_fallback(monkeypatch):
    for name in ("KASTEC_SESSION_HOURS", "CLOUDTEC_SESSION_HOURS", "KASTEC_SHELL", "CLOUDTEC_SHELL"):
        monkeypatch.delenv(name, raising=False)
    assert env("SESSION_HOURS", "8") == "8"
    monkeypatch.setenv("CLOUDTEC_SESSION_HOURS", "4")          # the old name still works
    assert Settings.from_env().session_hours == 4
    monkeypatch.setenv("KASTEC_SESSION_HOURS", "2")            # the new name wins
    assert Settings.from_env().session_hours == 2
    monkeypatch.setenv("KASTEC_SHELL", "OFF")
    assert Settings.from_env().shell == "off"
