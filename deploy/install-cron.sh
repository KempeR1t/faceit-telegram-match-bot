#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -un)" == kemper1t ]] || { echo 'Run as kemper1t.' >&2; exit 1; }
umask 077
STATE_DIR=/home/kemper1t/.local/state/faceit_bot
BACKUP_DIR=/home/kemper1t/backups/faceit-bot-crontabs
mkdir -p "$STATE_DIR" "$BACKUP_DIR"
chmod 700 "$STATE_DIR" "$BACKUP_DIR"
CURRENT=$(mktemp)
NEW=$(mktemp)
trap 'rm -f "$CURRENT" "$NEW"' EXIT
crontab -l >"$CURRENT" 2>/dev/null || true
install -m 600 "$CURRENT" "$BACKUP_DIR/crontab.$(date +%Y%m%dT%H%M%S)"
awk '$0 == "# BEGIN CODEX MANAGED: faceit_bot" {skip=1; next}
     $0 == "# END CODEX MANAGED: faceit_bot" {skip=0; next}
     !skip && $0 !~ /\/home\/kemper1t\/faceit_bot\/(run_bot\.sh|deploy\/run\.sh)/ {print}' "$CURRENT" >"$NEW"
printf '%s\n' '# BEGIN CODEX MANAGED: faceit_bot' \
 '*/10 * * * * /home/kemper1t/faceit_bot/deploy/run.sh' \
 '# END CODEX MANAGED: faceit_bot' >>"$NEW"
crontab "$NEW"
echo 'Managed cron installed: every 10 minutes. Empty Telegram token safely skips runs.'
