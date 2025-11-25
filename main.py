from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import CommandObject, CommandStart
from apscheduler.triggers.cron import CronTrigger
from aiogram.types import InlineKeyboardButton
from aiogram import types, Dispatcher, Bot, F
import moduls.time as tm
import moduls.db as dbm
import logging
import asyncio
import config
import arrow
import json

#########################################################################################
#                           Инициализация логирования и бота                            #
#########################################################################################

logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.TOKEN)
dp = Dispatcher()

scheduler = AsyncIOScheduler()

#########################################################################################
#                       Загрузка настроек и дополнительных файлов                       #
#########################################################################################

try:
    with open(rf"{config.BASE_DIR}/settings/global.json", "r", encoding="utf_8_sig") as f:
        settings = json.loads(f.read())
except:
    print('Не удалось загрузить глобальные настройки')

try:
    with open(rf"{config.BASE_DIR}/settings/schedule.json", "r", encoding="utf_8_sig") as f:
        schedule = json.loads(f.read())
except:
    print('Не удалось загрузить расписание')

try:
    with open(rf"{config.BASE_DIR}/settings/addresses.json", "r", encoding="utf_8_sig") as f:
        addresses = json.loads(f.read())
except:
    print('Не удалось загрузить адреса')

#########################################################################################
#                                 Основная логика бота                                  #
#########################################################################################

async def check_registration(event):
    user_data = await dbm.check_tg_id(event.from_user.id)

    if user_data[1] is None:
        buttons = InlineKeyboardBuilder()

        if isinstance(event, types.Message):
            message = event.answer
        else:
            message = event.message.edit_text

        for i in schedule.keys():
            buttons.row(InlineKeyboardButton(text = i, callback_data = f'settings_group_{i}'))

        await message('Для начала выберите вашу группу:', reply_markup=buttons.as_markup())

        return False

    return True


@dp.message(CommandStart())
@dp.callback_query(F.data == 'back')
async def start(event: types.Message | types.CallbackQuery):
    user_data = await dbm.check_tg_id(event.from_user.id)

    if isinstance(event, types.Message):
        message = event.answer
    else:
        message = event.message.edit_text

    buttons = InlineKeyboardBuilder()

    if await check_registration(event) == True:
        buttons.row(InlineKeyboardButton(text = 'Расписание', callback_data = f'schedule'))
        if user_data[2] == 1:
            buttons.row(InlineKeyboardButton(text = 'Админ панель', callback_data = f'admin'))

        await message(f'С возвращением, {event.from_user.full_name}!', reply_markup=buttons.as_markup())

@dp.callback_query(F.data[:8] == 'schedule')
async def schedule_manager(callback: types.CallbackQuery):
    if await check_registration(callback) == True:
        temp = callback.data.split('_')

        if len(temp) == 1:
            temp.append(arrow.now().format("DD.MM.YYYY"))

        buttons = InlineKeyboardBuilder()
        buttons.row(InlineKeyboardButton(text = '⏪', callback_data = f'schedule_{await tm.get_next_previous(temp[1], "e_p")}'), InlineKeyboardButton(text = '◀️', callback_data = f'schedule_{await tm.get_next_previous(temp[1], "p")}'), InlineKeyboardButton(text = '🔄️', callback_data = f'schedule'), InlineKeyboardButton(text = '▶️', callback_data = f'schedule_{await tm.get_next_previous(temp[1], "n")}'), InlineKeyboardButton(text = '⏩', callback_data = f'schedule_{await tm.get_next_previous(temp[1], "e_n")}'))
        buttons.row(InlineKeyboardButton(text = 'Назад', callback_data = 'back'))
        group = (await dbm.check_tg_id(callback.from_user.id))[1]
        day, color = await tm.get_this_weekday(settings["references"]["date"], settings["references"]["color"], date=temp[1])
        
        text = f'<b>{day} - {color} ({arrow.get(temp[1], "DD.MM.YYYY").format("DD.MM")})</b>\n\n'

        try:
            schedule_fd = schedule[group][day]
            count = 0

            for i in schedule_fd.keys():
                if schedule_fd[i][color]["title"] != '' or schedule_fd[i][color]["teacher"] != '':
                    count += 1
                    text += f'{count}-я пара <b>{schedule_fd[i]["time"]["start"]} - {schedule_fd[i]["time"]["end"]}</b>:\n'

                    if schedule_fd[i][color]["title"] != '':
                        text += f'{schedule_fd[i][color]["title"]}\n'

                    if schedule_fd[i][color]["teacher"] != '':
                        text += f'{schedule_fd[i][color]["teacher"]}\n'

                    if schedule_fd[i][color]["room"] != '':
                        for corpuse in addresses.keys():
                            for flore in addresses[corpuse].keys():
                                if str(schedule_fd[i][color]["room"]).isdigit():
                                    if int(addresses[corpuse][flore]["min"]) <= int(schedule_fd[i][color]["room"]) <= int(addresses[corpuse][flore]["max"]):
                                        adress = f'{corpuse} {schedule_fd[i][color]["room"]} {schedule_fd[i][color]["type"]}'
                                elif str(schedule_fd[i][color]["room"])[:2] == '9-':
                                    adress = f'9-й корпус {str(schedule_fd[i][color]["room"])[2:]} {schedule_fd[i][color]["type"]}'
                                else:
                                    adress = f'{schedule_fd[i][color]["room"]} {schedule_fd[i][color]["type"]}'
                            
                            if 'adress' in locals():
                                break

                        text += f'{adress}\n\n'
        except:
            text += 'Выходной'

        try:
            await callback.message.edit_text(text, reply_markup=buttons.as_markup(), parse_mode='HTML')
        except:
            await callback.answer('Ничего не изменилось...', show_alert=False)

@dp.callback_query(F.data[:8] == 'settings')
async def settings_manager(callback: types.CallbackQuery):
    temp = callback.data.split('_')
    
    if temp[1] == 'group':
        await callback.answer(await dbm.update_user_settings((await dbm.check_tg_id(callback.from_user.id))[0], 'group_name', temp[2]), show_alert=False)

    await start(callback)

@dp.callback_query(F.data[:5] == 'admin')
async def profile_manager(callback: types.CallbackQuery):
    temp = callback.data.split('_')

    buttons = InlineKeyboardBuilder()
    buttons.row(InlineKeyboardButton(text = 'Назад', callback_data = 'back'))

    user_data = await dbm.check_tg_id(callback.from_user.id)

    await callback.message.edit_text(f'Профиль:\nИмя: {callback.from_user.full_name}\nСсылка: tg://user?id={callback.from_user.id}\nTelegram_ID: {callback.from_user.id}\nDB_ID: {user_data[0]}\nГруппа: {user_data[1]}\nDebug режим: {'true' if user_data[2] == 1 else 'false'}', reply_markup=buttons.as_markup())

@dp.message()
async def dell_user_massage(message: types.Message):
    await message.delete()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
