#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$HOME/.local/state/faceit_bot"
umask 077
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
exec 9>"$STATE_DIR/deploy.lock"
flock -n 9 || { echo 'Update aborted: a run or update is active.' >&2; exit 1; }
cd "$APP_DIR"
[[ "$(git branch --show-current)" == main ]] || { echo 'Expected main branch.' >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo 'Checkout is dirty; publish code through GitHub.' >&2; exit 1; }
git fetch --prune origin main </dev/null
git merge-base --is-ancestor HEAD origin/main || { echo 'Update is not a fast-forward.' >&2; exit 1; }
git merge --ff-only origin/main
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt </dev/null
.venv/bin/python -m pip check
./deploy/validate.sh
[[ -z "$(git status --porcelain)" ]] || { echo 'Validation left checkout changes.' >&2; exit 1; }
echo "Updated to $(git rev-parse --short HEAD); cron jobs were not started."
