import asyncio

import pytest

from moduls.time import get_next_previous, get_this_weekday


def run(coroutine):
    return asyncio.run(coroutine)


def test_week_color_alternates() -> None:
    assert run(get_this_weekday("18.08.2025", "Белая неделя", "18.08.2025")) == (
        "ПН",
        "Белая неделя",
    )
    assert run(get_this_weekday("18.08.2025", "Белая неделя", "25.08.2025")) == (
        "ПН",
        "Зелёная неделя",
    )


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        ("previous", "21.08.2026"),
        ("p", "21.08.2026"),
        ("next", "23.08.2026"),
        ("n", "23.08.2026"),
        ("extra_previous", "15.08.2026"),
        ("e_p", "15.08.2026"),
        ("extra_next", "29.08.2026"),
        ("e_n", "29.08.2026"),
    ],
)
def test_date_navigation(direction: str, expected: str) -> None:
    assert run(get_next_previous("22.08.2026", direction)) == expected


def test_invalid_direction() -> None:
    with pytest.raises(ValueError):
        run(get_next_previous("22.08.2026", "somewhere"))
