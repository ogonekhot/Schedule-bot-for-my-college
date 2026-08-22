import pytest

from moduls.schedule import (
    GREEN_WEEK,
    WHITE_WEEK,
    ScheduleUpdateError,
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
