"""Application configuration loaded from environment variables."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом") from exc


def _path_env(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default)).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


# Telegram
TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_API_BASE_URL = os.getenv("BOT_API_BASE_URL", "").strip().rstrip("/")
BOT_API_IS_LOCAL = _bool_env("BOT_API_IS_LOCAL", bool(BOT_API_BASE_URL))
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow").strip()

# Database
DB_HOST = os.getenv("DB_HOST", "127.0.0.1").strip()
DB_PORT = _int_env("DB_PORT", 3306)
DB_USER = os.getenv("DB_USER", "schedule_bot").strip()
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "schedule_bot").strip()

# Runtime and static files
GLOBAL_SETTINGS_FILE = _path_env("GLOBAL_SETTINGS_FILE", "settings/global.json")
ADDRESSES_FILE = _path_env("ADDRESSES_FILE", "settings/addresses.json")
INITIAL_SCHEDULE_FILE = _path_env("INITIAL_SCHEDULE_FILE", "settings/schedule.json")
DATA_DIR = _path_env("DATA_DIR", "data")
SCHEDULE_FILE = _path_env("SCHEDULE_FILE", str(DATA_DIR / "schedule.json"))
REFERENCE_FILE = _path_env("REFERENCE_FILE", str(DATA_DIR / "reference.json"))
SCHEDULE_ACCOUNTS_FILE = _path_env(
    "SCHEDULE_ACCOUNTS_FILE", str(DATA_DIR / "accounts.json")
)
RUNTIME_SETTINGS_FILE = _path_env(
    "RUNTIME_SETTINGS_FILE", str(DATA_DIR / "runtime-settings.json")
)
SCHEDULE_BACKUP_DIR = _path_env(
    "SCHEDULE_BACKUP_DIR", str(DATA_DIR / "backups")
)
SCHEDULE_BACKUP_LIMIT = _int_env("SCHEDULE_BACKUP_LIMIT", 10)

# College site and automatic updates
COLLEGE_LOGIN_URL = os.getenv("COLLEGE_LOGIN_URL", "http://lk.stu.lipetsk.ru/").strip()
COLLEGE_SCHEDULE_URL = os.getenv(
    "COLLEGE_SCHEDULE_URL",
    "http://lk.stu.lipetsk.ru/education/0/5:136841076/",
).strip()
SCHEDULE_BROWSER = os.getenv("SCHEDULE_BROWSER", "chromium").strip().lower()
SCHEDULE_HEADLESS = _bool_env("SCHEDULE_HEADLESS", True)
SCHEDULE_AUTO_UPDATE_ENABLED = _bool_env(
    "SCHEDULE_AUTO_UPDATE_ENABLED", True
)
SCHEDULE_UPDATE_ON_STARTUP = _bool_env("SCHEDULE_UPDATE_ON_STARTUP", True)
SCHEDULE_UPDATE_INTERVAL_MINUTES = _int_env(
    "SCHEDULE_UPDATE_INTERVAL_MINUTES", 360, minimum=5
)
SCHEDULE_UPDATE_MAX_ATTEMPTS = _int_env("SCHEDULE_UPDATE_MAX_ATTEMPTS", 3)
SCHEDULE_UPDATE_RETRY_SECONDS = _int_env("SCHEDULE_UPDATE_RETRY_SECONDS", 10)
SCHEDULE_PAGE_TIMEOUT_SECONDS = _int_env("SCHEDULE_PAGE_TIMEOUT_SECONDS", 35)
SCHEDULE_RECOVERY_ENABLED = _bool_env("SCHEDULE_RECOVERY_ENABLED", False)
SCHEDULE_RECOVERY_GROUP = os.getenv("SCHEDULE_RECOVERY_GROUP", "").strip()
SCHEDULE_RECOVERY_INTERVAL_MINUTES = _int_env(
    "SCHEDULE_RECOVERY_INTERVAL_MINUTES", 5, minimum=5
)
SCHEDULE_RECOVERY_JITTER_SECONDS = _int_env(
    "SCHEDULE_RECOVERY_JITTER_SECONDS", 45, minimum=0
)
SCHEDULE_RECOVERY_HTTP_TIMEOUT_SECONDS = _int_env(
    "SCHEDULE_RECOVERY_HTTP_TIMEOUT_SECONDS", 20, minimum=5
)
SCHEDULE_RECOVERY_CAPTURE_DIR = _path_env(
    "SCHEDULE_RECOVERY_CAPTURE_DIR", str(DATA_DIR / "recovery-captures")
)
SCHEDULE_RECOVERY_STATE_FILE = _path_env(
    "SCHEDULE_RECOVERY_STATE_FILE", str(DATA_DIR / "recovery-state.json")
)


def _validated_schedule_accounts(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise TypeError("Список аккаунтов расписания должен быть JSON-объектом")

    validated: dict[str, dict[str, str]] = {}
    for group, credentials in value.items():
        if not isinstance(credentials, dict):
            continue
        login = str(credentials.get("login", "")).strip()
        password = str(credentials.get("password", "")).strip()
        if login and password and set(login) != {"*"} and set(password) != {"*"}:
            validated[str(group)] = {"login": login, "password": password}
    return validated


def load_runtime_schedule_accounts() -> dict[str, dict[str, str]]:
    """Load accounts added by users through the Telegram registration flow."""

    if not SCHEDULE_ACCOUNTS_FILE.exists():
        return {}
    try:
        with SCHEDULE_ACCOUNTS_FILE.open("r", encoding="utf-8-sig") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Не удалось загрузить {SCHEDULE_ACCOUNTS_FILE}: {exc}"
        ) from exc
    return _validated_schedule_accounts(value)


def load_schedule_accounts(settings: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Merge bundled, environment and user-added college accounts."""

    accounts = _validated_schedule_accounts(settings.get("accounts", {}))

    raw = os.getenv("SCHEDULE_ACCOUNTS_JSON", "").strip()
    if raw:
        try:
            environment_accounts = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "SCHEDULE_ACCOUNTS_JSON содержит некорректный JSON"
            ) from exc
        accounts.update(_validated_schedule_accounts(environment_accounts))

    accounts.update(load_runtime_schedule_accounts())
    return accounts

def validate_runtime_config() -> None:
    if not TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и заполните его."
        )
    if SCHEDULE_BROWSER not in {"chromium", "firefox", "webkit"}:
        raise RuntimeError("SCHEDULE_BROWSER должен быть chromium, firefox или webkit")
    if SCHEDULE_RECOVERY_ENABLED and not SCHEDULE_RECOVERY_GROUP:
        raise RuntimeError(
            "При SCHEDULE_RECOVERY_ENABLED=true задайте SCHEDULE_RECOVERY_GROUP"
        )
