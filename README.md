# Schedule Bot

Telegram-бот с расписанием Липецкого колледжа. Бот самостоятельно входит в личный кабинет, получает расписание всех настроенных групп и обновляет локальный кеш без перезапуска.

## Что теперь работает автоматически

- обновление расписания при запуске и затем каждые 6 часов;
- до трёх повторных попыток для каждой группы при временной ошибке сайта;
- фоновая загрузка: бот отвечает пользователям, пока идёт обновление;
- безопасная атомарная запись — неудачное обновление не уничтожает старое расписание;
- мгновенная перезагрузка данных в памяти после успешного получения;
- работа через локальный Telegram Bot API или через официальный API;
- ручное обновление из админ-панели и командой `/update`;
- сохранение расписания и базы данных в Docker volumes.

## Быстрый запуск через Docker Compose

Требуются Docker и Docker Compose. Локальный Telegram Bot API должен быть уже запущен — этот репозиторий его не дублирует.

```bash
git clone https://github.com/Not-Config/Schedule-bot-for-my-college.git
cd Schedule-bot-for-my-college
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f bot
```

Обязательно заполните в `.env`:

- `BOT_TOKEN` — токен бота;
- `SCHEDULE_ACCOUNTS_JSON` — логин и пароль кабинета для каждой группы;
- `DB_PASSWORD` и `DB_ROOT_PASSWORD` — новые пароли MariaDB.

Если локальный Bot API слушает порт `8081` на том же Linux-сервере, оставьте:

```dotenv
BOT_API_BASE_URL=http://host.docker.internal:8081
BOT_API_IS_LOCAL=true
```

Если бот и Bot API находятся в одной Docker-сети, вместо адреса хоста укажите имя сервиса, например `http://telegram-bot-api:8081`, и подключите контейнер бота к этой сети. Чтобы временно вернуться к официальному API Telegram, оставьте `BOT_API_BASE_URL` пустым.

## Настройка автообновления

Основные переменные:

| Переменная | Значение по умолчанию | Назначение |
|---|---:|---|
| `SCHEDULE_UPDATE_ON_STARTUP` | `true` | обновить расписание сразу после запуска |
| `SCHEDULE_UPDATE_INTERVAL_MINUTES` | `360` | интервал фонового обновления |
| `SCHEDULE_UPDATE_MAX_ATTEMPTS` | `3` | число попыток на одну группу |
| `SCHEDULE_UPDATE_RETRY_SECONDS` | `10` | базовая пауза между попытками |
| `SCHEDULE_PAGE_TIMEOUT_SECONDS` | `35` | тайм-аут страниц и AJAX-ответа |
| `SCHEDULE_BROWSER` | `chromium` | браузер Playwright |
| `SCHEDULE_HEADLESS` | `true` | запуск браузера без окна |
| `BOT_TIMEZONE` | `Europe/Moscow` | часовой пояс расписания |

Интервал меньше пяти минут намеренно не допускается, чтобы не нагружать сайт колледжа.

Аккаунты передаются одним JSON-объектом:

```dotenv
SCHEDULE_ACCOUNTS_JSON='{"Т9-ИП-24-1":{"login":"login1","password":"password1"},"Т9-ИП-24-2":{"login":"login2","password":"password2"}}'
```

Не добавляйте `.env` в Git — файл уже находится в `.gitignore`.

## База данных

При обычном `docker compose up` запускается MariaDB, а таблицы из `schema.sql` создаются при первом старте. Старую внешнюю MySQL/MariaDB тоже можно использовать: задайте `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, примените `schema.sql` и запустите только бота:

```bash
docker compose up -d --build --no-deps bot
```

Администраторов можно указать массивом Telegram ID в `settings/global.json`. Поле `is_admin` в базе также поддерживается.

В админ-панели доступно глобальное оповещение. Администратор отправляет текст, фото, видео, документ или другое обычное сообщение Telegram, подтверждает рассылку и получает статистику доставки. Сообщение копируется всем зарегистрированным пользователям с ограничением скорости и обработкой блокировок бота.

## Как хранится расписание

`settings/schedule.json` используется как начальный резервный кеш. Свежие данные записываются в `data/schedule.json`, а дата и цвет эталонной недели — в `data/reference.json`. В Docker каталог `data` подключён к постоянному volume.

Если сайт временно недоступен, бот пишет ошибку в лог и продолжает показывать последний успешно полученный вариант. Следующая попытка произойдёт автоматически.

## Локальная разработка и тесты

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest
```
