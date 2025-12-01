from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import asyncio
import bleach
import json
import arrow
import config

white = 'Белая неделя'
green = 'Зелёная неделя'

async def update_schedule():
    schedule = {}

    with open(rf"{config.BASE_DIR}/settings/global.json", "r", encoding="utf_8_sig") as f:
        settings = json.loads(f.read())

    for i in settings['accounts'].keys():
        group = str(i)
        schedule[group] = {}
        login = settings['accounts'][group]['login']
        password = settings['accounts'][group]['password']

        async with async_playwright() as p:
            browser = await p.firefox.launch(headless=False)
            page = await browser.new_page()

            await page.goto("http://lk.stu.lipetsk.ru/", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            await page.wait_for_load_state("networkidle")

            await page.fill('input[name="LOGIN"]', login)
            await page.fill('input[name="PASSWORD"]', password)
            
            await page.click('button[type="submit"]')

            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)
            await page.wait_for_load_state("networkidle")

            while True:
                try:
                    await page.goto("http://lk.stu.lipetsk.ru/education/0/5:136841076/", wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                    await page.wait_for_load_state("networkidle")
                    
                    col = 0
                    ajax_response = ''

                    async def handle_response(response):
                        nonlocal ajax_response
                        nonlocal col
                        if "ajax.handler.php" in response.url:
                            col += 1
                            if col == 1:
                                try:
                                    body = await response.body()
                                    ajax_response = body.decode('cp1251')
                                except Exception as e:
                                    print(f"Ошибка при получении данных: {e}")

                    page.on("response", handle_response)

                    await page.reload()
                    await asyncio.sleep(2)
                    await page.wait_for_load_state("networkidle")

                    weak_color = await page.query_selector('div[role=alert]')
                    weak_color = await weak_color.text_content()

                    await browser.close()

                    soup = BeautifulSoup(ajax_response, 'html.parser')
                    s = soup.find('tbody')
                    count = 0

                    for i in s.select('td'):
                        clean = bleach.clean(str(i), tags=[], strip=True).strip()
                        
                        if clean != '':
                            if len(clean) == 2:
                                schedule[group][clean] = {}
                                count = 0
                            elif ' - ' in clean:
                                time = clean.split(' - ')
                                count += 1
                                schedule[group][list(schedule[group].keys())[-1]][str(count)] = {'time': {'start': time[0], 'end': time[1]}, white: {'title': '', 'teacher': '', 'room': '', 'type': ''}, green: {'title': '', 'teacher': '', 'room': '', 'type': ''}}
                            else:
                                if 'bgGreen' not in i.get('class'):
                                    tmp = schedule[group][list(schedule[group].keys())[-1]][str(count)][white]
                                elif 'bgGreen' in i.get('class'):
                                    tmp = schedule[group][list(schedule[group].keys())[-1]][str(count)][green]

                                if len(str(i).split('<br/>')) == 2:
                                    tmp['room'] = bleach.clean(str(i).split('<br/>')[0], tags=[], strip=True).strip()

                                    stype = bleach.clean(str(i).split('<br/>')[1], tags=[], strip=True).strip()

                                    if stype == "пр.":
                                        stype = 'практика'
                                    elif stype == "лек.":
                                        stype = 'лекция'
                                    elif stype == "лаб.":
                                        stype = 'лаборатоная'

                                    tmp['type'] = stype
                                elif len(str(i).split('<br/>')) == 3:
                                    tmp['title'] = bleach.clean(str(i).split('<br/>')[0], tags=[], strip=True).strip().capitalize()
                                    tmp['teacher'] = bleach.clean(str(i).split('<br/>')[2], tags=[], strip=True).strip()
                                else:
                                    tmp['title'] = bleach.clean(str(i), tags=[], strip=True).strip().capitalize()
                    break
                except Exception as e:
                    print(f"Ошибка при получении расписания для группы {group}: {e}")
            
            await browser.close()

    with open(rf"{config.BASE_DIR}/settings/schedule.json", 'w', encoding='utf-8') as f:
        json.dump(schedule, f, ensure_ascii=False, indent=4)

    settings['references']['date'] = arrow.now().format('DD.MM.YYYY')
    settings['references']['color'] = white if 'белая' in weak_color else green

    with open(rf"{config.BASE_DIR}/settings/global.json", 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)

