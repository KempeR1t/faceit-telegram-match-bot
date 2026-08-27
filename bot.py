#!/usr/bin/env python3
"""Send Telegram notifications about completed FACEIT matches.

The program performs one polling cycle and exits. In production it is intended
to be started periodically by cron.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


VERSION = "1.0.0"
BASE_DIR = Path(__file__).resolve().parent
FACEIT_API_BASE = "https://open.faceit.com/data/v4"
TELEGRAM_API_BASE = "https://api.telegram.org"
LOGGER = logging.getLogger("faceit_match_bot")


class ConfigurationError(ValueError):
    """Raised when an environment variable is missing or invalid."""


class StateError(RuntimeError):
    """Raised when the local state cannot be safely read or written."""


@dataclass(frozen=True)
class Config:
    faceit_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    players: dict[str, str]
    game_id: str
    timezone: ZoneInfo
    timezone_name: str
    state_file: Path
    request_timeout: float
    notify_on_first_run: bool
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        faceit_api_key = required_env("FACEIT_API_KEY")
        telegram_bot_token = required_env("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = required_env("TELEGRAM_CHAT_ID")

        raw_players_file = os.getenv("FACEIT_PLAYERS_FILE", "players.json").strip()
        if not raw_players_file:
            raise ConfigurationError("FACEIT_PLAYERS_FILE must not be empty.")
        players_file = Path(raw_players_file).expanduser()
        if not players_file.is_absolute():
            players_file = BASE_DIR / players_file
        players = load_players_file(players_file)

        game_id = os.getenv("FACEIT_GAME", "cs2").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", game_id):
            raise ConfigurationError(
                "FACEIT_GAME may contain only letters, digits, '_' and '-'."
            )

        timezone_name = os.getenv("APP_TIMEZONE", "Europe/Moscow").strip()
        try:
            app_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(
                f"Unknown APP_TIMEZONE: {timezone_name!r}."
            ) from exc

        raw_state_file = os.getenv("STATE_FILE", "last_matches.json").strip()
        if not raw_state_file:
            raise ConfigurationError("STATE_FILE must not be empty.")
        state_file = Path(raw_state_file).expanduser()
        if not state_file.is_absolute():
            state_file = BASE_DIR / state_file

        request_timeout = parse_float_env(
            "REQUEST_TIMEOUT_SECONDS", default=15.0, minimum=1.0, maximum=120.0
        )
        notify_on_first_run = parse_bool_env("NOTIFY_ON_FIRST_RUN", default=False)

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError(
                "LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL."
            )

        return cls(
            faceit_api_key=faceit_api_key,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            players=players,
            game_id=game_id,
            timezone=app_timezone,
            timezone_name=timezone_name,
            state_file=state_file,
            request_timeout=request_timeout,
            notify_on_first_run=notify_on_first_run,
            log_level=log_level,
        )


@dataclass(frozen=True)
class LatestMatchResult:
    ok: bool
    match_id: str | None


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.lower() in {"change_me", "replace_me", "changeme"}:
        raise ConfigurationError(f"Required environment variable {name} is not set.")
    return value


def json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"Duplicate key in players file: {key}.")
        result[key] = value
    return result


def load_players_file(players_file: Path) -> dict[str, str]:
    """Load and validate a JSON object mapping FACEIT player IDs to nicknames."""
    try:
        with players_file.open("r", encoding="utf-8") as file_handle:
            payload = json.load(
                file_handle, object_pairs_hook=json_object_without_duplicates
            )
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"Players file not found: {players_file}. Copy players.example.json "
            "to players.json and edit it."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Players file contains invalid JSON: {players_file} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc
    except UnicodeError as exc:
        raise ConfigurationError(
            f"Players file must be valid UTF-8: {players_file}."
        ) from exc
    except OSError as exc:
        raise ConfigurationError(f"Cannot read players file: {players_file}.") from exc

    if not isinstance(payload, dict):
        raise ConfigurationError("Players file must contain a JSON object.")

    players: dict[str, str] = {}
    for raw_player_id, raw_nickname in payload.items():
        if not isinstance(raw_player_id, str) or not isinstance(raw_nickname, str):
            raise ConfigurationError(
                "Every players file entry must map a player ID string to a "
                "nickname string."
            )

        try:
            player_id = str(UUID(raw_player_id.strip()))
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid FACEIT player ID in players file: {raw_player_id!r}."
            ) from exc

        nickname = raw_nickname.strip()
        if not nickname:
            raise ConfigurationError(
                f"Nickname is missing for FACEIT player {player_id}."
            )
        if "\n" in nickname or "\r" in nickname:
            raise ConfigurationError(
                f"Nickname for FACEIT player {player_id} contains a line break."
            )
        if player_id in players:
            raise ConfigurationError(
                f"Duplicate FACEIT player ID after UUID normalization: {player_id}."
            )

        players[player_id] = nickname

    if not players:
        raise ConfigurationError("Players file must contain at least one player.")
    return players


def parse_bool_env(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def parse_float_env(
    name: str, *, default: float, minimum: float, maximum: float
) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number.") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum:g} and {maximum:g}."
        )
    return value


def build_http_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": f"faceit-telegram-match-bot/{VERSION}"})
    return session


class FaceitClient:
    def __init__(
        self, session: requests.Session, api_key: str, request_timeout: float
    ) -> None:
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        self._request_timeout = request_timeout

    def _get_json(
        self, path: str, *, params: dict[str, Any] | None, operation: str
    ) -> dict[str, Any] | None:
        try:
            response = self._session.get(
                f"{FACEIT_API_BASE}{path}",
                headers=self._headers,
                params=params,
                timeout=self._request_timeout,
            )
        except requests.RequestException as exc:
            LOGGER.warning(
                "FACEIT request failed during %s (%s).",
                operation,
                type(exc).__name__,
            )
            return None

        if response.status_code != 200:
            LOGGER.warning(
                "FACEIT returned HTTP %s during %s.",
                response.status_code,
                operation,
            )
            return None

        try:
            payload = response.json()
        except requests.JSONDecodeError:
            LOGGER.warning("FACEIT returned invalid JSON during %s.", operation)
            return None

        if not isinstance(payload, dict):
            LOGGER.warning("FACEIT returned an unexpected payload during %s.", operation)
            return None
        return payload

    def latest_match(self, player_id: str, game_id: str) -> LatestMatchResult:
        payload = self._get_json(
            f"/players/{quote(player_id, safe='-')}/history",
            params={"game": game_id, "limit": 1},
            operation="player history lookup",
        )
        if payload is None:
            return LatestMatchResult(ok=False, match_id=None)

        items = payload.get("items")
        if not isinstance(items, list):
            LOGGER.warning("FACEIT player history has an unexpected format.")
            return LatestMatchResult(ok=False, match_id=None)
        if not items:
            return LatestMatchResult(ok=True, match_id=None)

        first_item = items[0]
        if not isinstance(first_item, dict) or not first_item.get("match_id"):
            LOGGER.warning("FACEIT player history does not contain a match ID.")
            return LatestMatchResult(ok=False, match_id=None)

        return LatestMatchResult(ok=True, match_id=str(first_item["match_id"]))

    def match_details(self, match_id: str) -> dict[str, Any] | None:
        return self._get_json(
            f"/matches/{quote(match_id, safe='-')}",
            params=None,
            operation="match details lookup",
        )

    def match_stats(self, match_id: str) -> dict[str, Any] | None:
        return self._get_json(
            f"/matches/{quote(match_id, safe='-')}/stats",
            params=None,
            operation="match statistics lookup",
        )


class TelegramClient:
    def __init__(
        self,
        session: requests.Session,
        bot_token: str,
        chat_id: str,
        request_timeout: float,
    ) -> None:
        self._session = session
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._request_timeout = request_timeout

    def send_message(self, text: str) -> bool:
        # The token is part of the Telegram Bot API URL. Never log this URL or
        # the raw exception message, because either can disclose the token.
        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }

        try:
            response = self._session.post(
                url, json=payload, timeout=self._request_timeout
            )
        except requests.RequestException as exc:
            LOGGER.error("Telegram request failed (%s).", type(exc).__name__)
            return False

        if response.status_code != 200:
            LOGGER.error("Telegram returned HTTP %s.", response.status_code)
            return False

        try:
            result = response.json()
        except requests.JSONDecodeError:
            LOGGER.error("Telegram returned invalid JSON.")
            return False

        if not isinstance(result, dict) or not result.get("ok"):
            LOGGER.error("Telegram rejected the message.")
            return False
        return True


def load_state(state_file: Path) -> tuple[dict[str, str], bool]:
    if not state_file.exists():
        return {}, True

    try:
        with state_file.open("r", encoding="utf-8") as file_handle:
            payload = json.load(file_handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(
            f"Cannot safely read state file {state_file}. Fix or restore it first."
        ) from exc

    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise StateError(f"State file {state_file} has an unexpected format.")

    return dict(payload), False


def save_state(state_file: Path, state: dict[str, str]) -> None:
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=state_file.parent,
            prefix=f".{state_file.name}.",
            suffix=".tmp",
            text=True,
        )
    except OSError as exc:
        raise StateError(f"Cannot prepare state file {state_file}.") from exc

    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            json.dump(
                state,
                file_handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, state_file)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StateError(f"Cannot write state file {state_file}.") from exc


def format_time(unix_timestamp: Any, app_timezone: ZoneInfo) -> str:
    try:
        timestamp = float(unix_timestamp)
        if timestamp <= 0:
            return "Неизвестно"
        value = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(
            app_timezone
        )
    except (OSError, OverflowError, TypeError, ValueError):
        return "Неизвестно"
    return value.strftime("%H:%M")


def format_duration(seconds: Any) -> str:
    try:
        total_seconds = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "Неизвестно"
    minutes, remaining_seconds = divmod(total_seconds, 60)
    return f"{minutes}м {remaining_seconds}с"


def escaped(value: Any) -> str:
    return html.escape(str(value), quote=True)


def build_message(
    match_id: str,
    match_details: dict[str, Any],
    stats_data: dict[str, Any],
    players: dict[str, str],
    game_id: str,
    app_timezone: ZoneInfo,
) -> str | None:
    rounds = stats_data.get("rounds")
    if not isinstance(rounds, list) or not rounds or not isinstance(rounds[0], dict):
        LOGGER.warning("FACEIT match statistics do not contain round data.")
        return None

    round_info = rounds[0]
    round_stats = round_info.get("round_stats")
    if not isinstance(round_stats, dict):
        round_stats = {}

    map_name = str(round_stats.get("Map", "Unknown Map"))
    if map_name.startswith("de_"):
        map_name = map_name[3:]
    map_name = map_name.capitalize()

    raw_score = str(round_stats.get("Score", "0 / 0"))
    match_score = re.sub(r"\s*/\s*", ":", raw_score)

    started_at = match_details.get("started_at")
    finished_at = match_details.get("finished_at")
    start_text = format_time(started_at, app_timezone)
    end_text = format_time(finished_at, app_timezone)

    duration_text = "Неизвестно"
    try:
        if float(started_at) > 0 and float(finished_at) >= float(started_at):
            duration_text = format_duration(float(finished_at) - float(started_at))
    except (TypeError, ValueError):
        pass

    teams = round_info.get("teams")
    if not isinstance(teams, list):
        LOGGER.warning("FACEIT match statistics do not contain team data.")
        return None

    player_blocks: list[str] = []
    for team in teams:
        if not isinstance(team, dict):
            continue
        team_stats = team.get("team_stats")
        if not isinstance(team_stats, dict):
            team_stats = {}
        is_win = str(team_stats.get("Team Win", "0")) == "1"
        result_text = "🟢 ВЫИГРАЛ 🎉" if is_win else "🔴 ПРОИГРАЛ 😡"

        team_players = team.get("players")
        if not isinstance(team_players, list):
            continue
        for player in team_players:
            if not isinstance(player, dict):
                continue
            player_id = str(player.get("player_id", ""))
            if player_id not in players:
                continue

            player_stats = player.get("player_stats")
            if not isinstance(player_stats, dict):
                player_stats = {}
            nickname = escaped(players[player_id])

            player_blocks.append(
                "\n"
                f"👤 <b>{nickname}</b> — {result_text}\n"
                f"• Kills: <code>{escaped(player_stats.get('Kills', '0'))}</code> | "
                f"Deaths: <code>{escaped(player_stats.get('Deaths', '0'))}</code> | "
                f"K/D: <code>{escaped(player_stats.get('K/D Ratio', '0.0'))}</code>\n"
                f"• ADR: <code>{escaped(player_stats.get('ADR', '0'))}</code> | "
                f"MVP: <code>{escaped(player_stats.get('MVPs', '0'))}</code>\n"
            )

    if not player_blocks:
        LOGGER.warning("No configured players were found in the match statistics.")
        return None

    room_url = (
        f"https://www.faceit.com/ru/{quote(game_id, safe='-_')}/room/"
        f"{quote(match_id, safe='-')}"
    )
    return (
        "🎮 <b>Матч на FACEIT завершён!</b>\n"
        f"🗺 Карта: <b>{escaped(map_name)}</b>\n"
        f"📊 Счёт: <code>{escaped(match_score)}</code>\n"
        f"⏱ Время матча: <code>{escaped(start_text)}–{escaped(end_text)}</code> "
        f"(длительность: <code>{escaped(duration_text)}</code>)\n"
        f"{''.join(player_blocks)}\n"
        f"🔗 <a href=\"{escaped(room_url)}\">Открыть комнату матча</a>"
    )


def run_once(
    config: Config, faceit: FaceitClient, telegram: TelegramClient
) -> int:
    state, is_new_state_file = load_state(config.state_file)
    original_state = dict(state)
    latest_by_player: dict[str, str | None] = {}
    had_error = False

    for player_id, nickname in config.players.items():
        result = faceit.latest_match(player_id, config.game_id)
        if not result.ok:
            LOGGER.warning("Could not read the latest match for %s.", nickname)
            had_error = True
            continue
        latest_by_player[player_id] = result.match_id

    baseline_count = 0
    if not config.notify_on_first_run:
        for player_id, match_id in latest_by_player.items():
            if player_id not in state:
                # An empty value records that the player had no matches when
                # added. Their first future match will still trigger a message.
                state[player_id] = match_id or ""
                baseline_count += 1
        if baseline_count:
            LOGGER.info(
                "Created a baseline for %s new player(s); no old match was sent.",
                baseline_count,
            )

    pending_match_ids: list[str] = []
    seen_match_ids: set[str] = set()
    for player_id in config.players:
        if player_id not in latest_by_player:
            continue
        match_id = latest_by_player[player_id]
        if not match_id or state.get(player_id) == match_id:
            continue
        if match_id not in seen_match_ids:
            pending_match_ids.append(match_id)
            seen_match_ids.add(match_id)

    if pending_match_ids:
        LOGGER.info("Found %s pending match(es).", len(pending_match_ids))
    else:
        LOGGER.info("No new matches found.")

    for match_id in pending_match_ids:
        match_details = faceit.match_details(match_id)
        stats_data = faceit.match_stats(match_id)
        if match_details is None or stats_data is None:
            had_error = True
            continue

        message = build_message(
            match_id,
            match_details,
            stats_data,
            config.players,
            config.game_id,
            config.timezone,
        )
        if message is None:
            had_error = True
            continue

        if not telegram.send_message(message):
            had_error = True
            continue

        for player_id, latest_match_id in latest_by_player.items():
            if latest_match_id == match_id:
                state[player_id] = match_id
        LOGGER.info("Sent a notification for match %s.", match_id)

    if is_new_state_file or state != original_state:
        save_state(config.state_file, state)

    return 1 if had_error else 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send Telegram notifications for completed FACEIT matches."
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate environment variables without making network requests",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="send one test message and exit",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    configure_logging()

    try:
        config = Config.from_env()
    except ConfigurationError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return 2

    logging.getLogger().setLevel(getattr(logging, config.log_level))

    if args.check_config:
        LOGGER.info(
            "Configuration is valid: %s player(s), game=%s, timezone=%s, state=%s.",
            len(config.players),
            config.game_id,
            config.timezone_name,
            config.state_file,
        )
        return 0

    session = build_http_session()
    try:
        telegram = TelegramClient(
            session,
            config.telegram_bot_token,
            config.telegram_chat_id,
            config.request_timeout,
        )

        if args.test_telegram:
            if telegram.send_message("✅ FACEIT Match Bot: тестовое сообщение."):
                LOGGER.info("Telegram test message sent successfully.")
                return 0
            return 1

        faceit = FaceitClient(
            session, config.faceit_api_key, config.request_timeout
        )
        return run_once(config, faceit, telegram)
    except StateError as exc:
        LOGGER.error("State error: %s", exc)
        return 2
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
