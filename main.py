from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import CommandObject, CommandStart
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

#########################################################################################
#                       Загрузка настроек и дополнительных файлов                       #
#########################################################################################

try:
    with open(r"./settings/global.json", "r", encoding="utf_8_sig") as f:
        settings = json.loads(f.read())
except:
    print('Не удалось загрузить глобальные настройки')

try:
    with open(r"./settings/schedule.json", "r", encoding="utf_8_sig") as f:
        schedule = json.loads(f.read())
except:
    print('Не удалось загрузить расписание')

try:
    with open(r"./settings/addresses.json", "r", encoding="utf_8_sig") as f:
        adresses = json.loads(f.read())
except:
    print('Не удалось загрузить адреса')

#########################################################################################
#                                 Основная логика бота                                  #
#########################################################################################

@dp.message(CommandStart())
@dp.callback_query(F.data == 'back')
async def start(event: types.Message | types.CallbackQuery):
    buttons = InlineKeyboardBuilder()
    buttons.row(InlineKeyboardButton(text = 'Расписание', callback_data = f'schedule'))

    if isinstance(event, types.Message):
        message = event.answer
    else:
        message = event.message.edit_text

    await message('Добро пожаловать в бот расписания!', reply_markup=buttons.as_markup())

@dp.callback_query(F.data[:8] == 'schedule')
async def schedule_manager(callback: types.CallbackQuery):
    s = callback.data.split('_')

    if len(s) == 1:
        s.append(arrow.now().format("DD.MM.YYYY"))

    buttons = InlineKeyboardBuilder()
    buttons.row(InlineKeyboardButton(text = '⏪', callback_data = f'schedule_{await tm.get_next_previous(s[1], "extra_previous")}'), InlineKeyboardButton(text = '◀️', callback_data = f'schedule_{await tm.get_next_previous(s[1], "previous")}'), InlineKeyboardButton(text = '🔄️', callback_data = f'schedule'), InlineKeyboardButton(text = '▶️', callback_data = f'schedule_{await tm.get_next_previous(s[1], "next")}'), InlineKeyboardButton(text = '⏩', callback_data = f'schedule_{await tm.get_next_previous(s[1], "extra_next")}'))
    buttons.row(InlineKeyboardButton(text = 'Назад', callback_data = 'back'))
    group = (await dbm.check_tg_id(callback.from_user.id))[1]
    day, color = await tm.get_this_weekday(settings["references"]["date"], settings["references"]["color"], date=s[1])
    
    text = f'<b>{day} - {color} ({arrow.get(s[1], "DD.MM.YYYY").format("DD.MM")})</b>\n\n'

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
                    for corpuse in adresses.keys():
                        for flore in adresses[corpuse].keys():
                            if str(schedule_fd[i][color]["room"]).isdigit() and str(schedule_fd[i][color]["room"]) != 'спортзал':
                                if schedule_fd[i][color]["room"] >= adresses[corpuse][flore]["min"] and schedule_fd[i][color]["room"] <= adresses[corpuse][flore]["max"]:
                                    adress = f'{corpuse} {schedule_fd[i][color]["room"]} {schedule_fd[i][color]["type"]}'
                            elif str(schedule_fd[i][color]["room"])[0] == '9':
                                adress = f'9-й корпус {str(schedule_fd[i][color]["room"])[2:]} {schedule_fd[i][color]["type"]}'

                    text += f'{adress}\n\n'
    except:
        text += 'Выходной'

    try:
        await callback.message.edit_text(text, reply_markup=buttons.as_markup(), parse_mode='HTML')
    except:
        pass

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
