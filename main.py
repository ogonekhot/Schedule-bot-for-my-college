from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import arrow
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import moduls.broadcast as broadcast_service
import moduls.db as dbm
import moduls.schedule as schedule_source
import moduls.time as tm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger(__name__)

class BroadcastFlow(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()


dp = Dispatcher()
refresh_lock = asyncio.Lock()
broadcast_lock = asyncio.Lock()

settings: dict[str, Any] = {}
schedule: dict[str, Any] = {}
addresses: dict[str, Any] = {}
reference: dict[str, str] = {}
last_update_error: str | None = None


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось загрузить {path}: {exc}") from exc


def reload_runtime_data() -> None:
    """Reload atomically written schedule files without restarting the bot."""

    global settings, schedule, addresses, reference
    settings = _load_json(config.GLOBAL_SETTINGS_FILE)
    addresses = _load_json(config.ADDRESSES_FILE)

    schedule_path = (
        config.SCHEDULE_FILE
        if config.SCHEDULE_FILE.exists()
        else config.INITIAL_SCHEDULE_FILE
    )
    schedule = _load_json(schedule_path)
    reference = (
        _load_json(config.REFERENCE_FILE)
        if config.REFERENCE_FILE.exists()
        else settings.get("references", {})
    )
    if not reference.get("date") or not reference.get("color"):
        raise RuntimeError("Не заданы дата и цвет эталонной недели")


def build_bot() -> Bot:
    if not config.BOT_API_BASE_URL:
        LOGGER.info("Используется официальный Telegram Bot API")
        return Bot(token=config.TOKEN)

    api = TelegramAPIServer.from_base(
        config.BOT_API_BASE_URL,
        is_local=config.BOT_API_IS_LOCAL,
    )
    session = AiohttpSession(api=api)
    LOGGER.info("Используется локальный Telegram Bot API: %s", config.BOT_API_BASE_URL)
    return Bot(token=config.TOKEN, session=session)


def _available_groups() -> list[str]:
    groups = list(schedule)
    if groups:
        return groups
    return list(config.load_schedule_accounts(settings))


def _is_admin(user_data: list, telegram_id: int) -> bool:
    database_admin = len(user_data) > 2 and bool(user_data[2])
    return database_admin or telegram_id in settings.get("admins", [])


async def refresh_schedule() -> schedule_source.UpdateResult:
    global last_update_error
    async with refresh_lock:
        try:
            result = await schedule_source.update_schedule()
            reload_runtime_data()
            last_update_error = None
            LOGGER.info("Расписание обновлено для групп: %s", ", ".join(result.groups))
            return result
        except Exception as exc:
            last_update_error = str(exc)
            LOGGER.exception("Автоматическое обновление расписания не удалось")
            raise


async def scheduled_refresh() -> None:
    if refresh_lock.locked():
        LOGGER.info("Предыдущее обновление ещё идёт, новый запуск пропущен")
        return
    try:
        await refresh_schedule()
    except Exception as exc:  # noqa: BLE001 - keep cached data on every update failure
        # The cached schedule remains available; the next scheduled run retries.
        LOGGER.debug("Плановое обновление завершилось ошибкой: %s", exc)


async def check_registration(event: types.Message | types.CallbackQuery) -> bool:
    user_data = await dbm.check_tg_id(event.from_user.id)
    if len(user_data) > 1 and user_data[1] is not None:
        return True

    groups = _available_groups()
    if isinstance(event, types.Message):
        send = event.answer
    else:
        send = event.message.edit_text

    if not groups:
        await send(
            "Расписание пока не загружено. Попробуйте ещё раз через несколько минут."
        )
        return False

    buttons = InlineKeyboardBuilder()
    for group in groups:
        buttons.row(
            InlineKeyboardButton(
                text=group,
                callback_data=f"settings_group_{group}",
            )
        )
    await send("Для начала выберите вашу группу:", reply_markup=buttons.as_markup())
    return False


def _format_room(room: str, lesson_type: str) -> str:
    room = str(room).strip()
    lesson_type = str(lesson_type).strip()
    if not room:
        return ""

    location = room
    if room.isdigit():
        room_number = int(room)
        for corpus, floors in addresses.items():
            if any(
                int(floor["min"]) <= room_number <= int(floor["max"])
                for floor in floors.values()
            ):
                location = f"{corpus} {room}"
                break
    elif room.startswith("9-"):
        location = f"9-й корпус {room[2:]}"

    return " ".join(part for part in (location, lesson_type) if part)


async def render_schedule(
    user_id: int,
    date: str | None = None,
) -> str:
    date = date or arrow.now(config.BOT_TIMEZONE).format("DD.MM.YYYY")
    group = (await dbm.check_tg_id(user_id))[1]
    day, color = await tm.get_this_weekday(
        reference["date"], reference["color"], date=date
    )

    shown_date = arrow.get(date, "DD.MM.YYYY").format("DD.MM")
    lines = [f"<b>{html.escape(day)} — {html.escape(color)} ({shown_date})</b>", ""]
    lessons = schedule.get(group, {}).get(day, {})
    visible_count = 0

    for lesson in lessons.values():
        details = lesson.get(color, {})
        if not any(details.get(field) for field in ("title", "teacher", "room")):
            continue
        visible_count += 1
        lesson_time = lesson.get("time", {})
        lines.append(
            f"{visible_count}-я пара <b>"
            f"{html.escape(str(lesson_time.get('start', '')))} — "
            f"{html.escape(str(lesson_time.get('end', '')))}</b>:"
        )
        if details.get("title"):
            lines.append(html.escape(str(details["title"])))
        if details.get("teacher"):
            lines.append(html.escape(str(details["teacher"])))
        room = _format_room(details.get("room", ""), details.get("type", ""))
        if room:
            lines.append(html.escape(room))
        lines.append("")

    if visible_count == 0:
        lines.append("Выходной")
    return "\n".join(lines).rstrip()


@dp.message(CommandStart())
@dp.callback_query(F.data == "back")
async def start(
    event: types.Message | types.CallbackQuery,
    callback_notice: str | None = None,
) -> None:
    user_data = await dbm.check_tg_id(event.from_user.id)
    if not await check_registration(event):
        return

    buttons = InlineKeyboardBuilder()
    buttons.row(InlineKeyboardButton(text="Расписание", callback_data="schedule"))
    if _is_admin(user_data, event.from_user.id):
        buttons.row(InlineKeyboardButton(text="Админ-панель", callback_data="admin"))

    text = f"С возвращением, {html.escape(event.from_user.full_name)}!"
    if isinstance(event, types.Message):
        await event.answer(text, reply_markup=buttons.as_markup(), parse_mode="HTML")
    else:
        await event.message.edit_text(
            text, reply_markup=buttons.as_markup(), parse_mode="HTML"
        )
        await event.answer(callback_notice)


@dp.callback_query(F.data.startswith("schedule"))
async def schedule_manager(callback: types.CallbackQuery) -> None:
    if not await check_registration(callback):
        await callback.answer()
        return

    parts = callback.data.split("_", maxsplit=1)
    date = (
        parts[1]
        if len(parts) == 2
        else arrow.now(config.BOT_TIMEZONE).format("DD.MM.YYYY")
    )
    try:
        text = await render_schedule(callback.from_user.id, date)
        previous_week, previous_day, next_day, next_week = await asyncio.gather(
            tm.get_next_previous(date, "extra_previous"),
            tm.get_next_previous(date, "previous"),
            tm.get_next_previous(date, "next"),
            tm.get_next_previous(date, "extra_next"),
        )
    except ValueError:
        await callback.answer("Некорректная дата", show_alert=True)
        return

    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(text="⏪", callback_data=f"schedule_{previous_week}"),
        InlineKeyboardButton(text="◀️", callback_data=f"schedule_{previous_day}"),
        InlineKeyboardButton(text="🔄", callback_data="schedule"),
        InlineKeyboardButton(text="▶️", callback_data=f"schedule_{next_day}"),
        InlineKeyboardButton(text="⏩", callback_data=f"schedule_{next_week}"),
    )
    buttons.row(InlineKeyboardButton(text="Назад", callback_data="back"))

    try:
        await callback.message.edit_text(
            text,
            reply_markup=buttons.as_markup(),
            parse_mode="HTML",
        )
        await callback.answer()
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            await callback.answer("Ничего не изменилось")
        else:
            raise


@dp.callback_query(F.data.startswith("settings_group_"))
async def settings_manager(callback: types.CallbackQuery) -> None:
    group = callback.data.removeprefix("settings_group_")
    if group not in _available_groups():
        await callback.answer("Такой группы больше нет", show_alert=True)
        return
    user_id = (await dbm.check_tg_id(callback.from_user.id))[0]
    message = await dbm.update_user_settings(user_id, "group_name", group)
    await start(callback, callback_notice=message)


def _update_status_text() -> str:
    if refresh_lock.locked():
        return "Обновление расписания: выполняется"
    if last_update_error:
        return f"Последняя ошибка обновления: {last_update_error}"
    updated_at = reference.get("updated_at")
    if updated_at:
        try:
            value = datetime.fromisoformat(updated_at).astimezone(
                ZoneInfo(config.BOT_TIMEZONE)
            )
            return f"Расписание обновлено: {value:%d.%m.%Y %H:%M}"
        except ValueError:
            pass
    return "Используется расписание из начального файла"


async def show_admin_panel(callback: types.CallbackQuery) -> bool:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return False

    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(
            text="🔄 Обновить расписание", callback_data="admin_update"
        )
    )
    buttons.row(
        InlineKeyboardButton(
            text="📢 Глобальное оповещение",
            callback_data="admin_broadcast",
        )
    )
    buttons.row(
        InlineKeyboardButton(
            text="👥 Оповещение группе",
            callback_data="admin_group_broadcast",
        )
    )
    buttons.row(InlineKeyboardButton(text="Назад", callback_data="back"))
    admin_value = "true" if _is_admin(user_data, callback.from_user.id) else "false"
    text = (
        "Профиль:\n"
        f"Имя: {html.escape(callback.from_user.full_name)}\n"
        f"Ссылка: tg://user?id={callback.from_user.id}\n"
        f"Telegram ID: {callback.from_user.id}\n"
        f"DB ID: {user_data[0]}\n"
        f"Группа: {html.escape(str(user_data[1]))}\n"
        f"Администратор: {admin_value}\n\n"
        f"{html.escape(_update_status_text())}"
    )
    await callback.message.edit_text(
        text, reply_markup=buttons.as_markup(), parse_mode="HTML"
    )
    return True


@dp.callback_query(F.data == "admin_update")
async def force_update_callback(callback: types.CallbackQuery) -> None:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if refresh_lock.locked():
        await callback.answer("Обновление уже выполняется", show_alert=True)
        return

    await callback.answer("Обновляю расписание…")
    await callback.message.edit_text("Получаю свежее расписание с сайта колледжа…")
    try:
        await refresh_schedule()
        await show_admin_panel(callback)
    except Exception as exc:  # noqa: BLE001 - show any updater failure to the admin
        buttons = InlineKeyboardBuilder()
        buttons.row(InlineKeyboardButton(text="Назад", callback_data="admin"))
        await callback.message.edit_text(
            "Не удалось обновить расписание. Старое расписание сохранено.\n\n"
            f"Ошибка: {html.escape(str(exc))}",
            reply_markup=buttons.as_markup(),
            parse_mode="HTML",
        )


@dp.callback_query(F.data == "admin_broadcast")
async def begin_global_broadcast(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if broadcast_lock.locked():
        await callback.answer("Другая рассылка уже выполняется", show_alert=True)
        return

    await state.clear()
    await state.update_data(target_group=None)
    await state.set_state(BroadcastFlow.waiting_for_message)

    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(
            text="Отмена",
            callback_data="admin_broadcast_cancel",
        )
    )
    await callback.message.edit_text(
        "Отправьте сообщение для глобального оповещения.\n\n"
        "Поддерживаются текст, фото, видео, документы и другие "
        "обычные сообщения Telegram. Перед рассылкой бот попросит подтверждение.",
        reply_markup=buttons.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_group_broadcast")
async def choose_broadcast_group(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if broadcast_lock.locked():
        await callback.answer("Другая рассылка уже выполняется", show_alert=True)
        return

    groups = _available_groups()
    if not groups:
        await callback.answer("Список групп пока пуст", show_alert=True)
        return

    await state.clear()
    buttons = InlineKeyboardBuilder()
    for index, group in enumerate(groups):
        buttons.row(
            InlineKeyboardButton(
                text=group,
                callback_data=f"admin_broadcast_group_{index}",
            )
        )
    buttons.row(
        InlineKeyboardButton(
            text="Отмена",
            callback_data="admin_broadcast_cancel",
        )
    )
    await callback.message.edit_text(
        "Выберите группу, которой нужно отправить оповещение:",
        reply_markup=buttons.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_broadcast_group_"))
async def begin_group_broadcast(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if broadcast_lock.locked():
        await callback.answer("Другая рассылка уже выполняется", show_alert=True)
        return

    try:
        group_index = int(callback.data.removeprefix("admin_broadcast_group_"))
        group = _available_groups()[group_index]
    except (ValueError, IndexError):
        await callback.answer("Такая группа больше недоступна", show_alert=True)
        return

    recipients = await dbm.get_telegram_ids_by_group(group)
    if not recipients:
        await callback.answer(
            "В этой группе пока нет зарегистрированных пользователей",
            show_alert=True,
        )
        return

    await state.clear()
    await state.update_data(target_group=group)
    await state.set_state(BroadcastFlow.waiting_for_message)

    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(
            text="Отмена",
            callback_data="admin_broadcast_cancel",
        )
    )
    await callback.message.edit_text(
        f"Отправьте оповещение для группы «{group}».\n\n"
        f"Сейчас в группе получателей: {len(set(recipients))}. "
        "Поддерживаются текст, фото, видео, документы и другие "
        "обычные сообщения Telegram. Перед рассылкой бот попросит подтверждение.",
        reply_markup=buttons.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_broadcast_cancel")
async def cancel_global_broadcast(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    await state.clear()
    await show_admin_panel(callback)
    await callback.answer("Рассылка отменена")


@dp.message(BroadcastFlow.waiting_for_message)
@dp.message(BroadcastFlow.waiting_for_confirmation)
async def prepare_global_broadcast(
    message: types.Message,
    state: FSMContext,
) -> None:
    user_data = await dbm.check_tg_id(message.from_user.id)
    if not _is_admin(user_data, message.from_user.id):
        await state.clear()
        await message.answer("Недостаточно прав")
        return

    data = await state.get_data()
    target_group = data.get("target_group")
    if target_group:
        recipients = await dbm.get_telegram_ids_by_group(str(target_group))
        target_text = f"группе «{target_group}»"
        confirm_text = f"✅ Отправить группе ({len(set(recipients))})"
    else:
        recipients = await dbm.get_all_telegram_ids()
        target_text = "всем зарегистрированным пользователям"
        confirm_text = f"✅ Отправить всем ({len(set(recipients))})"

    recipient_count = len(set(recipients))
    if recipient_count == 0:
        await state.clear()
        await message.answer("Для выбранной рассылки пока нет получателей")
        return

    await state.update_data(
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
    )
    await state.set_state(BroadcastFlow.waiting_for_confirmation)

    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(
            text=confirm_text,
            callback_data="admin_broadcast_confirm",
        )
    )
    buttons.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="admin_broadcast_cancel",
        )
    )
    await message.answer(
        f"Сообщение принято. Отправить его {target_text}?",
        reply_markup=buttons.as_markup(),
    )


@dp.callback_query(
    BroadcastFlow.waiting_for_confirmation,
    F.data == "admin_broadcast_confirm",
)
async def confirm_global_broadcast(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await state.clear()
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if broadcast_lock.locked():
        await callback.answer("Другая рассылка уже выполняется", show_alert=True)
        return

    data = await state.get_data()
    source_chat_id = data.get("source_chat_id")
    source_message_id = data.get("source_message_id")
    target_group = data.get("target_group")
    if source_chat_id is None or source_message_id is None:
        await state.clear()
        await callback.answer("Сообщение для рассылки потеряно", show_alert=True)
        return

    async with broadcast_lock:
        if target_group:
            recipients = await dbm.get_telegram_ids_by_group(str(target_group))
            progress_text = (
                f"Отправляю оповещение группе «{target_group}»: "
                f"{len(set(recipients))}…"
            )
            completed_text = f"Оповещение группе «{target_group}» отправлено."
        else:
            recipients = await dbm.get_all_telegram_ids()
            progress_text = (
                "Отправляю глобальное оповещение пользователям: "
                f"{len(set(recipients))}…"
            )
            completed_text = "Глобальное оповещение отправлено."

        await state.clear()
        if not recipients:
            await callback.answer("Получателей больше нет", show_alert=True)
            buttons = InlineKeyboardBuilder()
            buttons.row(InlineKeyboardButton(text="Назад", callback_data="admin"))
            await callback.message.edit_text(
                "Рассылка не выполнена: для выбранной аудитории больше нет "
                "зарегистрированных пользователей.",
                reply_markup=buttons.as_markup(),
            )
            return

        await callback.answer("Рассылка началась")
        await callback.message.edit_text(progress_text)

        result = await broadcast_service.copy_message_to_users(
            callback.bot,
            recipients,
            source_chat_id=int(source_chat_id),
            source_message_id=int(source_message_id),
        )

    buttons = InlineKeyboardBuilder()
    buttons.row(InlineKeyboardButton(text="Назад", callback_data="admin"))
    await callback.message.edit_text(
        f"{completed_text}\n\n"
        f"Всего получателей: {result.total}\n"
        f"✅ Доставлено: {result.delivered}\n"
        f"🚫 Бот заблокирован: {result.blocked}\n"
        f"⚠️ Другие ошибки: {result.failed}",
        reply_markup=buttons.as_markup(),
    )


@dp.callback_query(F.data == "admin")
async def admin_panel(callback: types.CallbackQuery) -> None:
    if await show_admin_panel(callback):
        await callback.answer()


@dp.message(Command("update"))
async def force_update_command(message: types.Message) -> None:
    user_data = await dbm.check_tg_id(message.from_user.id)
    if not _is_admin(user_data, message.from_user.id):
        await message.answer("Недостаточно прав")
        return
    if refresh_lock.locked():
        await message.answer("Обновление уже выполняется")
        return

    status = await message.answer("Получаю свежее расписание с сайта колледжа…")
    try:
        result = await refresh_schedule()
        await status.edit_text(
            "Расписание обновлено для групп: " + ", ".join(result.groups)
        )
    except Exception as exc:  # noqa: BLE001 - show any updater failure to the admin
        await status.edit_text(
            "Не удалось обновить расписание. Старое расписание сохранено.\n\n"
            f"Ошибка: {html.escape(str(exc))}",
            parse_mode="HTML",
        )


@dp.message()
async def delete_user_message(message: types.Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def main() -> None:
    config.validate_runtime_config()
    reload_runtime_data()
    bot = build_bot()
    scheduler = AsyncIOScheduler(timezone=config.BOT_TIMEZONE)
    scheduler.add_job(
        scheduled_refresh,
        "interval",
        minutes=config.SCHEDULE_UPDATE_INTERVAL_MINUTES,
        id="schedule-refresh",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()

    startup_task: asyncio.Task | None = None
    if config.SCHEDULE_UPDATE_ON_STARTUP:
        startup_task = asyncio.create_task(scheduled_refresh(), name="startup-refresh")

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(
            bot,
            close_bot_session=False,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        scheduler.shutdown(wait=False)
        if startup_task and not startup_task.done():
            startup_task.cancel()
            await asyncio.gather(startup_task, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
