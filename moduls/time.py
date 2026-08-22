"""Date helpers for schedule navigation and alternating week colors."""

from __future__ import annotations

import arrow

import config

WEEKDAYS = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
WHITE_WEEK = "Белая неделя"
GREEN_WEEK = "Зелёная неделя"


async def get_this_weekday(
    reference_date: str,
    reference_color: str,
    date: str | None = None,
) -> tuple[str, str]:
    """Return the weekday and alternating week color for a date."""

    date = date or arrow.now(config.BOT_TIMEZONE).format("DD.MM.YYYY")
    try:
        day = arrow.get(date, "DD.MM.YYYY", tzinfo=config.BOT_TIMEZONE)
        reference = arrow.get(reference_date, "DD.MM.YYYY", tzinfo=config.BOT_TIMEZONE)
    except (arrow.parser.ParserError, ValueError) as exc:
        raise ValueError("Неверный формат даты. Используйте ДД.ММ.ГГГГ") from exc

    weeks = (day.floor("week") - reference.floor("week")).days // 7
    if weeks % 2 == 0:
        color = reference_color
    else:
        color = GREEN_WEEK if reference_color == WHITE_WEEK else WHITE_WEEK
    return WEEKDAYS[day.weekday()], color


async def get_next_previous(date: str | None = None, direction: str = "") -> str:
    """Move the date by one day or one week in either direction."""

    date = date or arrow.now(config.BOT_TIMEZONE).format("DD.MM.YYYY")
    aliases = {
        "e_n": "extra_next",
        "n": "next",
        "p": "previous",
        "e_p": "extra_previous",
    }
    direction = aliases.get(direction, direction)
    shifts = {
        "extra_next": {"weeks": 1},
        "next": {"days": 1},
        "previous": {"days": -1},
        "extra_previous": {"weeks": -1},
    }
    if direction not in shifts:
        raise ValueError(f"Неизвестное направление: {direction}")

    try:
        current = arrow.get(date, "DD.MM.YYYY", tzinfo=config.BOT_TIMEZONE)
    except (arrow.parser.ParserError, ValueError) as exc:
        raise ValueError("Неверный формат даты. Используйте ДД.ММ.ГГГГ") from exc
    return current.shift(**shifts[direction]).format("DD.MM.YYYY")
