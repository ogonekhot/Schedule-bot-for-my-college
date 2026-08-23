"""Helpers for copying an administrator message to every bot user."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BroadcastResult:
    total: int
    delivered: int
    blocked: int
    failed: int


async def copy_message_to_users(
    bot: Bot,
    recipients: Iterable[int],
    *,
    source_chat_id: int,
    source_message_id: int,
    delay_seconds: float = 0.05,
) -> BroadcastResult:
    """Copy one Telegram message to unique recipients with flood-wait handling."""

    unique_recipients = tuple(dict.fromkeys(int(chat_id) for chat_id in recipients))
    delivered = 0
    blocked = 0
    failed = 0

    for chat_id in unique_recipients:
        while True:
            try:
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                )
                delivered += 1
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(max(float(exc.retry_after), 0.0) + 0.1)
            except TelegramForbiddenError:
                blocked += 1
                break
            except (TelegramBadRequest, TelegramAPIError) as exc:
                failed += 1
                LOGGER.warning(
                    "Не удалось доставить глобальное оповещение пользователю %s: %s",
                    chat_id,
                    exc,
                )
                break

        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    return BroadcastResult(
        total=len(unique_recipients),
        delivered=delivered,
        blocked=blocked,
        failed=failed,
    )
