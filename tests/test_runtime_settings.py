import json
import stat

import config
import moduls.runtime_settings as runtime_settings


def test_auto_update_toggle_is_persistent(monkeypatch, tmp_path) -> None:
    settings_file = tmp_path / "runtime-settings.json"
    monkeypatch.setattr(config, "RUNTIME_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "SCHEDULE_AUTO_UPDATE_ENABLED", True)

    assert runtime_settings.is_auto_update_enabled() is True
    assert runtime_settings.toggle_auto_update() is False
    assert runtime_settings.is_auto_update_enabled() is False
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "auto_update_enabled": False
    }
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600


def test_invalid_runtime_settings_use_environment_default(
    monkeypatch,
    tmp_path,
) -> None:
    settings_file = tmp_path / "runtime-settings.json"
    settings_file.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(config, "RUNTIME_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "SCHEDULE_AUTO_UPDATE_ENABLED", False)

    assert runtime_settings.is_auto_update_enabled() is False
