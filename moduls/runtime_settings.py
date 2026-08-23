"""Persistent runtime switches controlled from the Telegram admin panel."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import config

LOGGER = logging.getLogger(__name__)


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


def load_runtime_settings() -> dict[str, Any]:
    if not config.RUNTIME_SETTINGS_FILE.exists():
        return {}
    try:
        with config.RUNTIME_SETTINGS_FILE.open(
            "r",
            encoding="utf-8-sig",
        ) as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error(
            "Не удалось загрузить runtime-настройки %s: %s",
            config.RUNTIME_SETTINGS_FILE,
            exc,
        )
        return {}
    if not isinstance(value, dict):
        LOGGER.error(
            "Runtime-настройки %s должны быть JSON-объектом",
            config.RUNTIME_SETTINGS_FILE,
        )
        return {}
    return value


def is_auto_update_enabled() -> bool:
    value = load_runtime_settings().get("auto_update_enabled")
    return value if isinstance(value, bool) else config.SCHEDULE_AUTO_UPDATE_ENABLED


def set_auto_update_enabled(enabled: bool) -> bool:
    settings = load_runtime_settings()
    settings["auto_update_enabled"] = bool(enabled)
    _atomic_json_dump(config.RUNTIME_SETTINGS_FILE, settings)
    return bool(enabled)


def toggle_auto_update() -> bool:
    return set_auto_update_enabled(not is_auto_update_enabled())
