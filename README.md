# FACEIT Match Bot

Python-бот для отслеживания завершённых матчей FACEIT. Бот получает основную
статистику через FACEIT Data API, дополняет её Rating и Swing через локальный
FlareSolverr и отправляет сводку в Telegram.

Один запуск `bot.py` выполняет одну проверку и завершается. Для регулярной
работы проект можно запускать через cron, например раз в 15 минут.

## Возможности

- отслеживание нескольких FACEIT-игроков;
- одно уведомление для игроков, участвовавших в одном матче;
- карта, счёт, длительность, результат, Rating, Swing, K/D, ADR и MVP;
- защита от повторных уведомлений через `last_matches.json`;
- повторная попытка на следующем запуске, если Telegram не принял сообщение;
- секреты через переменные окружения, список игроков в отдельном JSON-файле;
- таймауты и повторные GET-запросы при временных ошибках FACEIT API;
- совместимость с Python 3.10+ и Ubuntu 24.04.

## Структура проекта

```text
.
├── bot.py
├── run_bot.sh
├── requirements.txt
├── .env.example
├── players.example.json
├── tools/
│   ├── resolve_faceit_players.py
│   └── list_telegram_chats.py
└── tests/
    └── test_bot.py
```

## Требования

- Python 3.10 или новее;
- FACEIT server-side API key;
- Telegram-бот;
- FACEIT player ID отслеживаемых игроков.
- локальный FlareSolverr — для необязательных полей Rating и Swing.

## Получение FACEIT API key

1. Войти в [FACEIT Developer Portal](https://developers.faceit.com/).
2. Создать приложение в разделе **App Studio**.
3. Открыть приложение и перейти в раздел **API Keys**.
4. Создать ключ типа **Server side**.

Для чтения публичных данных через Data API OAuth client secret не требуется.

Документация FACEIT:

- [Start here](https://docs.faceit.com/getting-started/intro/start-here/)
- [API Keys](https://docs.faceit.com/getting-started/authentication/api-keys/)
- [Data API](https://docs.faceit.com/docs/data-api/)

## Получение FACEIT player ID

FACEIT player ID — UUID игрока, например
`00000000-0000-0000-0000-000000000000`. Steam ID и URL профиля вместо него не
подойдут.

После установки зависимостей player ID можно получить по никнейму:

```bash
read -rsp "FACEIT API key: " FACEIT_API_KEY; echo
export FACEIT_API_KEY
faceit_env/bin/python3 tools/resolve_faceit_players.py \
  Nickname1 Nickname2 \
  --output players.json
unset FACEIT_API_KEY
```

Скрипт найдёт ID и создаст готовый `players.json`. Если файл уже существует,
его можно отредактировать вручную или явно заменить с помощью `--force`.

## Создание Telegram-бота

1. Открыть [@BotFather](https://t.me/BotFather).
2. Выполнить команду `/newbot`.
3. Задать имя и username бота.
4. Сохранить полученный токен в `TELEGRAM_BOT_TOKEN`.

Для личного чата необходимо открыть созданного бота и отправить ему `/start`.
Для группы — добавить бота в группу и отправить команду, например `/start`.

После этого chat ID можно получить вспомогательным скриптом:

```bash
read -rsp "Telegram bot token: " TELEGRAM_BOT_TOKEN; echo
export TELEGRAM_BOT_TOKEN
faceit_env/bin/python3 tools/list_telegram_chats.py
unset TELEGRAM_BOT_TOKEN
```

Значение `id=...` из вывода используется как `TELEGRAM_CHAT_ID`. У групп и
каналов ID обычно отрицательный.

Документация Telegram:

- [создание бота](https://core.telegram.org/bots/tutorial)
- [`getUpdates`](https://core.telegram.org/bots/api#getupdates)

## Установка

```bash
git clone https://github.com/KempeR1t/faceit-telegram-match-bot.git faceit_bot
cd faceit_bot

python3 -m venv faceit_env
faceit_env/bin/python3 -m pip install --upgrade pip
faceit_env/bin/python3 -m pip install -r requirements.txt
```

## FlareSolverr для Rating и Swing

`faceit_rating` и `faceit_rating_swing` отсутствуют в публичном FACEIT Data
API. Бот получает их из scoreboard-summary через локальный FlareSolverr.

### Установка Docker на Ubuntu 24.04

Если Docker уже установлен, этот шаг можно пропустить. Для установки версии из
репозитория Ubuntu достаточно выполнить:

```bash
sudo apt update
sudo apt install -y docker.io curl
sudo systemctl enable --now docker
sudo docker version
```

Отдельный системный пользователь для FlareSolverr или бота не нужен. Команды
управления Docker выполняются через `sudo`, а бот продолжает запускаться от
обычного текущего пользователя.

### Запуск FlareSolverr

Контейнер публикует API только на `127.0.0.1`, поэтому порт 8191 недоступен из
интернета:

```bash
sudo docker run -d \
  --name=flaresolverr \
  -p 127.0.0.1:8191:8191 \
  -e LOG_LEVEL=info \
  --restart unless-stopped \
  ghcr.io/flaresolverr/flaresolverr:v3.5.0
```

Параметр `--restart unless-stopped` автоматически поднимет контейнер после
перезагрузки VPS. Если контейнер с таким именем уже создан, повторять
`docker run` не нужно — достаточно запустить его:

```bash
sudo docker update --restart unless-stopped flaresolverr
sudo docker start flaresolverr
```

Первая команда включает автозапуск для уже существующего контейнера без его
пересоздания.

Проверка API, состояния и последних логов:

```bash
curl -fsS http://127.0.0.1:8191/ | python3 -m json.tool
sudo docker ps --filter name=flaresolverr
sudo docker logs --tail 100 flaresolverr
```

В ответе первой команды должно быть сообщение `FlareSolverr is ready!`.
Перезапустить сервис вручную можно командой
`sudo docker restart flaresolverr`.

Для обработки нового матча бот создаёт уникальную браузерную сессию и сразу
запрашивает scoreboard-summary. Если первый запрос завершается ошибкой, бот
через 5 секунд повторяет его в той же сессии. После работы бот удаляет только
свою сессию. Строка дополнительной статистики выводится над K/D; отрицательный
Swing отмечается красным индикатором:

```text
• Rating: 1.07 | 🔴 Swing: -0.71%
```

Если обе попытки не прошли Cloudflare, FlareSolverr недоступен или ответ не
содержит нужных полей, уведомление всё равно будет отправлено — без Rating и
Swing.

## Настройка

Создать локальный файл конфигурации:

```bash
cp .env.example .env
cp players.example.json players.json
chmod 600 .env
chmod 600 players.json
nano .env
nano players.json
```

Пример:

```dotenv
FACEIT_API_KEY=faceit_api_key
TELEGRAM_BOT_TOKEN=telegram_bot_token
TELEGRAM_CHAT_ID=-1001234567890
FACEIT_PLAYERS_FILE=players.json

FACEIT_GAME=cs2
APP_TIMEZONE=Europe/Moscow
STATE_FILE=last_matches.json
REQUEST_TIMEOUT_SECONDS=15
FLARESOLVERR_ENABLED=true
FLARESOLVERR_URL=http://127.0.0.1:8191/v1
FLARESOLVERR_MAX_TIMEOUT_MS=120000
NOTIFY_ON_FIRST_RUN=false
LOG_LEVEL=INFO
```

Файл `.env` исключён из Git. Значения из него загружаются в окружение процесса
скриптом `run_bot.sh`.

Игроки настраиваются отдельно в `players.json`:

```json
{
  "11111111-1111-4111-8111-111111111111": "Nickname1",
  "22222222-2222-4222-8222-222222222222": "Nickname2"
}
```

`players.json` также исключён из Git. В репозитории находится только безопасный
шаблон `players.example.json`.

### Переменные окружения

| Переменная | Обязательна | Назначение |
|---|---:|---|
| `FACEIT_API_KEY` | да | server-side API key FACEIT |
| `TELEGRAM_BOT_TOKEN` | да | токен Telegram-бота |
| `TELEGRAM_CHAT_ID` | да | ID личного чата, группы или канала |
| `FACEIT_PLAYERS_FILE` | нет | путь к списку игроков, по умолчанию `players.json` |
| `FACEIT_GAME` | нет | идентификатор игры, по умолчанию `cs2` |
| `APP_TIMEZONE` | нет | часовой пояс, по умолчанию `Europe/Moscow` |
| `STATE_FILE` | нет | файл состояния, по умолчанию `last_matches.json` |
| `REQUEST_TIMEOUT_SECONDS` | нет | HTTP-таймаут, по умолчанию 15 секунд |
| `FLARESOLVERR_ENABLED` | нет | добавлять Rating и Swing через FlareSolverr, по умолчанию `true` |
| `FLARESOLVERR_URL` | нет | адрес API FlareSolverr, по умолчанию `http://127.0.0.1:8191/v1` |
| `FLARESOLVERR_MAX_TIMEOUT_MS` | нет | ожидание решения Cloudflare, по умолчанию 120000 мс |
| `NOTIFY_ON_FIRST_RUN` | нет | отправлять ли последний матч при первом запуске |
| `LOG_LEVEL` | нет | `DEBUG`, `INFO`, `WARNING`, `ERROR` или `CRITICAL` |

Формат файла игроков:

```json
{
  "11111111-1111-4111-8111-111111111111": "Nickname1",
  "22222222-2222-4222-8222-222222222222": "Nickname2"
}
```

Относительный путь в `FACEIT_PLAYERS_FILE` считается от корня проекта. Бот
проверяет корректность JSON, UUID, пустые никнеймы и повторяющиеся ID.

## Проверка конфигурации

```bash
set -a
. ./.env
set +a

faceit_env/bin/python3 bot.py --check-config
```

Команда проверяет переменные окружения без сетевых запросов и не выводит
значения ключей.

## Проверка Telegram

```bash
set -a
. ./.env
set +a

faceit_env/bin/python3 bot.py --test-telegram
```

В указанный чат должно прийти тестовое сообщение.

## Ручной запуск

```bash
chmod 755 run_bot.sh
./run_bot.sh
```

При `NOTIFY_ON_FIRST_RUN=false` первое наблюдаемое состояние каждого нового
игрока сохраняется без отправки старого матча. Следующие завершённые матчи уже
создают уведомления.

## Запуск через cron на Ubuntu 24.04

Установить необходимые пакеты и включить cron:

```bash
sudo apt update
sudo apt install -y cron git python3 python3-venv
sudo systemctl enable --now cron
```

Открыть пользовательский crontab:

```bash
crontab -e
```

Добавить строку, заменив `USERNAME` абсолютным путём к домашнему каталогу:

```cron
*/15 * * * * /home/USERNAME/faceit_bot/run_bot.sh >> /home/USERNAME/faceit_bot/bot.log 2>&1
```

Подготовить файл лога:

```bash
touch bot.log
chmod 600 bot.log
```

Проверка:

```bash
crontab -l
tail -n 100 bot.log
```

## Файл состояния

`last_matches.json` содержит последний обработанный match ID для каждого
игрока. Файл создаётся автоматически, записывается атомарно и исключён из Git.

Для переноса работающей установки достаточно скопировать этот файл в корень
проекта и установить права:

```bash
chmod 600 last_matches.json
```

Если файл отсутствует, бот создаст его при первом запуске.

## Обновление

```bash
git pull --ff-only
faceit_env/bin/python3 -m pip install -r requirements.txt
./run_bot.sh
```

## Тесты

```bash
faceit_env/bin/python3 -m unittest discover -s tests -v
faceit_env/bin/python3 -m compileall -q bot.py tools tests
sh -n run_bot.sh
```

## Диагностика

- `HTTP 401` от FACEIT — неверный или отозванный API key.
- `HTTP 403` от FACEIT — ключ отключён либо запрос запрещён настройками
  приложения.
- `HTTP 429` — превышен лимит API; бот повторит GET-запрос с задержкой.
- `FlareSolverr failed ... Timeout` — Cloudflare не удалось пройти за заданное
  время; сообщение будет отправлено без Rating и Swing.
- FlareSolverr не используется — проверить `FLARESOLVERR_ENABLED`, адрес
  `FLARESOLVERR_URL` и состояние контейнера командами
  `sudo docker ps --filter name=flaresolverr` и
  `sudo docker logs --tail 100 flaresolverr`.
- Telegram не отправляет сообщения — проверить токен, chat ID и наличие бота в
  целевом чате.
- `No chats found` — отправить боту новое сообщение или команду и повторить
  `tools/list_telegram_chats.py`.
- Ошибка чтения `last_matches.json` — проверить JSON или восстановить файл из
  резервной копии; повреждённый файл не перезаписывается автоматически.
