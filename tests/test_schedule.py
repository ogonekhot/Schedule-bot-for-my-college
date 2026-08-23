import json
import stat

import pytest

import config
from moduls.schedule import (
    GREEN_WEEK,
    WHITE_WEEK,
    ScheduleUpdateError,
    _persist_verified_account,
    detect_group_name,
    parse_schedule_html,
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
      <td>Информатика</td>
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
    assert result["ПН"]["2"][WHITE_WEEK]["type"] == "практика"
    assert result["ВТ"]["1"][WHITE_WEEK]["teacher"] == "Сидоров Сидор Сидорович"


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
        json.dumps({"accounts": {}, "references": {}}),
        encoding="utf-8",
    )
    initial_schedule_file.write_text(
        json.dumps({"Т9-ИП-24-1": {"ПН": {}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "GLOBAL_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "INITIAL_SCHEDULE_FILE", initial_schedule_file)
    monkeypatch.setattr(config, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(config, "SCHEDULE_ACCOUNTS_FILE", accounts_file)
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
                "references": {},
            }
        ),
        encoding="utf-8",
    )
    initial_schedule_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "GLOBAL_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "INITIAL_SCHEDULE_FILE", initial_schedule_file)
    monkeypatch.setattr(config, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(config, "SCHEDULE_ACCOUNTS_FILE", accounts_file)
    monkeypatch.delenv("SCHEDULE_ACCOUNTS_JSON", raising=False)

    added = _persist_verified_account(
        "Т9-ИП-24-1",
        {"login": "user-input", "password": "user-secret"},
        {"ПН": {}},
    )

    assert added is False
    assert accounts_file.exists() is False

