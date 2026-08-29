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
- необязательный HTTP/SOCKS5-прокси только для запросов Telegram;
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
├── systemd/
│   └── telegram-proxy.service.example
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

## Подготовка Ubuntu 24.04

Сначала установить системные пакеты. `python3-venv` обязателен для создания
виртуального окружения `faceit_env`, а `python3-pip` устанавливает необходимые
компоненты pip:

```bash
sudo apt update
sudo apt install -y \
  git curl cron docker.io \
  python3 python3-venv python3-pip

sudo systemctl enable --now docker cron
python3 --version
sudo docker version
```

Повторный запуск `apt install` безопасен: уже установленные пакеты будут
пропущены. Отдельный системный пользователь для FlareSolverr или бота не
нужен.

## FlareSolverr для Rating и Swing

`faceit_rating` и `faceit_rating_swing` отсутствуют в публичном FACEIT Data
API. Бот получает их из scoreboard-summary через локальный FlareSolverr.

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
Swing отмечается компактным красным индикатором:

```text
🏁 Результат: 🔴 ПОРАЖЕНИЕ 😡

👤 Nickname
• Rating: 1.07 | 🔻 Swing: -0.71%
• Rating: 1.55 | 💚 Swing: +7.24%
```

Общий результат матча выводится под длительностью один раз. Он определяется по
первому игроку из порядка `players.json`, найденному в статистике матча.

Если обе попытки не прошли Cloudflare, FlareSolverr недоступен или ответ не
содержит нужных полей, уведомление всё равно будет отправлено — без Rating и
Swing.

## Установка бота

Команды выполняются от того обычного пользователя, в чьём `crontab` затем
будет запускаться бот:

```bash
cd ~
git clone https://github.com/KempeR1t/faceit-telegram-match-bot.git faceit_bot
cd faceit_bot

python3 -m venv faceit_env
faceit_env/bin/python3 -m pip install --upgrade pip
faceit_env/bin/python3 -m pip install -r requirements.txt

faceit_env/bin/python3 --version
faceit_env/bin/python3 -m pip --version
```

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
TELEGRAM_PROXY_URL=
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
| `TELEGRAM_PROXY_URL` | нет | HTTP/SOCKS5-прокси только для Telegram; пустое значение означает прямое подключение |
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

## Опциональный SSH-прокси для Telegram

Этот раздел нужен только тогда, когда VPS с ботом не может подключиться к
`api.telegram.org`, но имеется другая VPS с рабочим доступом к Telegram. Если
`TELEGRAM_PROXY_URL` отсутствует или оставлена пустой, бот подключается к
Telegram напрямую и никаких дополнительных действий не требуется.

Самый простой безопасный вариант — динамический SOCKS5-туннель OpenSSH.
Локальный SOCKS-порт будет слушать только `127.0.0.1` на VPS с ботом. На VPS
с доступом к Telegram отдельный прокси-сервер устанавливать не нужно:
используется уже работающий SSH-сервер.

В командах ниже заменить:

- `SSH_PORT` — порт SSH на VPS с доступом к Telegram;
- `PROXY_USER` — существующий пользователь этой VPS;
- `PROXY_VPS_IP` — её IP-адрес.

Все команды, кроме явно оговорённых, выполняются на VPS с ботом.

### Создание ключа и проверка SSH

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/telegram_proxy -C telegram-proxy -N ''

ssh-copy-id \
  -p SSH_PORT \
  -i ~/.ssh/telegram_proxy.pub \
  PROXY_USER@PROXY_VPS_IP

ssh \
  -p SSH_PORT \
  -i ~/.ssh/telegram_proxy \
  PROXY_USER@PROXY_VPS_IP \
  true
```

Последняя команда должна завершиться без ошибки. Если вход по паролю на
удалённой VPS отключён, содержимое `~/.ssh/telegram_proxy.pub` нужно добавить
в `~/.ssh/authorized_keys` существующего удалённого пользователя вручную.
Приватный файл `~/.ssh/telegram_proxy` копировать нельзя.

### Ручная проверка туннеля

В первом терминале VPS с ботом запустить:

```bash
ssh -NT \
  -p SSH_PORT \
  -D 127.0.0.1:1080 \
  -i ~/.ssh/telegram_proxy \
  -o IdentitiesOnly=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  PROXY_USER@PROXY_VPS_IP
```

Отсутствие вывода означает, что туннель работает. Во втором терминале:

```bash
curl \
  --proxy socks5h://127.0.0.1:1080 \
  --connect-timeout 10 \
  --max-time 20 \
  -o /dev/null \
  -w 'Telegram HTTP %{http_code}\n' \
  https://api.telegram.org
```

Любой HTTP-код, отличный от `000`, подтверждает доступ через прокси. После
проверки временный туннель обязательно остановить через `Ctrl+C`, иначе он
займёт порт 1080 и помешает запуску постоянной службы.

### Автозапуск SSH-туннеля

Создать пользовательскую службу:

```bash
cd ~/faceit_bot
mkdir -p ~/.config/systemd/user
cp systemd/telegram-proxy.service.example \
  ~/.config/systemd/user/telegram-proxy.service
nano ~/.config/systemd/user/telegram-proxy.service
```

Содержимое файла, в котором также нужно заменить три значения-заполнителя:

```ini
[Unit]
Description=SSH SOCKS proxy for Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -NT -p SSH_PORT -D 127.0.0.1:1080 -i %h/.ssh/telegram_proxy -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 PROXY_USER@PROXY_VPS_IP
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Включить запуск пользовательских служб после загрузки VPS и запустить
туннель:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now telegram-proxy.service
```

Проверка:

```bash
systemctl --user status telegram-proxy.service --no-pager
ss -lntp | grep ':1080'
```

Ожидается статус `active (running)`, а порт должен слушать только на
`127.0.0.1:1080`.

### Подключение бота к прокси

Добавить в локальный `~/faceit_bot/.env`:

```dotenv
TELEGRAM_PROXY_URL=socks5h://127.0.0.1:1080
```

Затем проверить конфигурацию и отправку:

```bash
cd ~/faceit_bot
set -a
. ./.env
set +a

faceit_env/bin/python3 bot.py --check-config
faceit_env/bin/python3 bot.py --test-telegram
```

В проверке конфигурации должно появиться `telegram_proxy=enabled`, а в Telegram
должно прийти тестовое сообщение. `run_bot.sh` и cron автоматически загружают
переменную из `.env`.

Поддерживаются адреса с протоколами `http://`, `https://`, `socks5://` и
`socks5h://`. Для SSH-туннеля рекомендуется `socks5h://`, чтобы имя
`api.telegram.org` разрешалось на VPS с рабочим доступом.

Настройка применяется только к Telegram. FACEIT Data API и локальный
FlareSolverr продолжают работать напрямую. Конфликта с FlareSolverr нет:
обычно он слушает `127.0.0.1:8191`, а SOCKS-туннель — `127.0.0.1:1080` на VPS
с ботом. На удалённой VPS используется только её существующий SSH-порт.

Диагностика туннеля:

```bash
journalctl --user -u telegram-proxy.service -n 100 --no-pager
```

Если служба завершается с кодом `255`, точная причина будет в этой команде.
Сообщение `Address already in use` означает, что порт 1080 всё ещё занят
ручным SSH-туннелем.

## Ручной запуск

```bash
chmod 755 run_bot.sh
./run_bot.sh
```

При `NOTIFY_ON_FIRST_RUN=false` первое наблюдаемое состояние каждого нового
игрока сохраняется без отправки старого матча. Следующие завершённые матчи уже
создают уведомления.

## Запуск через cron на Ubuntu 24.04

`cron` уже установлен и включён на этапе подготовки Ubuntu. Проверить сервис:

```bash
systemctl is-active cron
```

Подготовить скрипт запуска и файл лога:

```bash
cd ~/faceit_bot
chmod 755 run_bot.sh
touch bot.log
chmod 600 bot.log
```

Открыть пользовательский crontab:

```bash
crontab -e
```

Добавить строку:

```cron
*/15 * * * * "$HOME/faceit_bot/run_bot.sh" >> "$HOME/faceit_bot/bot.log" 2>&1
```

Задание запускается от владельца этого `crontab`; отдельный пользователь не
нужен. Cron автоматически задаёт `$HOME` для этого пользователя. Скрипт
`run_bot.sh` сам переходит в каталог проекта, загружает `.env` и запускает
Python из `faceit_env`.

Проверить сохранённое задание:

```bash
crontab -l
```

Не дожидаясь следующей четверти часа, можно выполнить ту же команду вручную и
проверить лог:

```bash
cd ~/faceit_bot
./run_bot.sh >> bot.log 2>&1
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
- Telegram недоступен напрямую — проверить `TELEGRAM_PROXY_URL`, статус
  `telegram-proxy.service` и доступ через `curl --proxy` по инструкции выше.
- `No chats found` — отправить боту новое сообщение или команду и повторить
  `tools/list_telegram_chats.py`.
- Ошибка чтения `last_matches.json` — проверить JSON или восстановить файл из
  резервной копии; повреждённый файл не перезаписывается автоматически.
