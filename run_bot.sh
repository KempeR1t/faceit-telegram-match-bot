#!/bin/sh
set -eu

BOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_PATH="$BOT_DIR/.env"
PYTHON_PATH="$BOT_DIR/faceit_env/bin/python3"

if [ ! -r "$ENV_PATH" ]; then
    echo "Configuration file is missing or unreadable: $ENV_PATH" >&2
    exit 2
fi

if [ ! -x "$PYTHON_PATH" ]; then
    echo "Python virtual environment is missing: $PYTHON_PATH" >&2
    exit 2
fi

set -a
. "$ENV_PATH"
set +a

exec "$PYTHON_PATH" "$BOT_DIR/bot.py"
