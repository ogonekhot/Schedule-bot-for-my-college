"""Download, parse and atomically persist the college schedule."""

from __future__ import annotations

import asyncio
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

from bs4 import BeautifulSoup, Tag
from playwright.async_api import (
    Browser,
    Page,
    Response,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

import config

LOGGER = logging.getLogger(__name__)

WHITE_WEEK = "Белая неделя"
GREEN_WEEK = "Зелёная неделя"
WEEKDAYS = {"ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"}
TIME_RE = re.compile(r"^(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})$")
GROUP_LABEL_RE = re.compile(
    r"(?:учебн(?:ая|ой)\s+)?групп(?:а|ы|е)\s*(?:№|N)?\s*[:\-]?\s*"
    r"([А-ЯЁA-Z0-9]{1,12}(?:\s*-\s*[А-ЯЁA-Z0-9]{1,12}){2,4})",
    re.IGNORECASE,
)
GROUP_CODE_RE = re.compile(
    r"(?<![\w-])([А-ЯЁA-Z]\d{0,2}\s*-\s*[А-ЯЁA-Z]{1,8}"
    r"\s*-\s*\d{2}\s*-\s*\d{1,3})(?![\w-])",
    re.IGNORECASE,
)
LESSON_TYPES = {
    "пр.": "практика",
    "практ.": "практика",
    "лек.": "лекция",
    "лаб.": "лабораторная",
}


class ScheduleUpdateError(RuntimeError):
    """Raised when a complete, safe schedule update cannot be produced."""


class ScheduleCredentialsError(ScheduleUpdateError):
    """Raised when the college site rejects supplied credentials."""


@dataclass(frozen=True)
class AccountRegistrationResult:
    group: str
    account_added: bool


@dataclass(frozen=True)
class UpdateResult:
    groups: tuple[str, ...]
    reference_date: str
    reference_color: str
    updated_at: str


def _empty_lesson(start: str, end: str) -> dict[str, Any]:
    empty = {"title": "", "teacher": "", "room": "", "type": ""}
    return {
        "time": {"start": start, "end": end},
        WHITE_WEEK: empty.copy(),
        GREEN_WEEK: empty.copy(),
    }


def _clean_lines(cell: Tag) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in cell.get_text("\n", strip=True).splitlines()
        if line.strip()
    ]


def parse_schedule_html(html: str) -> dict[str, dict[str, Any]]:
    """Convert the AJAX schedule table into the bot's JSON structure."""

    soup = BeautifulSoup(html, "html.parser")
    table_body = soup.find("tbody")
    if table_body is None:
        raise ScheduleUpdateError("в ответе сайта не найдена таблица расписания")

    result: dict[str, dict[str, Any]] = {}
    current_day: str | None = None
    current_lesson: str | None = None

    for cell in table_body.select("td"):
        lines = _clean_lines(cell)
        if not lines:
            continue

        joined = " ".join(lines).strip()
        weekday = joined.upper()
        if weekday in WEEKDAYS:
            current_day = weekday
            result.setdefault(current_day, {})
            current_lesson = None
            continue

        time_match = TIME_RE.fullmatch(joined)
        if time_match:
            if current_day is None:
                continue
            current_lesson = str(len(result[current_day]) + 1)
            result[current_day][current_lesson] = _empty_lesson(*time_match.groups())
            continue

        if current_day is None or current_lesson is None:
            continue

        classes = set(cell.get("class", []))
        color = GREEN_WEEK if "bgGreen" in classes else WHITE_WEEK
        lesson = result[current_day][current_lesson][color]

        lesson_type = LESSON_TYPES.get(lines[-1].casefold())
        if len(lines) == 2 and lesson_type:
            lesson["room"] = lines[0]
            lesson["type"] = lesson_type
        elif len(lines) >= 3:
            lesson["title"] = lines[0]
            lesson["teacher"] = lines[-1]
        elif len(lines) == 2:
            lesson["title"] = lines[0]
            lesson["teacher"] = lines[1]
        else:
            lesson["title"] = lines[0]

    lesson_count = sum(len(day) for day in result.values())
    if not result or lesson_count == 0:
        raise ScheduleUpdateError("сайт вернул пустое расписание")
    return result


def _normalize_group_name(value: str) -> str:
    return re.sub(r"\s*-\s*", "-", value.strip()).upper()


def detect_group_name(*documents: str) -> str:
    """Extract the authenticated student's group from college page HTML."""

    searchable: list[str] = []
    for document in documents:
        if not document:
            continue
        text = BeautifulSoup(document, "html.parser").get_text(" ", strip=True)
        searchable.append(re.sub(r"\s+", " ", text))

    for text in searchable:
        match = GROUP_LABEL_RE.search(text)
        if match:
            return _normalize_group_name(match.group(1))

    for text in searchable:
        match = GROUP_CODE_RE.search(text)
        if match:
            return _normalize_group_name(match.group(1))

    raise ScheduleUpdateError(
        "в личном кабинете не удалось определить название учебной группы"
    )


def _decode_body(body: bytes) -> str:
    for encoding in ("utf-8", "cp1251"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("cp1251", errors="replace")


async def _wait_for_schedule_response(page: Page) -> str:
    loop = asyncio.get_running_loop()
    response_future: asyncio.Future[str] = loop.create_future()

    async def capture(response: Response) -> None:
        if "ajax.handler.php" not in response.url or response_future.done():
            return
        try:
            text = _decode_body(await response.body())
            if "<td" in text.casefold() or "<tbody" in text.casefold():
                response_future.set_result(text)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            LOGGER.debug("Не удалось прочитать AJAX-ответ %s: %s", response.url, exc)

    page.on("response", capture)
    timeout_ms = config.SCHEDULE_PAGE_TIMEOUT_SECONDS * 1000
    try:
        await page.goto(
            config.COLLEGE_SCHEDULE_URL,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(response_future),
                timeout=config.SCHEDULE_PAGE_TIMEOUT_SECONDS,
            )
        except (TimeoutError, PlaywrightTimeoutError):
            LOGGER.info(
                "AJAX-ответ не пришёл после перехода, пробуем обновить страницу"
            )
            await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            return await asyncio.wait_for(
                asyncio.shield(response_future),
                timeout=config.SCHEDULE_PAGE_TIMEOUT_SECONDS,
            )
    finally:
        page.remove_listener("response", capture)


async def _read_week_color(page: Page) -> str | None:
    for text in await page.locator("div[role=alert]").all_text_contents():
        normalized = text.casefold().replace("ё", "е")
        if "белая" in normalized:
            return WHITE_WEEK
        if "зеленая" in normalized:
            return GREEN_WEEK
    return None


async def _fetch_group(
    browser: Browser,
    group: str | None,
    credentials: dict[str, str],
    *,
    detect_group: bool = False,
) -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()
    timeout_ms = config.SCHEDULE_PAGE_TIMEOUT_SECONDS * 1000
    page.set_default_timeout(timeout_ms)

    try:
        await page.goto(
            config.COLLEGE_LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        await page.locator('input[name="LOGIN"]').fill(credentials["login"])
        await page.locator('input[name="PASSWORD"]').fill(credentials["password"])
        await page.locator('button[type="submit"]').click()
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            LOGGER.debug("После входа networkidle не наступил, продолжаем")

        login_field = page.locator('input[name="LOGIN"]')
        if await login_field.count() and await login_field.first.is_visible():
            prefix = f"группа {group}: " if group else ""
            raise ScheduleCredentialsError(
                f"{prefix}сайт не принял логин или пароль"
            )

        authenticated_html = await page.content()
        ajax_html = await _wait_for_schedule_response(page)
        schedule_page_html = await page.content()
        week_color = await _read_week_color(page)
        detected_group = (
            detect_group_name(
                authenticated_html,
                schedule_page_html,
                ajax_html,
            )
            if detect_group
            else None
        )
        return parse_schedule_html(ajax_html), week_color, detected_group
    finally:
        await context.close()


async def _fetch_group_with_retries(
    browser: Browser, group: str, credentials: dict[str, str]
) -> tuple[dict[str, dict[str, Any]], str | None]:
    last_error: Exception | None = None
    for attempt in range(1, config.SCHEDULE_UPDATE_MAX_ATTEMPTS + 1):
        try:
            group_schedule, color, _ = await _fetch_group(
                browser,
                group,
                credentials,
            )
            return group_schedule, color
        except ScheduleCredentialsError:
            raise
        except Exception as exc:  # noqa: BLE001 - retries cover site failures
            last_error = exc
            LOGGER.warning(
                "Попытка %s/%s для группы %s не удалась: %s",
                attempt,
                config.SCHEDULE_UPDATE_MAX_ATTEMPTS,
                group,
                exc,
            )
            if attempt < config.SCHEDULE_UPDATE_MAX_ATTEMPTS:
                await asyncio.sleep(config.SCHEDULE_UPDATE_RETRY_SECONDS * attempt)
    raise ScheduleUpdateError(f"не удалось обновить группу {group}: {last_error}")


async def _verify_account_with_retries(
    browser: Browser,
    credentials: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], str | None, str]:
    last_error: Exception | None = None
    for attempt in range(1, config.SCHEDULE_UPDATE_MAX_ATTEMPTS + 1):
        try:
            group_schedule, color, group = await _fetch_group(
                browser,
                None,
                credentials,
                detect_group=True,
            )
            if not group:
                raise ScheduleUpdateError(
                    "в личном кабинете не удалось определить учебную группу"
                )
            return group_schedule, color, group
        except ScheduleCredentialsError:
            raise
        except Exception as exc:  # noqa: BLE001 - retries cover site failures
            last_error = exc
            LOGGER.warning(
                "Проверка нового аккаунта, попытка %s/%s, не удалась: %s",
                attempt,
                config.SCHEDULE_UPDATE_MAX_ATTEMPTS,
                exc,
            )
            if attempt < config.SCHEDULE_UPDATE_MAX_ATTEMPTS:
                await asyncio.sleep(config.SCHEDULE_UPDATE_RETRY_SECONDS * attempt)
    raise ScheduleUpdateError(f"не удалось проверить аккаунт: {last_error}")


def _atomic_json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=4)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _load_global_settings() -> dict[str, Any]:
    with config.GLOBAL_SETTINGS_FILE.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def _load_schedule_cache() -> dict[str, dict[str, Any]]:
    path = (
        config.SCHEDULE_FILE
        if config.SCHEDULE_FILE.exists()
        else config.INITIAL_SCHEDULE_FILE
    )
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ScheduleUpdateError(f"не удалось загрузить кеш расписания: {exc}") from exc
    if not isinstance(value, dict):
        raise ScheduleUpdateError("кеш расписания имеет некорректный формат")
    return value


def _persist_verified_account(
    group: str,
    credentials: dict[str, str],
    group_schedule: dict[str, dict[str, Any]],
) -> bool:
    settings = _load_global_settings()
    all_accounts = config.load_schedule_accounts(settings)
    runtime_accounts = config.load_runtime_schedule_accounts()
    account_added = group not in all_accounts

    cached_schedule = _load_schedule_cache()
    cached_schedule[group] = group_schedule
    _atomic_json_dump(config.SCHEDULE_FILE, cached_schedule)

    if account_added:
        runtime_accounts[group] = credentials
        _atomic_json_dump(config.SCHEDULE_ACCOUNTS_FILE, runtime_accounts)
        os.chmod(config.SCHEDULE_ACCOUNTS_FILE, 0o600)

    return account_added


async def register_schedule_account(
    login: str,
    password: str,
) -> AccountRegistrationResult:
    """Validate credentials, detect the group and persist a new account."""

    login = login.strip()
    password = password.strip()
    if not login or not password:
        raise ScheduleCredentialsError("логин и пароль не могут быть пустыми")
    if len(login) > 256 or len(password) > 256:
        raise ScheduleCredentialsError("логин или пароль слишком длинный")

    credentials = {"login": login, "password": password}
    async with async_playwright() as playwright:
        browser_type = getattr(playwright, config.SCHEDULE_BROWSER)
        browser = await browser_type.launch(headless=config.SCHEDULE_HEADLESS)
        try:
            group_schedule, _, group = await _verify_account_with_retries(
                browser,
                credentials,
            )
        finally:
            await browser.close()

    account_added = _persist_verified_account(
        group,
        credentials,
        group_schedule,
    )
    return AccountRegistrationResult(
        group=group,
        account_added=account_added,
    )


async def update_schedule() -> UpdateResult:
    """Fetch every configured group and replace cache only after full success."""

    settings = _load_global_settings()
    accounts = config.load_schedule_accounts(settings)
    if not accounts:
        raise ScheduleUpdateError(
            "не заданы аккаунты колледжа: заполните SCHEDULE_ACCOUNTS_JSON"
        )

    new_schedule: dict[str, dict[str, Any]] = {}
    detected_colors: list[str] = []

    async with async_playwright() as playwright:
        browser_type = getattr(playwright, config.SCHEDULE_BROWSER)
        browser = await browser_type.launch(headless=config.SCHEDULE_HEADLESS)
        try:
            for group, credentials in accounts.items():
                group_schedule, color = await _fetch_group_with_retries(
                    browser, group, credentials
                )
                new_schedule[group] = group_schedule
                if color:
                    detected_colors.append(color)
        finally:
            await browser.close()

    fallback_color = settings.get("references", {}).get("color", WHITE_WEEK)
    reference_color = detected_colors[0] if detected_colors else fallback_color
    if any(color != reference_color for color in detected_colors):
        LOGGER.warning("Страница показала разные цвета недели для разных аккаунтов")

    now = datetime.now(ZoneInfo(config.BOT_TIMEZONE))
    state = {
        "date": now.strftime("%d.%m.%Y"),
        "color": reference_color,
        "updated_at": now.isoformat(timespec="seconds"),
    }

    _atomic_json_dump(config.SCHEDULE_FILE, new_schedule)
    _atomic_json_dump(config.REFERENCE_FILE, state)
    return UpdateResult(
        groups=tuple(new_schedule),
        reference_date=state["date"],
        reference_color=state["color"],
        updated_at=state["updated_at"],
    )
