import asyncio
import json
import stat
from types import SimpleNamespace

import pytest

import config
from moduls.schedule import (
    GREEN_WEEK,
    WHITE_WEEK,
    ScheduleRequestDescriptor,
    ScheduleUpdateError,
    _cached_schedule_request,
    _persist_verified_account,
    _persist_recovered_group,
    _recovery_route,
    _redact_recovery_value,
    _remember_schedule_request,
    _request_schedule_directly,
    _schedule_request_descriptor_from_metadata,
    detect_group_name,
    normalize_lesson_title,
    parse_schedule_html,
    recovery_is_complete,
)

SCHEDULE_HTML = """
<table>
  <tbody>
    <tr>
      <td>ПН</td>
      <td>08:00 - 09:30</td>
      <td>Математика<br>подгруппа 1<br>Иванов Иван Иванович</td>
      <td>101<br>лек.</td>
      <td class="bgGreen">Физика<br>подгруппа 1<br>Петров Пётр Петрович</td>
      <td class="bgGreen">9-208<br>лаб.</td>
    </tr>
    <tr>
      <td>09:40 – 11:10</td>
      <td>ИНФОРМАТИКА</td>
      <td>305<br>пр.</td>
    </tr>
    <tr>
      <td>ВТ</td>
      <td>11:20 — 12:50</td>
      <td>История<br>Сидоров Сидор Сидорович</td>
    </tr>
  </tbody>
</table>
"""


def test_parse_schedule_html() -> None:
    result = parse_schedule_html(SCHEDULE_HTML)

    first = result["ПН"]["1"]
    assert first["time"] == {"start": "08:00", "end": "09:30"}
    assert first[WHITE_WEEK] == {
        "title": "Математика",
        "teacher": "Иванов Иван Иванович",
        "room": "101",
        "type": "лекция",
    }
    assert first[GREEN_WEEK] == {
        "title": "Физика",
        "teacher": "Петров Пётр Петрович",
        "room": "9-208",
        "type": "лабораторная",
    }
    assert result["ПН"]["2"][WHITE_WEEK]["title"] == "Информатика"
    assert result["ПН"]["2"][WHITE_WEEK]["type"] == "практика"
    assert result["ВТ"]["1"][WHITE_WEEK]["teacher"] == "Сидоров Сидор Сидорович"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("ЭЛЕКТРОТЕХНИКА", "Электротехника"),
        (
            "АДМИНИСТРИРОВАНИЕ В ОС LINUX",
            "Администрирование в ОС Linux",
        ),
        ("Основы SQL", "Основы SQL"),
    ],
)
def test_normalize_lesson_title(source: str, expected: str) -> None:
    assert normalize_lesson_title(source) == expected


@pytest.mark.parametrize("html", ["", "<div>сайт временно недоступен</div>"])
def test_parse_rejects_invalid_response(html: str) -> None:
    with pytest.raises(ScheduleUpdateError):
        parse_schedule_html(html)

def test_detect_group_name_from_profile_label() -> None:
    html = "<main><p>Учебная группа: Т9-ИП-24-3</p></main>"

    assert detect_group_name(html) == "Т9-ИП-24-3"


def test_detect_group_name_from_unlabelled_page() -> None:
    html = "<div>Студент группы</div><strong>т9 - ип - 24 - 4</strong>"

    assert detect_group_name(html) == "Т9-ИП-24-4"


def test_detect_group_name_rejects_unknown_page() -> None:
    with pytest.raises(ScheduleUpdateError):
        detect_group_name("<main>Личный кабинет студента</main>")


def test_persist_new_group_account(monkeypatch, tmp_path) -> None:
    settings_file = tmp_path / "global.json"
    initial_schedule_file = tmp_path / "initial-schedule.json"
    schedule_file = tmp_path / "schedule.json"
    accounts_file = tmp_path / "accounts.json"

    settings_file.write_text(
        json.dumps(
            {
                "accounts": {},
                "references": {
                    "date": "01.01.2026",
                    "color": "Белая неделя",
                },
            }
        ),
        encoding="utf-8",
    )
    initial_schedule_file.write_text(
        json.dumps({"Т9-ИП-24-1": {"ПН": {}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "GLOBAL_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "INITIAL_SCHEDULE_FILE", initial_schedule_file)
    monkeypatch.setattr(config, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(config, "REFERENCE_FILE", tmp_path / "reference.json")
    monkeypatch.setattr(config, "SCHEDULE_ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(config, "SCHEDULE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "SCHEDULE_BACKUP_LIMIT", 10)
    monkeypatch.delenv("SCHEDULE_ACCOUNTS_JSON", raising=False)

    added = _persist_verified_account(
        "Т9-ИП-24-3",
        {"login": "student", "password": "secret"},
        {"ПН": {"1": {}}},
    )

    assert added is True
    assert json.loads(accounts_file.read_text(encoding="utf-8")) == {
        "Т9-ИП-24-3": {"login": "student", "password": "secret"}
    }
    assert "Т9-ИП-24-3" in json.loads(
        schedule_file.read_text(encoding="utf-8")
    )
    assert stat.S_IMODE(accounts_file.stat().st_mode) == 0o600


def test_existing_group_credentials_are_not_saved(monkeypatch, tmp_path) -> None:
    settings_file = tmp_path / "global.json"
    initial_schedule_file = tmp_path / "initial-schedule.json"
    schedule_file = tmp_path / "schedule.json"
    accounts_file = tmp_path / "accounts.json"

    settings_file.write_text(
        json.dumps(
            {
                "accounts": {
                    "Т9-ИП-24-1": {
                        "login": "configured",
                        "password": "configured-secret",
                    }
                },
                "references": {
                    "date": "01.01.2026",
                    "color": "Белая неделя",
                },
            }
        ),
        encoding="utf-8",
    )
    initial_schedule_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "GLOBAL_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "INITIAL_SCHEDULE_FILE", initial_schedule_file)
    monkeypatch.setattr(config, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(config, "REFERENCE_FILE", tmp_path / "reference.json")
    monkeypatch.setattr(config, "SCHEDULE_ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(config, "SCHEDULE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "SCHEDULE_BACKUP_LIMIT", 10)
    monkeypatch.delenv("SCHEDULE_ACCOUNTS_JSON", raising=False)

    added = _persist_verified_account(
        "Т9-ИП-24-1",
        {"login": "user-input", "password": "user-secret"},
        {"ПН": {}},
    )

    assert added is False
    assert accounts_file.exists() is False


def test_recovered_group_preserves_other_cached_groups(monkeypatch, tmp_path) -> None:
    settings_file = tmp_path / "global.json"
    initial_schedule_file = tmp_path / "initial-schedule.json"
    schedule_file = tmp_path / "schedule.json"
    reference_file = tmp_path / "reference.json"

    settings_file.write_text(
        json.dumps(
            {
                "accounts": {},
                "references": {
                    "date": "01.01.2026",
                    "color": WHITE_WEEK,
                },
            }
        ),
        encoding="utf-8",
    )
    initial_schedule_file.write_text(
        json.dumps({"Т9-ИП-24-2": {"ПН": {"1": {"old": True}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "GLOBAL_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "INITIAL_SCHEDULE_FILE", initial_schedule_file)
    monkeypatch.setattr(config, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(config, "REFERENCE_FILE", reference_file)
    monkeypatch.setattr(config, "SCHEDULE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "SCHEDULE_BACKUP_LIMIT", 10)

    result = _persist_recovered_group(
        "Т9-ИП-24-1",
        {"ПН": {"1": {"new": True}}},
        GREEN_WEEK,
    )

    saved = json.loads(schedule_file.read_text(encoding="utf-8"))
    assert list(saved) == ["Т9-ИП-24-2", "Т9-ИП-24-1"]
    assert saved["Т9-ИП-24-2"]["ПН"]["1"] == {"old": True}
    assert saved["Т9-ИП-24-1"]["ПН"]["1"] == {"new": True}
    assert result.groups == ("Т9-ИП-24-1",)
    assert json.loads(reference_file.read_text(encoding="utf-8"))["color"] == GREEN_WEEK


def test_recovery_state_must_match_target_group(monkeypatch, tmp_path) -> None:
    state_file = tmp_path / "recovery-state.json"
    state_file.write_text(
        json.dumps({"completed": True, "group": "Т9-ИП-24-1"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SCHEDULE_RECOVERY_STATE_FILE", state_file)
    monkeypatch.setattr(config, "SCHEDULE_RECOVERY_GROUP", "Т9-ИП-24-1")

    assert recovery_is_complete() is True
    assert recovery_is_complete("Т9-ИП-24-2") is False


def test_recovery_redaction_hides_both_credentials() -> None:
    assert _redact_recovery_value(
        "LOGIN=student&PASSWORD=very-secret",
        {"login": "student", "password": "very-secret"},
    ) == "LOGIN=<hidden>&PASSWORD=<hidden>"


class _FakeHttpResponse:
    def __init__(self, body: str, url: str) -> None:
        self._body = body.encode("utf-8")
        self.url = url
        self.status = 200

    async def read(self) -> bytes:
        return self._body


class _FakeRequestContext:
    def __init__(self, response: _FakeHttpResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeHttpResponse:
        return self.response

    async def __aexit__(self, *_args) -> None:
        return None


class _FakeHttpSession:
    def __init__(self) -> None:
        self.post_kwargs = None

    def get(self, url: str, **_kwargs) -> _FakeRequestContext:
        page = '<div role="alert">Сейчас зелёная неделя</div>'
        return _FakeRequestContext(_FakeHttpResponse(page, url))

    def post(self, url: str, **kwargs) -> _FakeRequestContext:
        self.post_kwargs = {"url": url, **kwargs}
        return _FakeRequestContext(_FakeHttpResponse(SCHEDULE_HTML, url))


def test_schedule_request_descriptor_and_cache(monkeypatch, tmp_path) -> None:
    schedule_url = "http://lk.stu.lipetsk.ru/education/0/5:143027436/"
    descriptor = _schedule_request_descriptor_from_metadata(
        {
            "url": "http://lk.stu.lipetsk.ru/ajax.handler.php",
            "page_url": schedule_url,
            "post_data": (
                "student_schedule=1&semester=5%3A143027436"
                "&group=5%3A130743009"
            ),
        }
    )

    assert descriptor == ScheduleRequestDescriptor(
        endpoint_url="http://lk.stu.lipetsk.ru/ajax.handler.php",
        schedule_url=schedule_url,
        semester_id="5:143027436",
        group_id="5:130743009",
    )

    cache_file = tmp_path / "schedule-source-cache.json"
    monkeypatch.setattr(config, "SCHEDULE_SOURCE_CACHE_FILE", cache_file)
    monkeypatch.setattr(config, "COLLEGE_SCHEDULE_URL", schedule_url)
    monkeypatch.setattr(config, "BOT_TIMEZONE", "Europe/Moscow")
    _remember_schedule_request("Т9-ИП-24-1", descriptor)

    assert _cached_schedule_request("Т9-ИП-24-1") == descriptor
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600

    monkeypatch.setattr(
        config,
        "COLLEGE_SCHEDULE_URL",
        "http://lk.stu.lipetsk.ru/education/0/next-semester/",
    )
    assert _cached_schedule_request("Т9-ИП-24-1") is None


def test_direct_schedule_request_uses_discovered_ids() -> None:
    descriptor = ScheduleRequestDescriptor(
        endpoint_url="http://lk.stu.lipetsk.ru/ajax.handler.php",
        schedule_url="http://lk.stu.lipetsk.ru/education/0/5:143027436/",
        semester_id="5:143027436",
        group_id="5:130743009",
    )
    session = _FakeHttpSession()

    ajax_html, _page_html, color, metadata = asyncio.run(
        _request_schedule_directly(session, descriptor)
    )

    assert "<tbody>" in ajax_html
    assert color == GREEN_WEEK
    assert session.post_kwargs["data"] == {
        "student_schedule": "1",
        "semester": "5:143027436",
        "group": "5:130743009",
    }
    assert metadata["transport"] == "direct-aiohttp"


class _FakeRecoveryRoute:
    def __init__(self, url: str, resource_type: str = "script") -> None:
        self.request = SimpleNamespace(url=url, resource_type=resource_type)
        self.action: str | None = None

    async def abort(self) -> None:
        self.action = "abort"

    async def continue_(self) -> None:
        self.action = "continue"


def test_recovery_route_allows_required_schedule_bootstrap() -> None:
    route = _FakeRecoveryRoute(
        "http://lk.stu.lipetsk.ru/local/templates/main/assets/js/main-1.69.js"
    )

    asyncio.run(_recovery_route(route))

    assert route.action == "continue"


def test_recovery_route_still_blocks_nonessential_bootstrap() -> None:
    route = _FakeRecoveryRoute(
        "http://lk.stu.lipetsk.ru/local/templates/main/assets/js/bootstrap.min.js"
    )

    asyncio.run(_recovery_route(route))

    assert route.action == "abort"
