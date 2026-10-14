#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
for script in deploy/*.sh; do bash -n "$script"; done
.venv/bin/python -m py_compile bot.py deploy/preview.py deploy/replay.py deploy/set-telegram-token.py
.venv/bin/python -m unittest discover -s tests -q
set -a
source "$HOME/.config/faceit_bot.env"
set +a
# Validate all other settings before the user supplies a Telegram token.
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    export TELEGRAM_BOT_TOKEN=config-validation-only
fi
.venv/bin/python bot.py --check-config
