import asyncio

import moduls.broadcast as broadcast_module
from moduls.broadcast import copy_message_to_users


class FakeBot:
    def __init__(self) -> None:
        self.copies: list[tuple[int, int, int]] = []

    async def copy_message(
        self,
        *,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
    ) -> None:
        self.copies.append((chat_id, from_chat_id, message_id))


def test_copy_message_to_unique_users() -> None:
    bot = FakeBot()

    result = asyncio.run(
        copy_message_to_users(
            bot,
            [101, 202, 101],
            source_chat_id=303,
            source_message_id=404,
            delay_seconds=0,
        )
    )

    assert bot.copies == [
        (101, 303, 404),
        (202, 303, 404),
    ]
    assert result.total == 2
    assert result.delivered == 2
    assert result.blocked == 0
    assert result.failed == 0
    assert result.blocked_recipients == ()


def test_empty_broadcast() -> None:
    bot = FakeBot()

    result = asyncio.run(
        copy_message_to_users(
            bot,
            [],
            source_chat_id=303,
            source_message_id=404,
            delay_seconds=0,
        )
    )

    assert bot.copies == []
    assert result.total == 0
    assert result.delivered == 0

def test_blocked_recipient_is_reported(monkeypatch) -> None:
    class FakeForbiddenError(Exception):
        pass

    class PartiallyBlockedBot:
        async def copy_message(
            self,
            *,
            chat_id: int,
            from_chat_id: int,
            message_id: int,
        ) -> None:
            if chat_id == 202:
                raise FakeForbiddenError

    monkeypatch.setattr(
        broadcast_module,
        "TelegramForbiddenError",
        FakeForbiddenError,
    )

    result = asyncio.run(
        copy_message_to_users(
            PartiallyBlockedBot(),
            [101, 202, 303],
            source_chat_id=404,
            source_message_id=505,
            delay_seconds=0,
        )
    )

    assert result.total == 3
    assert result.delivered == 2
    assert result.blocked == 1
    assert result.failed == 0
    assert result.blocked_recipients == (202,)

