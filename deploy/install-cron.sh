#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Cron command paths must not contain shell or crontab metacharacters.
[[ "$APP_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]] || { echo 'Unsupported checkout path for cron.' >&2; exit 1; }
umask 077
STATE_DIR="$HOME/.local/state/faceit_bot"
BACKUP_DIR="$HOME/.config/deployment-backups/faceit-bot-crontabs"
mkdir -p "$STATE_DIR" "$BACKUP_DIR"
chmod 700 "$STATE_DIR" "$BACKUP_DIR"
CURRENT=$(mktemp)
NEW=$(mktemp)
trap 'rm -f "$CURRENT" "$NEW"' EXIT
crontab -l >"$CURRENT" 2>/dev/null || true
install -m 600 "$CURRENT" "$BACKUP_DIR/crontab.$(date +%Y%m%dT%H%M%S)"
awk -v app="$APP_DIR" '$0 == "# BEGIN CODEX MANAGED: faceit_bot" {skip=1; next}
     $0 == "# END CODEX MANAGED: faceit_bot" {skip=0; next}
     !skip && !index($0, app "/run_bot.sh") && !index($0, app "/deploy/run.sh") {print}' "$CURRENT" >"$NEW"
printf '%s\n' '# BEGIN CODEX MANAGED: faceit_bot' \
 "*/10 * * * * $APP_DIR/deploy/run.sh" \
 '# END CODEX MANAGED: faceit_bot' >>"$NEW"
crontab "$NEW"
echo 'Managed cron installed: every 10 minutes. Empty Telegram token safely skips runs.'
