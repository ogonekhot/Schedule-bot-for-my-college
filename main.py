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
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import moduls.backups as backup_service
import moduls.broadcast as broadcast_service
import moduls.db as dbm
import moduls.runtime_settings as runtime_settings
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


class RegistrationFlow(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()


dp = Dispatcher()
refresh_lock = asyncio.Lock()
broadcast_lock = asyncio.Lock()
registration_lock = asyncio.Lock()
runtime_settings_lock = asyncio.Lock()

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
    if (
        config.SCHEDULE_RECOVERY_ENABLED
        and not schedule_source.recovery_is_complete()
    ):
        LOGGER.info(
            "Обычное обновление отложено: recovery-monitor ждёт группу %s",
            config.SCHEDULE_RECOVERY_GROUP,
        )
        return
    if not runtime_settings.is_auto_update_enabled():
        LOGGER.info("Автоматическое обновление расписания отключено")
        return
    if refresh_lock.locked():
        LOGGER.info("Предыдущее обновление ещё идёт, новый запуск пропущен")
        return
    try:
        await refresh_schedule()
    except Exception as exc:  # noqa: BLE001 - keep cached data on every update failure
        # The cached schedule remains available; the next scheduled run retries.
        LOGGER.debug("Плановое обновление завершилось ошибкой: %s", exc)


async def scheduled_recovery(bot: Bot) -> None:
    """Make one low-rate attempt for the priority group and stop after success."""

    global last_update_error
    if not config.SCHEDULE_RECOVERY_ENABLED:
        return
    if schedule_source.recovery_is_complete():
        return
    if refresh_lock.locked():
        LOGGER.info("Recovery-monitor пропустил запуск: обновление уже выполняется")
        return

    try:
        async with refresh_lock:
            result = await schedule_source.recover_priority_group()
            reload_runtime_data()
            last_update_error = None
    except Exception as exc:  # noqa: BLE001 - the next interval retries once
        last_update_error = str(exc)
        LOGGER.warning(
            "Recovery-monitor: группа %s пока недоступна: %s",
            config.SCHEDULE_RECOVERY_GROUP,
            exc,
        )
        return

    LOGGER.info(
        "Recovery-monitor получил расписание %s; захват сохранён в %s",
        config.SCHEDULE_RECOVERY_GROUP,
        result.capture_dir,
    )
    message = (
        "✅ ЛК ЛГТУ ожил. Расписание группы "
        f"{config.SCHEDULE_RECOVERY_GROUP} получено и сохранено.\n\n"
        f"Время обновления: {result.update.updated_at}\n"
        "Recovery-monitor остановил сетевые попытки."
    )
    for admin_id in settings.get("admins", []):
        try:
            await bot.send_message(int(admin_id), message)
        except Exception:  # noqa: BLE001 - capture success must not be rolled back
            LOGGER.exception(
                "Не удалось уведомить администратора %s о восстановлении ЛК",
                admin_id,
            )


async def check_registration(event: types.Message | types.CallbackQuery) -> bool:
    user_data = await dbm.check_tg_id(event.from_user.id)
    if len(user_data) > 1 and user_data[1] is not None:
        return True

    groups = _available_groups()
    if isinstance(event, types.Message):
        send = event.answer
    else:
        send = event.message.edit_text

    buttons = InlineKeyboardBuilder()
    for group in groups:
        buttons.row(
            InlineKeyboardButton(
                text=group,
                callback_data=f"settings_group_{group}",
            )
        )
    buttons.row(
        InlineKeyboardButton(
            text="➕ Добавить свою группу",
            callback_data="registration_add_group",
        )
    )

    if groups:
        text = "Для начала выберите вашу группу или добавьте новую:"
    else:
        text = (
            "Расписание пока не загружено. Можно добавить свою группу "
            "с помощью аккаунта личного кабинета."
        )
    await send(text, reply_markup=buttons.as_markup())
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
            lines.append(
                html.escape(
                    schedule_source.normalize_lesson_title(str(details["title"]))
                )
            )
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
    buttons.row(InlineKeyboardButton(text="👤 Профиль", callback_data="profile"))
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


def _profile_text(
    telegram_user: types.User,
    user_data: list,
    account_link: dbm.CollegeAccountLink | None,
) -> str:
    group_name = (
        html.escape(str(user_data[1]))
        if len(user_data) > 1 and user_data[1] is not None
        else "не выбрана"
    )
    lines = [
        "👤 <b>Настройки профиля</b>",
        "",
        f"Имя: {html.escape(telegram_user.full_name)}",
        f"Telegram ID: <code>{telegram_user.id}</code>",
        f"Группа расписания: {group_name}",
        "",
        "<b>Аккаунт ЛГТУ</b>",
    ]
    if account_link is None:
        lines.extend(
            [
                "Статус: не привязан",
                "Привяжите аккаунт, чтобы бот проверил его и определил вашу группу.",
            ]
        )
    else:
        lines.extend(
            [
                "Статус: ✅ привязан",
                f"Логин: <code>{html.escape(account_link.login)}</code>",
                f"Группа аккаунта: {html.escape(account_link.group_name)}",
                "Пароль в профиле не хранится и не показывается.",
            ]
        )
    return "\n".join(lines)


def _profile_keyboard(linked: bool):
    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(
            text=(
                "🔄 Изменить привязку ЛГТУ"
                if linked
                else "🔗 Привязать аккаунт ЛГТУ"
            ),
            callback_data="profile_link_lgtu",
        )
    )
    buttons.row(InlineKeyboardButton(text="Назад", callback_data="back"))
    return buttons.as_markup()


async def show_profile(callback: types.CallbackQuery) -> bool:
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer(
            "Профиль доступен только в личном чате с ботом",
            show_alert=True,
        )
        return False

    user_data = await dbm.check_tg_id(callback.from_user.id)
    account_link = await dbm.get_college_account_link(int(user_data[0]))
    await callback.message.edit_text(
        _profile_text(callback.from_user, user_data, account_link),
        reply_markup=_profile_keyboard(account_link is not None),
        parse_mode="HTML",
    )
    return True


@dp.callback_query(F.data == "profile")
async def profile_callback(callback: types.CallbackQuery) -> None:
    if await show_profile(callback):
        await callback.answer()


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
async def settings_manager(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    group = callback.data.removeprefix("settings_group_")
    if group not in _available_groups():
        await callback.answer("Такой группы больше нет", show_alert=True)
        return
    await state.clear()
    user_id = (await dbm.check_tg_id(callback.from_user.id))[0]
    message = await dbm.update_user_settings(user_id, "group_name", group)
    await start(callback, callback_notice=message)


def _registration_cancel_keyboard(return_to_profile: bool = False):
    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(
            text="Отмена",
            callback_data=(
                "registration_cancel_profile"
                if return_to_profile
                else "registration_cancel"
            ),
        )
    )
    return buttons.as_markup()


def _registration_retry_keyboard(return_to_profile: bool):
    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(
            text="Попробовать снова",
            callback_data=(
                "profile_link_lgtu"
                if return_to_profile
                else "registration_add_group"
            ),
        )
    )
    buttons.row(
        InlineKeyboardButton(
            text=(
                "Вернуться в профиль"
                if return_to_profile
                else "Выбрать готовую группу"
            ),
            callback_data=(
                "registration_cancel_profile"
                if return_to_profile
                else "registration_cancel"
            ),
        )
    )
    return buttons.as_markup()


async def _delete_sensitive_message(message: types.Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        LOGGER.warning(
            "Не удалось удалить сообщение с учётными данными пользователя %s",
            message.from_user.id,
        )


@dp.callback_query(F.data == "profile_link_lgtu")
@dp.callback_query(F.data == "registration_add_group")
async def begin_group_registration(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    return_to_profile = callback.data == "profile_link_lgtu"
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if (
        len(user_data) > 1
        and user_data[1] is not None
        and not return_to_profile
    ):
        await state.clear()
        await callback.answer("Вы уже зарегистрированы", show_alert=True)
        return
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer(
            "Добавлять группу можно только в личном чате с ботом",
            show_alert=True,
        )
        return
    if registration_lock.locked():
        await callback.answer(
            "Сейчас проверяется другой аккаунт. Попробуйте немного позже.",
            show_alert=True,
        )
        return

    await state.clear()
    await state.update_data(registration_return_to_profile=return_to_profile)
    await state.set_state(RegistrationFlow.waiting_for_login)
    await callback.message.edit_text(
        "Отправьте логин от личного кабинета колледжа.\n\n"
        "Сообщение будет сразу удалено. После проверки в профиле сохранится "
        "только логин и найденная группа. Если группа новая, пароль сохранится "
        "только в закрытом runtime-файле для автообновления расписания; "
        "в профиль и таблицу пользователей он не попадёт.",
        reply_markup=_registration_cancel_keyboard(return_to_profile),
    )
    await callback.answer()


@dp.callback_query(F.data == "registration_cancel_profile")
@dp.callback_query(F.data == "registration_cancel")
async def cancel_group_registration(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    return_to_profile = (
        callback.data == "registration_cancel_profile"
        or bool(data.get("registration_return_to_profile"))
    )
    await state.clear()
    if return_to_profile:
        await show_profile(callback)
    else:
        await check_registration(callback)
    await callback.answer("Привязка аккаунта отменена")


@dp.message(RegistrationFlow.waiting_for_login)
async def receive_college_login(
    message: types.Message,
    state: FSMContext,
) -> None:
    value = message.text or message.caption or ""
    await _delete_sensitive_message(message)
    login = value.strip()
    data = await state.get_data()
    return_to_profile = bool(data.get("registration_return_to_profile"))
    if not login or len(login) > 256:
        await message.answer(
            "Логин пустой или слишком длинный. Отправьте корректный логин.",
            reply_markup=_registration_cancel_keyboard(return_to_profile),
        )
        return

    await state.update_data(college_login=login)
    await state.set_state(RegistrationFlow.waiting_for_password)
    await message.answer(
        "Теперь отправьте пароль от личного кабинета. "
        "Сообщение с паролем тоже будет сразу удалено.",
        reply_markup=_registration_cancel_keyboard(return_to_profile),
    )


@dp.message(RegistrationFlow.waiting_for_password)
async def receive_college_password(
    message: types.Message,
    state: FSMContext,
) -> None:
    value = message.text or message.caption or ""
    await _delete_sensitive_message(message)
    password = value.strip()
    data = await state.get_data()
    login = str(data.get("college_login", "")).strip()
    return_to_profile = bool(data.get("registration_return_to_profile"))
    await state.clear()

    if not login or not password or len(password) > 256:
        await message.answer(
            "Логин или пароль пустой либо слишком длинный. Начните заново.",
            reply_markup=_registration_retry_keyboard(return_to_profile),
        )
        return
    if registration_lock.locked():
        await message.answer(
            "Другой аккаунт уже проверяется. Введённые данные удалены, "
            "попробуйте снова немного позже.",
            reply_markup=_registration_retry_keyboard(return_to_profile),
        )
        return

    status = await message.answer(
        "Проверяю аккаунт и получаю расписание. Это может занять несколько минут…"
    )
    try:
        async with registration_lock:
            async with refresh_lock:
                result = await schedule_source.register_schedule_account(
                    login,
                    password,
                )
                reload_runtime_data()
                user_id = (await dbm.check_tg_id(message.from_user.id))[0]
                await dbm.save_user_group_and_college_account_link(
                    user_id,
                    login,
                    result.group,
                )
    except Exception as exc:  # noqa: BLE001 - show safe scraper errors to the user
        LOGGER.warning(
            "Не удалось добавить группу для пользователя %s: %s",
            message.from_user.id,
            exc,
        )
        await status.edit_text(
            "Не удалось проверить аккаунт. Логин и пароль не сохранены.\n\n"
            f"Ошибка: {str(exc)[:700]}",
            reply_markup=_registration_retry_keyboard(return_to_profile),
        )
        return
    finally:
        login = ""
        password = ""

    action = "добавлена в бот" if result.account_added else "уже была в боте"
    if return_to_profile:
        buttons = InlineKeyboardBuilder()
        buttons.row(
            InlineKeyboardButton(text="👤 Открыть профиль", callback_data="profile")
        )
        buttons.row(InlineKeyboardButton(text="Главное меню", callback_data="back"))
        await status.edit_text(
            f"Аккаунт ЛГТУ привязан. Группа «{result.group}» {action}.",
            reply_markup=buttons.as_markup(),
        )
    else:
        await status.edit_text(
            f"Группа «{result.group}» {action}. Вы успешно зарегистрированы."
        )
        await start(message)




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

    auto_update_enabled = runtime_settings.is_auto_update_enabled()
    backup_count = len(backup_service.list_schedule_backups())

    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(
            text="🔄 Обновить расписание", callback_data="admin_update"
        )
    )
    buttons.row(
        InlineKeyboardButton(
            text=(
                "⏱ Автообновление: ВКЛ"
                if auto_update_enabled
                else "⏱ Автообновление: ВЫКЛ"
            ),
            callback_data="admin_toggle_auto_update",
        )
    )
    buttons.row(
        InlineKeyboardButton(
            text=f"💾 Резервные копии ({backup_count})",
            callback_data="admin_backups",
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
        f"{html.escape(_update_status_text())}\n"
        f"Автообновление: "
        f"{'включено' if auto_update_enabled else 'выключено'}\n"
        f"Резервных копий: {backup_count}"
    )
    await callback.message.edit_text(
        text, reply_markup=buttons.as_markup(), parse_mode="HTML"
    )
    return True


def _backup_reason_label(reason: str) -> str:
    return {
        "schedule_update": "перед обновлением",
        "schedule_recovery": "перед аварийным восстановлением",
        "group_registration": "перед добавлением группы",
        "before_restore": "перед восстановлением",
        "manual": "создана вручную",
    }.get(reason, reason)


def _format_backup_time(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value).astimezone(
            ZoneInfo(config.BOT_TIMEZONE)
        )
        return f"{moment:%d.%m.%Y %H:%M:%S}"
    except ValueError:
        return value or "неизвестное время"


@dp.callback_query(F.data == "admin_toggle_auto_update")
async def toggle_auto_update_callback(callback: types.CallbackQuery) -> None:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    async with runtime_settings_lock:
        enabled = runtime_settings.toggle_auto_update()
    await show_admin_panel(callback)
    await callback.answer(
        "Автообновление включено" if enabled else "Автообновление выключено"
    )


async def show_backups_panel(callback: types.CallbackQuery) -> bool:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return False

    backups = backup_service.list_schedule_backups()
    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(
            text="➕ Создать копию сейчас",
            callback_data="admin_backup_create",
        )
    )
    for item in backups:
        buttons.row(
            InlineKeyboardButton(
                text=(
                    f"{_format_backup_time(item.created_at)} · "
                    f"{item.group_count} гр."
                ),
                callback_data=f"admin_backup_view_{item.backup_id}",
            )
        )
    buttons.row(InlineKeyboardButton(text="Назад", callback_data="admin"))

    text = (
        f"Резервные копии расписания: {len(backups)}/"
        f"{config.SCHEDULE_BACKUP_LIMIT}.\n\n"
        "Копия создаётся автоматически перед каждым успешным обновлением, "
        "добавлением группы и восстановлением. Логины и пароли в неё не входят."
    )
    if not backups:
        text += "\n\nРезервных копий пока нет."

    await callback.message.edit_text(
        text,
        reply_markup=buttons.as_markup(),
    )
    return True


@dp.callback_query(F.data == "admin_backups")
async def backups_panel_callback(callback: types.CallbackQuery) -> None:
    if await show_backups_panel(callback):
        await callback.answer()


@dp.callback_query(F.data == "admin_backup_create")
async def create_backup_callback(callback: types.CallbackQuery) -> None:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if refresh_lock.locked():
        await callback.answer(
            "Сейчас изменяется расписание, попробуйте позже",
            show_alert=True,
        )
        return

    try:
        async with refresh_lock:
            info = backup_service.create_schedule_backup("manual")
    except Exception as exc:  # noqa: BLE001 - show backup failure to admin
        LOGGER.exception("Не удалось создать резервную копию расписания")
        await callback.answer(
            f"Ошибка создания копии: {str(exc)[:150]}",
            show_alert=True,
        )
        return

    await show_backups_panel(callback)
    await callback.answer(
        f"Копия от {_format_backup_time(info.created_at)} создана"
    )


@dp.callback_query(F.data.startswith("admin_backup_view_"))
async def view_backup_callback(callback: types.CallbackQuery) -> None:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    backup_id = callback.data.removeprefix("admin_backup_view_")
    try:
        info = backup_service.get_schedule_backup(backup_id)
    except backup_service.ScheduleBackupError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    buttons = InlineKeyboardBuilder()
    buttons.row(
        InlineKeyboardButton(
            text="♻️ Восстановить эту копию",
            callback_data=f"admin_backup_confirm_{backup_id}",
        )
    )
    buttons.row(
        InlineKeyboardButton(
            text="Назад к копиям",
            callback_data="admin_backups",
        )
    )
    await callback.message.edit_text(
        "Резервная копия расписания\n\n"
        f"Дата: {_format_backup_time(info.created_at)}\n"
        f"Причина: {_backup_reason_label(info.reason)}\n"
        f"Групп: {info.group_count}\n\n"
        "Перед восстановлением бот автоматически сохранит текущее расписание.",
        reply_markup=buttons.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_backup_confirm_"))
async def restore_backup_callback(callback: types.CallbackQuery) -> None:
    user_data = await dbm.check_tg_id(callback.from_user.id)
    if not _is_admin(user_data, callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if refresh_lock.locked():
        await callback.answer(
            "Сейчас изменяется расписание, попробуйте позже",
            show_alert=True,
        )
        return

    backup_id = callback.data.removeprefix("admin_backup_confirm_")
    try:
        async with refresh_lock:
            info = backup_service.restore_schedule_backup(backup_id)
            reload_runtime_data()
    except Exception as exc:  # noqa: BLE001 - show restore failure to admin
        LOGGER.exception("Не удалось восстановить расписание из копии")
        await callback.answer(
            f"Ошибка восстановления: {str(exc)[:150]}",
            show_alert=True,
        )
        return

    buttons = InlineKeyboardBuilder()
    buttons.row(InlineKeyboardButton(text="В админ-панель", callback_data="admin"))
    await callback.message.edit_text(
        "Расписание восстановлено.\n\n"
        f"Копия: {_format_backup_time(info.created_at)}\n"
        f"Групп: {info.group_count}",
        reply_markup=buttons.as_markup(),
    )
    await callback.answer("Восстановление завершено")


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
        groups = _available_groups()
        if not 0 <= group_index < len(groups):
            raise IndexError
        group = groups[group_index]
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

        removed_users = 0
        if result.blocked_recipients:
            try:
                removed_users = await dbm.delete_users_by_telegram_ids(
                    result.blocked_recipients
                )
            except Exception:  # noqa: BLE001 - delivery result must still be shown
                LOGGER.exception(
                    "Не удалось удалить пользователей, заблокировавших бота"
                )

    buttons = InlineKeyboardBuilder()
    buttons.row(InlineKeyboardButton(text="Назад", callback_data="admin"))
    await callback.message.edit_text(
        f"{completed_text}\n\n"
        f"Всего получателей: {result.total}\n"
        f"✅ Доставлено: {result.delivered}\n"
        f"🚫 Бот заблокирован: {result.blocked}\n"
        f"🗑 Удалено из базы: {removed_users}\n"
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
    await dbm.ensure_profile_schema()
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
    if (
        config.SCHEDULE_RECOVERY_ENABLED
        and not schedule_source.recovery_is_complete()
    ):
        scheduler.add_job(
            scheduled_recovery,
            "interval",
            args=(bot,),
            minutes=config.SCHEDULE_RECOVERY_INTERVAL_MINUTES,
            jitter=config.SCHEDULE_RECOVERY_JITTER_SECONDS,
            id="schedule-recovery",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    scheduler.start()

    startup_task: asyncio.Task | None = None
    if (
        config.SCHEDULE_RECOVERY_ENABLED
        and not schedule_source.recovery_is_complete()
    ):
        startup_task = asyncio.create_task(
            scheduled_recovery(bot), name="startup-recovery"
        )
    elif config.SCHEDULE_UPDATE_ON_STARTUP:
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

