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
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup, Tag
from playwright.async_api import (
    Page,
    Route,
    Response,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

import config
from moduls.backups import create_schedule_backup

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
TITLE_ACRONYMS = {
    "api": "API",
    "бд": "БД",
    "ис": "ИС",
    "лгту": "ЛГТУ",
    "ос": "ОС",
    "субд": "СУБД",
    "эвм": "ЭВМ",
    "css": "CSS",
    "html": "HTML",
    "js": "JS",
    "linux": "Linux",
    "php": "PHP",
    "sql": "SQL",
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


@dataclass(frozen=True)
class RecoveryResult:
    update: UpdateResult
    capture_dir: Path


@dataclass(frozen=True)
class ScheduleRequestDescriptor:
    endpoint_url: str
    schedule_url: str
    semester_id: str
    group_id: str


def normalize_lesson_title(value: str) -> str:
    """Fix accidental ALL-CAPS titles without damaging mixed-case names."""

    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    letters = [character for character in cleaned if character.isalpha()]
    if not letters or not all(character.isupper() for character in letters):
        return cleaned

    normalized = cleaned.lower()
    normalized = re.sub(
        r"[A-Za-zА-Яа-яЁё]+",
        lambda match: TITLE_ACRONYMS.get(match.group(0), match.group(0)),
        normalized,
    )
    if normalized and normalized[0].isalpha():
        normalized = normalized[0].upper() + normalized[1:]
    return normalized


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
            lesson["title"] = normalize_lesson_title(lines[0])
            lesson["teacher"] = lines[-1]
        elif len(lines) == 2:
            lesson["title"] = normalize_lesson_title(lines[0])
            lesson["teacher"] = lines[1]
        else:
            lesson["title"] = normalize_lesson_title(lines[0])

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


def _schedule_request_descriptor_from_metadata(
    metadata: dict[str, Any],
) -> ScheduleRequestDescriptor:
    post_data = str(metadata.get("post_data", ""))
    values = parse_qs(post_data, keep_blank_values=True)
    semester_id = values.get("semester", [""])[0].strip()
    group_id = values.get("group", [""])[0].strip()
    student_schedule = values.get("student_schedule", [""])[0].strip()
    endpoint_url = str(metadata.get("url", "")).strip()
    schedule_url = str(
        metadata.get("page_url") or config.COLLEGE_SCHEDULE_URL
    ).strip()

    if (
        student_schedule != "1"
        or not semester_id
        or not group_id
        or not endpoint_url
        or not schedule_url
    ):
        raise ScheduleUpdateError(
            "не удалось определить ID семестра и группы из AJAX-запроса"
        )
    return ScheduleRequestDescriptor(
        endpoint_url=endpoint_url,
        schedule_url=schedule_url,
        semester_id=semester_id,
        group_id=group_id,
    )


def _load_schedule_request_cache() -> dict[str, dict[str, str]]:
    path = config.SCHEDULE_SOURCE_CACHE_FILE
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Не удалось прочитать кеш ID расписания %s", path)
        return {}
    return value if isinstance(value, dict) else {}


def _cached_schedule_request(group: str) -> ScheduleRequestDescriptor | None:
    value = _load_schedule_request_cache().get(group)
    if not isinstance(value, dict):
        return None
    try:
        descriptor = ScheduleRequestDescriptor(
            endpoint_url=str(value["endpoint_url"]),
            schedule_url=str(value["schedule_url"]),
            semester_id=str(value["semester_id"]),
            group_id=str(value["group_id"]),
        )
    except (KeyError, TypeError, ValueError):
        return None

    configured_url = config.COLLEGE_SCHEDULE_URL.rstrip("/")
    cached_configured_url = str(
        value.get("configured_schedule_url", descriptor.schedule_url)
    ).rstrip("/")
    if cached_configured_url != configured_url:
        return None
    return descriptor


def _remember_schedule_request(
    group: str,
    descriptor: ScheduleRequestDescriptor,
) -> None:
    cache = _load_schedule_request_cache()
    cache[group] = {
        "endpoint_url": descriptor.endpoint_url,
        "schedule_url": descriptor.schedule_url,
        "semester_id": descriptor.semester_id,
        "group_id": descriptor.group_id,
        "configured_schedule_url": config.COLLEGE_SCHEDULE_URL,
        "discovered_at": datetime.now(ZoneInfo(config.BOT_TIMEZONE)).isoformat(
            timespec="seconds"
        ),
    }
    _atomic_json_dump(config.SCHEDULE_SOURCE_CACHE_FILE, cache)
    os.chmod(config.SCHEDULE_SOURCE_CACHE_FILE, 0o600)


async def _request_schedule_directly(
    session: aiohttp.ClientSession,
    descriptor: ScheduleRequestDescriptor,
) -> tuple[str, str, str | None, dict[str, Any]]:
    """Fetch the schedule through the same lightweight AJAX endpoint as the UI."""

    async with session.get(
        descriptor.schedule_url,
        allow_redirects=True,
    ) as response:
        page_body = await response.read()
        page_url = str(response.url)
        if response.status != 200:
            raise ScheduleUpdateError(
                f"страница расписания вернула HTTP {response.status}"
            )

    page_html = _decode_body(page_body)
    if BeautifulSoup(page_html, "html.parser").select_one(
        "input[name='LOGIN']"
    ) is not None:
        raise ScheduleUpdateError(
            "PHP-сессия потерялась при переходе к расписанию"
        )

    endpoint_parts = urlsplit(descriptor.endpoint_url)
    origin = f"{endpoint_parts.scheme}://{endpoint_parts.netloc}"
    payload = {
        "student_schedule": "1",
        "semester": descriptor.semester_id,
        "group": descriptor.group_id,
    }
    async with session.post(
        descriptor.endpoint_url,
        data=payload,
        headers={
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": origin,
            "Referer": page_url,
        },
        allow_redirects=False,
    ) as response:
        ajax_body = await response.read()
        ajax_html = _decode_body(ajax_body)
        if response.status != 200:
            raise ScheduleUpdateError(
                f"AJAX расписания вернул HTTP {response.status}"
            )

    if "<tbody" not in ajax_html.casefold():
        raise ScheduleUpdateError(
            "сохранённые ID расписания устарели или сайт вернул пустой AJAX-ответ"
        )

    metadata = {
        "url": descriptor.endpoint_url,
        "method": "POST",
        "post_data": (
            "student_schedule=1"
            f"&semester={descriptor.semester_id}"
            f"&group={descriptor.group_id}"
        ),
        "headers": {
            "origin": origin,
            "referer": page_url,
            "x-requested-with": "XMLHttpRequest",
        },
        "response_status": 200,
        "page_url": descriptor.schedule_url,
        "transport": "direct-aiohttp",
    }
    return (
        ajax_html,
        page_html,
        _read_week_color_html(page_html),
        metadata,
    )


def recovery_is_complete(group: str | None = None) -> bool:
    """Return whether the configured one-shot recovery has already succeeded."""

    target_group = group or config.SCHEDULE_RECOVERY_GROUP
    if not target_group or not config.SCHEDULE_RECOVERY_STATE_FILE.exists():
        return False
    try:
        with config.SCHEDULE_RECOVERY_STATE_FILE.open(
            "r", encoding="utf-8-sig"
        ) as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        state.get("completed") is True and state.get("group") == target_group
    )


def _redact_recovery_value(value: str, credentials: dict[str, str]) -> str:
    result = value
    for secret in (credentials.get("login", ""), credentials.get("password", "")):
        if secret:
            result = result.replace(secret, "<hidden>")
    return result


async def _authenticate_recovery_session(
    credentials: dict[str, str],
) -> tuple[aiohttp.ClientSession, str, str]:
    """Perform the lightweight AJAX login used by the college page."""

    timeout = aiohttp.ClientTimeout(
        total=config.SCHEDULE_RECOVERY_HTTP_TIMEOUT_SECONDS,
        connect=min(10, config.SCHEDULE_RECOVERY_HTTP_TIMEOUT_SECONDS),
    )
    session = aiohttp.ClientSession(
        timeout=timeout,
        cookie_jar=aiohttp.CookieJar(unsafe=True),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9",
        },
    )
    try:
        async with session.get(
            config.COLLEGE_LOGIN_URL,
            allow_redirects=True,
        ) as response:
            login_body = await response.read()
            login_url = str(response.url)
            if response.status != 200:
                raise ScheduleUpdateError(
                    f"форма входа вернула HTTP {response.status}"
                )

        login_html = _decode_body(login_body)
        soup = BeautifulSoup(login_html, "html.parser")
        form = soup.select_one("form#auth_form")
        if form is None:
            raise ScheduleUpdateError("на странице не найдена форма авторизации")

        form_data = {
            element["name"]: element.get("value", "")
            for element in form.select("input[name]")
        }
        form_data.update(
            {
                "AUTH_FORM": "1",
                "LOGIN": credentials["login"],
                "PASSWORD": credentials["password"],
            }
        )
        action_url = urljoin(login_url, form.get("action") or "/index.php")
        action_parts = urlsplit(action_url)
        origin = f"{action_parts.scheme}://{action_parts.netloc}"

        async with session.post(
            action_url,
            data=form_data,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": origin,
                "Referer": login_url,
            },
            allow_redirects=False,
        ) as response:
            auth_body = await response.read()
            auth_text = _decode_body(auth_body).lstrip("\ufeff").strip()
            if response.status != 200:
                raise ScheduleUpdateError(
                    f"авторизация вернула HTTP {response.status}"
                )

        try:
            auth_result = json.loads(auth_text)
        except json.JSONDecodeError as exc:
            message = re.sub(
                r"\s+",
                " ",
                BeautifulSoup(auth_text, "html.parser").get_text(" ", strip=True),
            )
            message = _redact_recovery_value(message, credentials)[:160]
            raise ScheduleUpdateError(
                "авторизация ЛГТУ ещё не восстановилась"
                + (f": {message}" if message else "")
            ) from exc

        if not isinstance(auth_result, dict):
            raise ScheduleUpdateError("авторизация вернула неожиданный JSON")
        if auth_result.get("ERRORS"):
            errors = _redact_recovery_value(
                json.dumps(auth_result["ERRORS"], ensure_ascii=False),
                credentials,
            )
            raise ScheduleCredentialsError(f"сайт отклонил аккаунт: {errors[:200]}")

        redirect_value = str(auth_result.get("REDIRECT_URL", "")).strip()
        if not redirect_value:
            raise ScheduleUpdateError(
                "авторизация не вернула REDIRECT_URL; повторим позже"
            )
        redirect_url = urljoin(action_url, redirect_value)

        async with session.get(redirect_url, allow_redirects=True) as response:
            authenticated_body = await response.read()
            authenticated_url = str(response.url)
            if response.status != 200:
                raise ScheduleUpdateError(
                    f"личный кабинет вернул HTTP {response.status}"
                )

        authenticated_html = _decode_body(authenticated_body)
        authenticated_soup = BeautifulSoup(authenticated_html, "html.parser")
        if authenticated_soup.select_one("input[name='LOGIN']") is not None:
            raise ScheduleUpdateError(
                "после REDIRECT_URL сайт снова вернул форму входа"
            )
        return session, authenticated_html, authenticated_url
    except BaseException:
        await session.close()
        raise


def _recovery_playwright_cookies(
    session: aiohttp.ClientSession,
) -> list[dict[str, Any]]:
    login_host = urlsplit(config.COLLEGE_LOGIN_URL).hostname or ""
    cookies: list[dict[str, Any]] = []
    for cookie in session.cookie_jar:
        domain = cookie["domain"] or login_host
        if not domain:
            continue
        cookies.append(
            {
                "name": cookie.key,
                "value": cookie.value,
                "domain": domain,
                "path": cookie["path"] or "/",
                "secure": bool(cookie["secure"]),
            }
        )
    return cookies


async def _recovery_route(route: Route) -> None:
    request = route.request
    path = urlsplit(request.url).path.casefold()
    # The college schedule bootstrap depends on main-1.69.js.  It used to
    # stall while the site was unhealthy, but blocking it also prevents the
    # authenticated page from issuing its schedule AJAX request.
    blocked_scripts = (
        "/bootstrap.min.js",
        "/jquery.maskedinput.min.js",
        "/ba.js",
    )
    if request.resource_type in {"image", "media", "font", "stylesheet"}:
        await route.abort()
    elif request.resource_type == "script" and path.endswith(blocked_scripts):
        await route.abort()
    else:
        await route.continue_()


async def _capture_recovery_schedule(
    session: aiohttp.ClientSession,
    credentials: dict[str, str],
) -> tuple[str, str, str | None, dict[str, Any]]:
    """Reuse the authenticated PHP session and capture the schedule AJAX call."""

    timeout_ms = config.SCHEDULE_PAGE_TIMEOUT_SECONDS * 1000
    async with async_playwright() as playwright:
        browser_type = getattr(playwright, config.SCHEDULE_BROWSER)
        browser = await browser_type.launch(headless=config.SCHEDULE_HEADLESS)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        cookies = _recovery_playwright_cookies(session)
        if cookies:
            await context.add_cookies(cookies)
        await page.route("**/*", _recovery_route)

        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[tuple[str, dict[str, Any]]] = (
            loop.create_future()
        )

        async def capture(response: Response) -> None:
            if "ajax.handler.php" not in response.url or response_future.done():
                return
            try:
                body = _decode_body(await response.body())
                if "<tbody" not in body.casefold():
                    return
                request = response.request
                safe_headers = {
                    key: _redact_recovery_value(value, credentials)
                    for key, value in request.headers.items()
                    if key.casefold() not in {"authorization", "cookie"}
                }
                metadata = {
                    "url": request.url,
                    "method": request.method,
                    "post_data": _redact_recovery_value(
                        request.post_data or "", credentials
                    ),
                    "headers": safe_headers,
                    "response_status": response.status,
                }
                response_future.set_result((body, metadata))
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("Не удалось захватить AJAX расписания: %s", exc)

        page.on("response", capture)
        try:
            try:
                await page.goto(
                    config.COLLEGE_SCHEDULE_URL,
                    wait_until="commit",
                    timeout=timeout_ms,
                )
            except PlaywrightTimeoutError:
                LOGGER.debug(
                    "Переход к расписанию не завершился, ожидаем AJAX-ответ"
                )

            try:
                ajax_html, request_metadata = await asyncio.wait_for(
                    response_future,
                    timeout=config.SCHEDULE_PAGE_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                current_html = await page.content()
                if BeautifulSoup(current_html, "html.parser").select_one(
                    "input[name='LOGIN']"
                ) is not None:
                    raise ScheduleUpdateError(
                        "PHP-сессия потерялась при переходе к расписанию"
                    ) from exc
                raise ScheduleUpdateError(
                    "вход сработал, но AJAX расписания пока не ответил"
                ) from exc

            page_html = await page.content()
            week_color = await _read_week_color(page)
            request_metadata["page_url"] = page.url
            request_metadata["scripts"] = [
                urljoin(page.url, script.get("src", ""))
                for script in BeautifulSoup(page_html, "html.parser").select(
                    "script[src]"
                )
            ]
            return ajax_html, page_html, week_color, request_metadata
        finally:
            page.remove_listener("response", capture)
            if not response_future.done():
                response_future.cancel()
            await context.close()
            await browser.close()


def _atomic_text_dump(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(value)
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


def _write_recovery_capture(
    group: str,
    credentials: dict[str, str],
    authenticated_html: str,
    authenticated_url: str,
    schedule_page_html: str,
    ajax_html: str,
    request_metadata: dict[str, Any],
) -> Path:
    now = datetime.now(ZoneInfo(config.BOT_TIMEZONE))
    safe_group = re.sub(r"[^\w.-]+", "-", group, flags=re.UNICODE).strip("-")
    capture_dir = config.SCHEDULE_RECOVERY_CAPTURE_DIR / (
        f"{now.strftime('%Y%m%d-%H%M%S')}-{safe_group}"
    )
    capture_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(capture_dir, 0o700)

    safe_metadata = json.loads(
        _redact_recovery_value(
            json.dumps(request_metadata, ensure_ascii=False), credentials
        )
    )
    manifest = {
        "group": group,
        "captured_at": now.isoformat(timespec="seconds"),
        "authenticated_url": authenticated_url,
        "schedule_url": config.COLLEGE_SCHEDULE_URL,
        "cookies_saved": False,
    }
    _atomic_text_dump(capture_dir / "authenticated-page.html", authenticated_html)
    _atomic_text_dump(capture_dir / "schedule-page.html", schedule_page_html)
    _atomic_text_dump(capture_dir / "schedule-response.html", ajax_html)
    _atomic_text_dump(
        capture_dir / "request.json",
        json.dumps(safe_metadata, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_text_dump(
        capture_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return capture_dir


def _persist_recovered_group(
    group: str,
    group_schedule: dict[str, dict[str, Any]],
    week_color: str | None,
) -> UpdateResult:
    settings = _load_global_settings()
    cached_schedule = _load_schedule_cache()
    cached_schedule[group] = group_schedule

    fallback_color = settings.get("references", {}).get("color", WHITE_WEEK)
    if config.REFERENCE_FILE.exists():
        try:
            with config.REFERENCE_FILE.open("r", encoding="utf-8-sig") as file:
                fallback_color = json.load(file).get("color", fallback_color)
        except (OSError, json.JSONDecodeError):
            pass

    now = datetime.now(ZoneInfo(config.BOT_TIMEZONE))
    state = {
        "date": now.strftime("%d.%m.%Y"),
        "color": week_color or fallback_color,
        "updated_at": now.isoformat(timespec="seconds"),
    }
    create_schedule_backup("schedule_recovery")
    _atomic_json_dump(config.SCHEDULE_FILE, cached_schedule)
    _atomic_json_dump(config.REFERENCE_FILE, state)
    return UpdateResult(
        groups=(group,),
        reference_date=state["date"],
        reference_color=state["color"],
        updated_at=state["updated_at"],
    )


async def recover_priority_group() -> RecoveryResult:
    """Try the configured group once and permanently record the first success."""

    group = config.SCHEDULE_RECOVERY_GROUP
    if not group:
        raise ScheduleUpdateError("не задана SCHEDULE_RECOVERY_GROUP")
    if recovery_is_complete(group):
        raise ScheduleUpdateError(f"восстановление группы {group} уже завершено")

    settings = _load_global_settings()
    accounts = config.load_schedule_accounts(settings)
    credentials = accounts.get(group)
    if credentials is None:
        raise ScheduleUpdateError(f"для группы {group} не найден аккаунт ЛГТУ")

    session, authenticated_html, authenticated_url = (
        await _authenticate_recovery_session(credentials)
    )
    try:
        ajax_html, page_html, week_color, request_metadata = (
            await _capture_recovery_schedule(session, credentials)
        )
    finally:
        await session.close()

    descriptor = _schedule_request_descriptor_from_metadata(request_metadata)
    group_schedule = parse_schedule_html(ajax_html)
    _remember_schedule_request(group, descriptor)
    capture_dir = _write_recovery_capture(
        group,
        credentials,
        authenticated_html,
        authenticated_url,
        page_html,
        ajax_html,
        request_metadata,
    )
    update = _persist_recovered_group(group, group_schedule, week_color)
    _atomic_json_dump(
        config.SCHEDULE_RECOVERY_STATE_FILE,
        {
            "completed": True,
            "group": group,
            "completed_at": update.updated_at,
            "capture_dir": str(capture_dir),
        },
    )
    os.chmod(config.SCHEDULE_RECOVERY_STATE_FILE, 0o600)
    return RecoveryResult(update=update, capture_dir=capture_dir)


def _read_week_color_html(document: str) -> str | None:
    soup = BeautifulSoup(document, "html.parser")
    for alert in soup.select("div[role=alert]"):
        normalized = alert.get_text(" ", strip=True).casefold().replace("ё", "е")
        if "белая" in normalized:
            return WHITE_WEEK
        if "зеленая" in normalized:
            return GREEN_WEEK
    return None


async def _read_week_color(page: Page) -> str | None:
    return _read_week_color_html(await page.content())


async def _fetch_group(
    group: str | None,
    credentials: dict[str, str],
    *,
    detect_group: bool = False,
) -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
    session, authenticated_html, authenticated_url = (
        await _authenticate_recovery_session(credentials)
    )
    try:
        descriptor = _cached_schedule_request(group) if group else None
        if descriptor is not None:
            try:
                ajax_html, page_html, week_color, _ = (
                    await _request_schedule_directly(session, descriptor)
                )
                detected_group = (
                    detect_group_name(
                        authenticated_html,
                        page_html,
                        ajax_html,
                    )
                    if detect_group
                    else None
                )
                return (
                    parse_schedule_html(ajax_html),
                    week_color,
                    detected_group,
                )
            except ScheduleCredentialsError:
                raise
            except Exception as exc:  # noqa: BLE001 - rediscovery is the fallback
                LOGGER.warning(
                    "Прямой AJAX для группы %s не сработал, "
                    "обновляем ID через Playwright: %s",
                    group,
                    exc,
                )

        ajax_html, page_html, week_color, request_metadata = (
            await _capture_recovery_schedule(session, credentials)
        )
        descriptor = _schedule_request_descriptor_from_metadata(request_metadata)
        detected_group = (
            detect_group_name(
                authenticated_html,
                page_html,
                ajax_html,
            )
            if detect_group
            else None
        )
        group_schedule = parse_schedule_html(ajax_html)
        cache_group = group or detected_group
        if cache_group:
            _remember_schedule_request(cache_group, descriptor)
        return group_schedule, week_color, detected_group
    finally:
        await session.close()


async def _fetch_group_with_retries(
    group: str, credentials: dict[str, str]
) -> tuple[dict[str, dict[str, Any]], str | None]:
    last_error: Exception | None = None
    for attempt in range(1, config.SCHEDULE_UPDATE_MAX_ATTEMPTS + 1):
        try:
            group_schedule, color, _ = await _fetch_group(
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
    credentials: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], str | None, str]:
    last_error: Exception | None = None
    for attempt in range(1, config.SCHEDULE_UPDATE_MAX_ATTEMPTS + 1):
        try:
            group_schedule, color, group = await _fetch_group(
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
    create_schedule_backup("group_registration")
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
    group_schedule, _, group = await _verify_account_with_retries(credentials)

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

    for group, credentials in accounts.items():
        group_schedule, color = await _fetch_group_with_retries(
            group, credentials
        )
        new_schedule[group] = group_schedule
        if color:
            detected_colors.append(color)

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

    create_schedule_backup("schedule_update")
    _atomic_json_dump(config.SCHEDULE_FILE, new_schedule)
    _atomic_json_dump(config.REFERENCE_FILE, state)
    return UpdateResult(
        groups=tuple(new_schedule),
        reference_date=state["date"],
        reference_color=state["color"],
        updated_at=state["updated_at"],
    )
