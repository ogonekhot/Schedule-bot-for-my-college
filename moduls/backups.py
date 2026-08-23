"""Create, list and restore local schedule snapshots."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config

LOGGER = logging.getLogger(__name__)
BACKUP_ID_RE = re.compile(r"^\d{8}T\d{12}$")


class ScheduleBackupError(RuntimeError):
    """Raised when a schedule snapshot cannot be created or restored."""


@dataclass(frozen=True)
class ScheduleBackupInfo:
    backup_id: str
    created_at: str
    reason: str
    group_count: int
    path: Path


def _atomic_json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=4)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ScheduleBackupError(f"не удалось прочитать {path}: {exc}") from exc


def _current_schedule() -> dict[str, Any]:
    path = (
        config.SCHEDULE_FILE
        if config.SCHEDULE_FILE.exists()
        else config.INITIAL_SCHEDULE_FILE
    )
    value = _load_json(path)
    if not isinstance(value, dict):
        raise ScheduleBackupError("текущий кеш расписания имеет неверный формат")
    return value


def _current_reference() -> dict[str, Any]:
    if config.REFERENCE_FILE.exists():
        value = _load_json(config.REFERENCE_FILE)
    else:
        settings = _load_json(config.GLOBAL_SETTINGS_FILE)
        value = settings.get("references", {}) if isinstance(settings, dict) else {}
    if not isinstance(value, dict) or not value.get("date") or not value.get("color"):
        raise ScheduleBackupError("не найдены дата и цвет эталонной недели")
    return value


def _backup_path(backup_id: str) -> Path:
    if not BACKUP_ID_RE.fullmatch(backup_id):
        raise ScheduleBackupError("некорректный идентификатор резервной копии")
    return config.SCHEDULE_BACKUP_DIR / f"schedule-{backup_id}.json"


def _payload_to_info(path: Path, payload: Any) -> ScheduleBackupInfo:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ScheduleBackupError(f"неподдерживаемый формат копии {path.name}")
    schedule = payload.get("schedule")
    reference = payload.get("reference")
    if not isinstance(schedule, dict) or not isinstance(reference, dict):
        raise ScheduleBackupError(f"копия {path.name} повреждена")
    backup_id = path.stem.removeprefix("schedule-")
    if not BACKUP_ID_RE.fullmatch(backup_id):
        raise ScheduleBackupError(f"некорректное имя копии {path.name}")
    return ScheduleBackupInfo(
        backup_id=backup_id,
        created_at=str(payload.get("created_at", "")),
        reason=str(payload.get("reason", "unknown")),
        group_count=len(schedule),
        path=path,
    )


def _prune_backups() -> None:
    paths = sorted(
        config.SCHEDULE_BACKUP_DIR.glob("schedule-*.json"),
        reverse=True,
    )
    for path in paths[config.SCHEDULE_BACKUP_LIMIT :]:
        try:
            path.unlink()
        except OSError as exc:
            LOGGER.warning("Не удалось удалить старую копию %s: %s", path, exc)


def create_schedule_backup(reason: str) -> ScheduleBackupInfo:
    """Snapshot the current schedule/reference and enforce retention."""

    config.SCHEDULE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(config.SCHEDULE_BACKUP_DIR, 0o700)

    now = datetime.now(ZoneInfo(config.BOT_TIMEZONE))
    backup_id = now.strftime("%Y%m%dT%H%M%S%f")
    path = _backup_path(backup_id)
    payload = {
        "version": 1,
        "created_at": now.isoformat(timespec="seconds"),
        "reason": str(reason),
        "schedule": _current_schedule(),
        "reference": _current_reference(),
    }
    _atomic_json_dump(path, payload)
    _prune_backups()
    return _payload_to_info(path, payload)


def list_schedule_backups() -> tuple[ScheduleBackupInfo, ...]:
    if not config.SCHEDULE_BACKUP_DIR.exists():
        return ()

    result: list[ScheduleBackupInfo] = []
    for path in sorted(
        config.SCHEDULE_BACKUP_DIR.glob("schedule-*.json"),
        reverse=True,
    ):
        try:
            result.append(_payload_to_info(path, _load_json(path)))
        except ScheduleBackupError as exc:
            LOGGER.warning("Пропускаем повреждённую резервную копию: %s", exc)
    return tuple(result)


def get_schedule_backup(backup_id: str) -> ScheduleBackupInfo:
    path = _backup_path(backup_id)
    if not path.is_file():
        raise ScheduleBackupError("резервная копия не найдена")
    return _payload_to_info(path, _load_json(path))


def restore_schedule_backup(backup_id: str) -> ScheduleBackupInfo:
    """Restore a validated snapshot after backing up the current state."""

    path = _backup_path(backup_id)
    payload = _load_json(path)
    info = _payload_to_info(path, payload)
    schedule = payload["schedule"]
    reference = payload["reference"]

    create_schedule_backup("before_restore")
    _atomic_json_dump(config.SCHEDULE_FILE, schedule)
    _atomic_json_dump(config.REFERENCE_FILE, reference)
    return info
