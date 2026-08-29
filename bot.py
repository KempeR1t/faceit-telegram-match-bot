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
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


VERSION = "1.3.1"
BASE_DIR = Path(__file__).resolve().parent
FACEIT_API_BASE = "https://open.faceit.com/data/v4"
FACEIT_WEB_BASE = "https://www.faceit.com"
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
    telegram_proxy_url: str | None
    players: dict[str, str]
    game_id: str
    timezone: ZoneInfo
    timezone_name: str
    state_file: Path
    request_timeout: float
    flaresolverr_enabled: bool
    flaresolverr_url: str
    flaresolverr_max_timeout_ms: int
    notify_on_first_run: bool
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        faceit_api_key = required_env("FACEIT_API_KEY")
        telegram_bot_token = required_env("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = required_env("TELEGRAM_CHAT_ID")
        telegram_proxy_url = normalize_telegram_proxy_url(
            os.getenv("TELEGRAM_PROXY_URL", "")
        )

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
        flaresolverr_enabled = parse_bool_env(
            "FLARESOLVERR_ENABLED", default=True
        )
        flaresolverr_url = normalize_flaresolverr_url(
            os.getenv("FLARESOLVERR_URL", "http://127.0.0.1:8191/v1")
        )
        flaresolverr_max_timeout_ms = parse_int_env(
            "FLARESOLVERR_MAX_TIMEOUT_MS",
            default=120000,
            minimum=1000,
            maximum=300000,
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
            telegram_proxy_url=telegram_proxy_url,
            players=players,
            game_id=game_id,
            timezone=app_timezone,
            timezone_name=timezone_name,
            state_file=state_file,
            request_timeout=request_timeout,
            flaresolverr_enabled=flaresolverr_enabled,
            flaresolverr_url=flaresolverr_url,
            flaresolverr_max_timeout_ms=flaresolverr_max_timeout_ms,
            notify_on_first_run=notify_on_first_run,
            log_level=log_level,
        )


@dataclass(frozen=True)
class LatestMatchResult:
    ok: bool
    match_id: str | None


@dataclass(frozen=True)
class FaceitRating:
    rating: float
    swing: float


class RatingProvider(Protocol):
    def match_ratings(
        self, match_id: str, game_id: str
    ) -> dict[str, FaceitRating]: ...


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


def parse_int_env(
    name: str, *, default: int, minimum: int, maximum: int
) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return value


def normalize_flaresolverr_url(raw_value: str) -> str:
    endpoint = raw_value.strip().rstrip("/")
    if not endpoint:
        raise ConfigurationError("FLARESOLVERR_URL must not be empty.")
    if not endpoint.endswith("/v1"):
        endpoint += "/v1"

    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(
            "FLARESOLVERR_URL must be a valid http:// or https:// URL."
        )
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            "FLARESOLVERR_URL must not contain a query string or fragment."
        )
    return endpoint


def normalize_telegram_proxy_url(raw_value: str) -> str | None:
    proxy_url = raw_value.strip()
    if not proxy_url:
        return None

    parsed = urlsplit(proxy_url)
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"}:
        raise ConfigurationError(
            "TELEGRAM_PROXY_URL must use http, https, socks5 or socks5h."
        )
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(
            "TELEGRAM_PROXY_URL contains an invalid port."
        ) from exc
    if not hostname or port is None or port <= 0:
        raise ConfigurationError(
            "TELEGRAM_PROXY_URL must contain a hostname and port."
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "TELEGRAM_PROXY_URL must not contain a path, query or fragment."
        )
    return proxy_url


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


def extract_faceit_ratings(
    payload: dict[str, Any], game_id: str
) -> dict[str, FaceitRating]:
    """Extract per-player FACEIT 2.0 rating fields from scoreboard-summary."""
    payload_data = payload.get("payload")
    if not isinstance(payload_data, dict):
        return {}
    game_data = payload_data.get(game_id)
    if not isinstance(game_data, dict):
        return {}
    teams = game_data.get("teams")
    if not isinstance(teams, list):
        return {}

    ratings: dict[str, FaceitRating] = {}
    for team in teams:
        if not isinstance(team, dict):
            continue
        team_players = team.get("players")
        if not isinstance(team_players, list):
            continue
        for player in team_players:
            if not isinstance(player, dict):
                continue
            try:
                player_id = str(UUID(str(player.get("player_id", "")).strip()))
            except ValueError:
                continue

            stats = player.get("stats")
            if not isinstance(stats, dict):
                continue
            rating = finite_float(stats.get("faceit_rating"))
            swing = finite_float(stats.get("faceit_rating_swing"))
            if rating is None or swing is None:
                continue
            ratings[player_id] = FaceitRating(rating=rating, swing=swing)
    return ratings


def finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def decode_flaresolverr_json(body: Any) -> dict[str, Any] | None:
    if isinstance(body, dict):
        return body
    if not isinstance(body, str) or not body.strip():
        return None

    document = body.strip()
    pre_match = re.search(
        r"<pre(?:\s[^>]*)?>(.*?)</pre>",
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if pre_match:
        document = html.unescape(pre_match.group(1))

    try:
        payload = json.loads(document)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


class FlareSolverrClient:
    """Fetch optional FACEIT scoreboard data through a private FlareSolverr."""

    SESSION_PREFIX = "faceit-bot-"

    def __init__(
        self,
        session: requests.Session,
        endpoint: str,
        max_timeout_ms: int,
        cleanup_stale_sessions: bool = False,
    ) -> None:
        self._session = session
        self._endpoint = endpoint
        self._max_timeout_ms = max_timeout_ms
        self._api_timeout = max(60.0, max_timeout_ms / 1000.0 + 30.0)
        self._cleanup_stale_sessions = cleanup_stale_sessions
        self._session_inventory_checked = False
        self._session_id: str | None = None

    def _api_call(
        self,
        payload: dict[str, Any],
        *,
        operation: str,
        read_timeout: float | None = None,
    ) -> dict[str, Any] | None:
        LOGGER.info("FlareSolverr: %s started.", operation)
        started = time.monotonic()
        try:
            response = self._session.post(
                self._endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=(10, read_timeout or self._api_timeout),
            )
        except requests.RequestException as exc:
            LOGGER.warning(
                "FlareSolverr request failed during %s after %.1fs (%s).",
                operation,
                time.monotonic() - started,
                type(exc).__name__,
            )
            return None

        elapsed = time.monotonic() - started
        try:
            result = response.json()
        except ValueError:
            LOGGER.warning(
                "FlareSolverr returned invalid JSON during %s: HTTP %s in %.1fs.",
                operation,
                response.status_code,
                elapsed,
            )
            return None

        if not isinstance(result, dict):
            LOGGER.warning(
                "FlareSolverr returned an unexpected payload during %s.", operation
            )
            return None

        if response.status_code != 200 or result.get("status") != "ok":
            message = " ".join(str(result.get("message", "unknown error")).split())
            LOGGER.warning(
                "FlareSolverr failed during %s: HTTP %s in %.1fs, message=%s.",
                operation,
                response.status_code,
                elapsed,
                message[:300],
            )
            return None

        LOGGER.info("FlareSolverr: %s completed in %.1fs.", operation, elapsed)
        return result

    def _list_sessions(self) -> list[str] | None:
        result = self._api_call(
            {"cmd": "sessions.list"},
            operation="session inventory",
            read_timeout=45.0,
        )
        if result is None:
            return None

        sessions = result.get("sessions")
        if not isinstance(sessions, list):
            LOGGER.warning("FlareSolverr returned an invalid session inventory.")
            return None
        return [session_id for session_id in sessions if isinstance(session_id, str)]

    def _destroy_named_session(self, session_id: str, *, operation: str) -> bool:
        result = self._api_call(
            {"cmd": "sessions.destroy", "session": session_id},
            operation=operation,
            read_timeout=45.0,
        )
        return result is not None

    def _cleanup_owned_sessions(self) -> bool:
        sessions = self._list_sessions()
        if sessions is None:
            return False

        owned_sessions = [
            session_id
            for session_id in sessions
            if session_id.startswith(self.SESSION_PREFIX)
        ]
        all_removed = True
        for session_id in owned_sessions:
            if not self._destroy_named_session(
                session_id,
                operation="stale session cleanup",
            ):
                all_removed = False

        LOGGER.info(
            "FlareSolverr session inventory checked: total=%s, "
            "owned_stale=%s, prefix=%s.",
            len(sessions),
            len(owned_sessions),
            self.SESSION_PREFIX,
        )
        return all_removed

    def _ensure_session(self) -> bool:
        if self._session_id:
            return True

        if self._cleanup_stale_sessions and not self._session_inventory_checked:
            if not self._cleanup_owned_sessions():
                LOGGER.warning(
                    "A new FlareSolverr session will not be created because "
                    "stale FACEIT sessions could not be inventoried or removed."
                )
                return False
            self._session_inventory_checked = True

        requested_id = f"{self.SESSION_PREFIX}{os.getpid()}-{uuid4().hex[:8]}"
        result = self._api_call(
            {"cmd": "sessions.create", "session": requested_id},
            operation="session creation",
            read_timeout=45.0,
        )
        if result is None:
            # sessions.create may leave Chrome running even when its API call
            # fails, so the next attempt must inventory the prefix again.
            self._session_inventory_checked = False
            return False

        self._session_id = str(result.get("session") or requested_id)
        LOGGER.info("Created a private FlareSolverr session for FACEIT.")
        return True

    def _get_solution(
        self, url: str, *, operation: str
    ) -> dict[str, Any] | None:
        if not self._ensure_session():
            return None

        result = self._api_call(
            {
                "cmd": "request.get",
                "session": self._session_id,
                "session_ttl_minutes": 10,
                "url": url,
                "maxTimeout": self._max_timeout_ms,
            },
            operation=operation,
        )
        if result is None:
            return None

        solution = result.get("solution")
        if not isinstance(solution, dict):
            LOGGER.warning("FlareSolverr returned no solution during %s.", operation)
            return None
        if int(finite_float(solution.get("status")) or 0) != 200:
            LOGGER.warning(
                "FACEIT returned HTTP %s through FlareSolverr during %s.",
                solution.get("status"),
                operation,
            )
            return None
        return solution

    def match_ratings(
        self, match_id: str, game_id: str
    ) -> dict[str, FaceitRating]:
        scoreboard_url = (
            f"{FACEIT_WEB_BASE}/api/statistics/v1/"
            f"{quote(game_id, safe='-_')}/matches/"
            f"{quote(match_id, safe='-')}/match-rounds/1/scoreboard-summary"
        )
        solution: dict[str, Any] | None = None
        for attempt in range(1, 3):
            solution = self._get_solution(
                scoreboard_url,
                operation=f"FACEIT scoreboard lookup (attempt {attempt}/2)",
            )
            if solution is not None:
                break
            if attempt == 1:
                LOGGER.warning(
                    "FACEIT scoreboard lookup failed; retrying in 5 seconds "
                    "without replacing the FlareSolverr session."
                )
                time.sleep(5.0)

        if solution is None:
            self._destroy_session()
            return {}

        payload = decode_flaresolverr_json(solution.get("response"))
        if payload is None:
            LOGGER.warning("FACEIT scoreboard response is not valid JSON.")
            return {}

        ratings = extract_faceit_ratings(payload, game_id)
        if ratings:
            LOGGER.info("Loaded FACEIT Rating and Swing for %s player(s).", len(ratings))
        else:
            LOGGER.warning("FACEIT scoreboard response contains no Rating data.")
        return ratings

    def _destroy_session(self) -> None:
        session_id = self._session_id
        self._session_id = None
        if not session_id:
            return

        if not self._destroy_named_session(
            session_id,
            operation="session cleanup",
        ):
            self._session_inventory_checked = False

    def close(self) -> None:
        self._destroy_session()
        if self._cleanup_stale_sessions:
            self._cleanup_owned_sessions()


class TelegramClient:
    def __init__(
        self,
        session: requests.Session,
        bot_token: str,
        chat_id: str,
        request_timeout: float,
        proxy_url: str | None = None,
    ) -> None:
        self._session = session
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._request_timeout = request_timeout
        self._proxy_url = proxy_url

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

        request_options: dict[str, Any] = {
            "json": payload,
            "timeout": self._request_timeout,
        }
        if self._proxy_url is not None:
            request_options["proxies"] = {"https": self._proxy_url}

        try:
            response = self._session.post(url, **request_options)
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


def format_faceit_rating(rating: FaceitRating | None) -> str:
    if rating is None:
        return ""
    swing_percent = rating.swing * 100
    if round(swing_percent, 2) == 0:
        swing_percent = 0.0
    if swing_percent < 0:
        swing_marker = "🔻 "
    elif swing_percent > 0:
        swing_marker = "🟩▲ "
    else:
        swing_marker = ""
    return (
        f"• Rating: <code>{rating.rating:.2f}</code> | "
        f"{swing_marker}Swing: <code>{swing_percent:+.2f}%</code>\n"
    )


def build_message(
    match_id: str,
    match_details: dict[str, Any],
    stats_data: dict[str, Any],
    players: dict[str, str],
    game_id: str,
    app_timezone: ZoneInfo,
    faceit_ratings: dict[str, FaceitRating] | None = None,
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
    tracked_player_wins: dict[str, bool] = {}
    for team in teams:
        if not isinstance(team, dict):
            continue
        team_stats = team.get("team_stats")
        if not isinstance(team_stats, dict):
            team_stats = {}
        is_win = str(team_stats.get("Team Win", "0")) == "1"

        team_players = team.get("players")
        if not isinstance(team_players, list):
            continue
        for player in team_players:
            if not isinstance(player, dict):
                continue
            player_id = str(player.get("player_id", ""))
            if player_id not in players:
                continue
            tracked_player_wins[player_id] = is_win

            player_stats = player.get("player_stats")
            if not isinstance(player_stats, dict):
                player_stats = {}
            nickname = escaped(players[player_id])
            rating_line = format_faceit_rating(
                (faceit_ratings or {}).get(player_id)
            )

            player_blocks.append(
                "\n"
                f"👤 <b>{nickname}</b>\n"
                f"{rating_line}"
                f"• Kills: <code>{escaped(player_stats.get('Kills', '0'))}</code> | "
                f"Deaths: <code>{escaped(player_stats.get('Deaths', '0'))}</code> | "
                f"K/D: <code>{escaped(player_stats.get('K/D Ratio', '0.0'))}</code>\n"
                f"• ADR: <code>{escaped(player_stats.get('ADR', '0'))}</code> | "
                f"MVP: <code>{escaped(player_stats.get('MVPs', '0'))}</code>\n"
            )

    if not player_blocks:
        LOGGER.warning("No configured players were found in the match statistics.")
        return None

    match_is_win = next(
        (
            tracked_player_wins[player_id]
            for player_id in players
            if player_id in tracked_player_wins
        ),
        None,
    )
    if match_is_win is None:
        LOGGER.warning("Could not determine the result for a configured player.")
        return None
    match_result = (
        "🟢 <b>ПОБЕДА</b> 🎉" if match_is_win else "🔴 <b>ПОРАЖЕНИЕ</b> 😡"
    )

    room_url = (
        f"https://www.faceit.com/ru/{quote(game_id, safe='-_')}/room/"
        f"{quote(match_id, safe='-')}/scoreboard"
    )
    return (
        "🎮 <b>Матч на FACEIT завершён!</b>\n"
        f"🗺 Карта: <b>{escaped(map_name)}</b>\n"
        f"📊 Счёт: <code>{escaped(match_score)}</code>\n"
        f"⏱ Время матча: <code>{escaped(start_text)}–{escaped(end_text)}</code> "
        f"(длительность: <code>{escaped(duration_text)}</code>)\n"
        f"🏁 Результат: {match_result}\n"
        f"{''.join(player_blocks)}\n"
        f"🔗 <a href=\"{escaped(room_url)}\">Открыть комнату матча</a>"
    )


def run_once(
    config: Config,
    faceit: FaceitClient,
    telegram: TelegramClient,
    flaresolverr: RatingProvider | None = None,
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

        faceit_ratings: dict[str, FaceitRating] = {}
        if flaresolverr is not None:
            try:
                faceit_ratings = flaresolverr.match_ratings(
                    match_id, config.game_id
                )
            except Exception as exc:
                LOGGER.warning(
                    "Optional FACEIT Rating lookup failed unexpectedly (%s); "
                    "the notification will be sent without Rating and Swing.",
                    type(exc).__name__,
                )

        message = build_message(
            match_id,
            match_details,
            stats_data,
            config.players,
            config.game_id,
            config.timezone,
            faceit_ratings,
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
            "Configuration is valid: %s player(s), game=%s, timezone=%s, "
            "state=%s, flaresolverr=%s, telegram_proxy=%s.",
            len(config.players),
            config.game_id,
            config.timezone_name,
            config.state_file,
            "enabled" if config.flaresolverr_enabled else "disabled",
            "enabled" if config.telegram_proxy_url else "disabled",
        )
        return 0

    session = build_http_session()
    flaresolverr: FlareSolverrClient | None = None
    try:
        telegram = TelegramClient(
            session,
            config.telegram_bot_token,
            config.telegram_chat_id,
            config.request_timeout,
            config.telegram_proxy_url,
        )

        if args.test_telegram:
            if telegram.send_message("✅ FACEIT Match Bot: тестовое сообщение."):
                LOGGER.info("Telegram test message sent successfully.")
                return 0
            return 1

        faceit = FaceitClient(
            session, config.faceit_api_key, config.request_timeout
        )
        if config.flaresolverr_enabled:
            flaresolverr = FlareSolverrClient(
                session,
                config.flaresolverr_url,
                config.flaresolverr_max_timeout_ms,
                cleanup_stale_sessions=True,
            )
        return run_once(config, faceit, telegram, flaresolverr)
    except StateError as exc:
        LOGGER.error("State error: %s", exc)
        return 2
    finally:
        if flaresolverr is not None:
            try:
                flaresolverr.close()
            except Exception as exc:
                LOGGER.warning(
                    "Could not clean up the private FlareSolverr session (%s).",
                    type(exc).__name__,
                )
        session.close()


if __name__ == "__main__":
    sys.exit(main())
