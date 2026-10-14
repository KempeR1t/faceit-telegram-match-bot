#!/usr/bin/env python3
"""Ask for a Telegram token locally, validate access, and save it atomically."""
import getpass
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile

import requests

ENV_FILE = Path.home() / ".config/faceit_bot.env"

def main():
    mode = ENV_FILE.stat()
    if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) != 0o600:
        raise SystemExit("Env file must belong to the current user and have mode 600.")
    token = getpass.getpass("Telegram bot token (hidden): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Invalid token format; file was not changed.")
    text = ENV_FILE.read_text()
    chat_settings = re.findall(r"(?m)^TELEGRAM_CHAT_ID=(.*)$", text)
    if len(chat_settings) != 1:
        raise SystemExit("Expected exactly one chat ID setting; file was not changed.")
    chat_parts = shlex.split(chat_settings[0])
    if len(chat_parts) != 1 or not re.fullmatch(r"-?[0-9]+", chat_parts[0]):
        raise SystemExit("Expected a numeric chat ID; file was not changed.")
    proxy_settings = re.findall(r"(?m)^TELEGRAM_PROXY_URL=(.*)$", text)
    if len(proxy_settings) > 1:
        raise SystemExit("Expected at most one proxy setting; file was not changed.")
    proxy_parts = shlex.split(proxy_settings[0]) if proxy_settings else []
    if len(proxy_parts) > 1:
        raise SystemExit("Invalid proxy setting; file was not changed.")
    proxy_url = proxy_parts[0] if proxy_parts else ""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    with requests.Session() as session:
        session.trust_env = False
        for method, params in (("getMe", None), ("getChat", {"chat_id": chat_parts[0]})):
            try:
                r = session.get("https://api.telegram.org/bot" + token + "/" + method,
                                params=params, timeout=20, proxies=proxies)
                data = r.json()
            except (requests.RequestException, ValueError):
                raise SystemExit("Telegram validation failed; file was not changed.") from None
            if r.status_code != 200 or not isinstance(data, dict) or not data.get("ok"):
                raise SystemExit("Telegram rejected " + method + "; file was not changed.")
    line = "TELEGRAM_BOT_TOKEN=" + shlex.quote(token)
    updated, n = re.subn(r"(?m)^TELEGRAM_BOT_TOKEN=.*$", lambda _: line, text)
    if n != 1:
        raise SystemExit("Expected exactly one token setting; file was not changed.")
    fd, name = tempfile.mkstemp(dir=ENV_FILE.parent, prefix=".faceit-token-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, ENV_FILE)
    finally:
        Path(name).unlink(missing_ok=True)
    print("Token saved to " + str(ENV_FILE) + ". Cron will send pending match notifications on its next run.")

if __name__ == "__main__":
    main()
