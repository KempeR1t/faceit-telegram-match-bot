#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="/home/kemper1t/.config/faceit_bot.env"
STATE_DIR="/home/kemper1t/.local/state/faceit_bot"
umask 077
mkdir -p "$STATE_DIR/logs"
chmod 700 "$STATE_DIR" "$STATE_DIR/logs"
exec 9>"$STATE_DIR/deploy.lock"
flock -n 9 || { echo 'Another run or update is active; skipped.'; exit 0; }
[[ -f "$ENV_FILE" && "$(stat -c %U "$ENV_FILE")" == "$(id -un)" && "$(stat -c %a "$ENV_FILE")" == 600 ]] || { echo 'Protected env file must belong to the current user with mode 600.' >&2; exit 2; }
LOG_FILE="$STATE_DIR/logs/cron.log"
if [[ -f "$LOG_FILE" && "$(stat -c %s "$LOG_FILE")" -ge 500000 ]]; then
    rm -f "$LOG_FILE.5"
    for i in 4 3 2 1; do
        [[ ! -f "$LOG_FILE.$i" ]] || mv "$LOG_FILE.$i" "$LOG_FILE.$((i+1))"
    done
    mv "$LOG_FILE" "$LOG_FILE.1"
fi
exec >>"$LOG_FILE" 2>&1
set -a
source "$ENV_FILE"
set +a
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo 'Waiting for TELEGRAM_BOT_TOKEN; state and success marker were not changed.'
    exit 0
fi
cd "$APP_DIR"
"$APP_DIR/.venv/bin/python" "$APP_DIR/bot.py" </dev/null
date -Is >"$STATE_DIR/last-success"
