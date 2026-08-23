import json
import stat

import config
import moduls.backups as backups


def configure_paths(monkeypatch, tmp_path, *, limit: int = 10) -> None:
    settings_file = tmp_path / "global.json"
    initial_schedule_file = tmp_path / "initial-schedule.json"
    schedule_file = tmp_path / "schedule.json"
    reference_file = tmp_path / "reference.json"
    backup_dir = tmp_path / "backups"

    settings_file.write_text(
        json.dumps(
            {
                "references": {
                    "date": "01.01.2026",
                    "color": "Белая неделя",
                }
            }
        ),
        encoding="utf-8",
    )
    initial_schedule_file.write_text(
        json.dumps({"ГРУППА-А": {"ПН": {}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "GLOBAL_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "INITIAL_SCHEDULE_FILE", initial_schedule_file)
    monkeypatch.setattr(config, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(config, "REFERENCE_FILE", reference_file)
    monkeypatch.setattr(config, "SCHEDULE_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(config, "SCHEDULE_BACKUP_LIMIT", limit)
    monkeypatch.setattr(config, "BOT_TIMEZONE", "Europe/Moscow")


def test_create_backup_contains_no_credentials(monkeypatch, tmp_path) -> None:
    configure_paths(monkeypatch, tmp_path)

    info = backups.create_schedule_backup("manual")
    payload = json.loads(info.path.read_text(encoding="utf-8"))

    assert info.reason == "manual"
    assert info.group_count == 1
    assert payload["schedule"] == {"ГРУППА-А": {"ПН": {}}}
    assert set(payload) == {
        "version",
        "created_at",
        "reason",
        "schedule",
        "reference",
    }
    assert stat.S_IMODE(info.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.SCHEDULE_BACKUP_DIR.stat().st_mode) == 0o700


def test_restore_backup_preserves_current_version_first(
    monkeypatch,
    tmp_path,
) -> None:
    configure_paths(monkeypatch, tmp_path)

    original = backups.create_schedule_backup("manual")
    config.SCHEDULE_FILE.write_text(
        json.dumps({"ГРУППА-Б": {"ВТ": {}}}),
        encoding="utf-8",
    )
    config.REFERENCE_FILE.write_text(
        json.dumps(
            {
                "date": "02.01.2026",
                "color": "Зелёная неделя",
            }
        ),
        encoding="utf-8",
    )

    restored = backups.restore_schedule_backup(original.backup_id)

    assert restored.backup_id == original.backup_id
    assert json.loads(config.SCHEDULE_FILE.read_text(encoding="utf-8")) == {
        "ГРУППА-А": {"ПН": {}}
    }
    assert json.loads(config.REFERENCE_FILE.read_text(encoding="utf-8")) == {
        "date": "01.01.2026",
        "color": "Белая неделя",
    }
    assert len(backups.list_schedule_backups()) == 2


def test_backup_retention_keeps_only_latest_files(monkeypatch, tmp_path) -> None:
    configure_paths(monkeypatch, tmp_path, limit=2)

    backups.create_schedule_backup("manual")
    backups.create_schedule_backup("schedule_update")
    backups.create_schedule_backup("group_registration")

    listed = backups.list_schedule_backups()
    assert len(listed) == 2
    assert listed[0].backup_id > listed[1].backup_id
