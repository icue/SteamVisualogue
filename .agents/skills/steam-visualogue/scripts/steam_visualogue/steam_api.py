"""Steam Web/Store adapters and the cache-aware profile collector."""

from __future__ import annotations

import json
import hashlib
import inspect
import random
import re
import sqlite3
import socket
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .cache_db import CacheDB
from .api_coordination import (
    APICoordination,
    CoordinationError,
    CoordinationQuotaExceeded,
    DEFAULT_DAILY_REQUEST_CEILING,
)
from .credentials import api_coordination_path
from .fingerprint import compute_evidence_fingerprint
from .rate_limit import RateLimiter, jittered_backoff


WEB_API_ROOT = "https://api.steampowered.com"
STORE_API_ROOT = "https://store.steampowered.com/api/appdetails"
STORE_TTL = 30 * 24 * 60 * 60
STORE_NEGATIVE_TTL = 7 * 24 * 60 * 60
SCHEMA_TTL = 90 * 24 * 60 * 60
GLOBAL_RARITY_TTL = 7 * 24 * 60 * 60
PLAYER_ACHIEVEMENT_TTL = 7 * 24 * 60 * 60
ProgressCallback = Callable[[str, int | None, int | None], None]


def _report_progress(
    progress: ProgressCallback | None,
    message: str,
    current: int | None = None,
    total: int | None = None,
) -> None:
    if progress is not None:
        progress(message, current, total)


class SteamDataError(RuntimeError):
    """Base error whose messages never include request URLs or credentials."""


class SteamRequestError(SteamDataError):
    pass


class SteamAuthenticationError(SteamRequestError):
    pass


class SteamRateLimitError(SteamRequestError):
    pass


class SteamResponseError(SteamDataError):
    pass


class InvalidSteamIdentity(SteamDataError):
    pass


class OwnedGamesUnavailable(SteamResponseError):
    pass


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: Mapping[str, str]
    body: bytes | str


@dataclass(frozen=True)
class ResolvedIdentity:
    steamid: str
    player_alias: str


@dataclass(frozen=True)
class NormalizedIdentity:
    key: str
    steamid: str | None = None
    vanity: str | None = None


def normalize_identity(identity: str) -> NormalizedIdentity:
    """Validate and canonicalize a supported Steam identity without network access."""

    candidate = str(identity).strip()
    if re.fullmatch(r"\d{16,20}", candidate):
        return NormalizedIdentity(f"steamid:{candidate}", steamid=candidate)
    parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
    try:
        port = parsed.port
    except ValueError:
        raise InvalidSteamIdentity("Expected a SteamID64 or steamcommunity.com profile URL") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or (parsed.hostname or "").lower() not in {"steamcommunity.com", "www.steamcommunity.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidSteamIdentity("Expected a SteamID64 or steamcommunity.com profile URL")
    parts = [unquote(part).strip() for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise InvalidSteamIdentity("Steam profile URL is incomplete")
    kind, value = parts[0].casefold(), parts[1]
    if kind == "profiles" and re.fullmatch(r"\d{16,20}", value):
        return NormalizedIdentity(
            f"steamcommunity.com/profiles/{value}", steamid=value
        )
    if kind != "id" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise InvalidSteamIdentity("Steam profile URL must use /profiles/ or /id/")
    vanity = value.casefold()
    return NormalizedIdentity(f"steamcommunity.com/id/{vanity}", vanity=vanity)


def normalize_identity_key(identity: str) -> str:
    """Return the canonical, non-secret identity key used for local hashing."""

    return normalize_identity(identity).key


def identity_key_hash(identity: str) -> str:
    return hashlib.sha256(normalize_identity_key(identity).encode("utf-8")).hexdigest()


class Transport(Protocol):
    def __call__(self, url: str, timeout: float) -> HTTPResult: ...


def _default_transport(url: str, timeout: float) -> HTTPResult:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "SteamVisualogue/1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return HTTPResult(int(response.status), dict(response.headers.items()), response.read())
    except HTTPError as error:
        return HTTPResult(int(error.code), dict(error.headers.items()) if error.headers else {}, error.read())


class SteamAPI:
    """Credential-safe, paced JSON client for the endpoints used by the skill."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport | Any | None = None,
        rate_limiter: RateLimiter | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_retries: int = 5,
        timeout: float = 30.0,
        daily_request_ceiling: int = DEFAULT_DAILY_REQUEST_CEILING,
        randomizer: Callable[[], float] = random.random,
        coordination_path: str | Path | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("A Steam Web API key is required")
        self._api_key = api_key.strip()
        self._transport = transport or _default_transport
        self._sleeper = sleeper
        self._clock = clock
        self._rate_limiter = rate_limiter or RateLimiter(sleeper=sleeper, clock=clock)
        self.max_retries = max(0, int(max_retries))
        self.timeout = float(timeout)
        self.daily_request_ceiling = min(DEFAULT_DAILY_REQUEST_CEILING, max(0, int(daily_request_ceiling)))
        self._randomizer = randomizer
        self._api_key_fingerprint = hashlib.sha256(self._api_key.encode("utf-8")).hexdigest()
        self._coordination_path = str(coordination_path) if coordination_path is not None else str(api_coordination_path())
        self._coordination: APICoordination | None = None

    def _coordinator(self) -> APICoordination:
        if self._coordination is None:
            try:
                self._coordination = APICoordination(
                    self._coordination_path,
                    clock=self._clock,
                )
            except (CoordinationError, OSError, sqlite3.Error) as exc:
                raise SteamRequestError("Steam request coordination is unavailable") from None
        return self._coordination

    def close(self) -> None:
        coordination = self._coordination
        self._coordination = None
        if coordination is not None:
            try:
                coordination.close()
            except CoordinationError:
                raise SteamRequestError("Steam request coordination is unavailable") from None

    def __enter__(self) -> "SteamAPI":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        coordination = getattr(self, "_coordination", None)
        if coordination is not None:
            try:
                coordination.close()
            except Exception:
                pass

    def _call_transport(self, url: str) -> HTTPResult:
        target = self._transport.request if hasattr(self._transport, "request") else self._transport
        result = target(url, self.timeout)
        if isinstance(result, HTTPResult):
            return result
        if isinstance(result, tuple) and len(result) == 3:
            return HTTPResult(int(result[0]), result[1] or {}, result[2])
        if isinstance(result, Mapping):
            if "status" in result:
                return HTTPResult(
                    int(result["status"]), result.get("headers") or {}, result.get("body", b"")
                )
            return HTTPResult(200, {}, json.dumps(result).encode("utf-8"))
        raw_status = getattr(result, "status", None)
        if raw_status is None:
            raw_status = result.getcode()
        status = int(raw_status)
        headers = getattr(result, "headers", {}) or {}
        body = result.read()
        return HTTPResult(status, headers, body)

    def _retry_after(self, headers: Mapping[str, Any]) -> float | None:
        raw = next((value for key, value in headers.items() if str(key).lower() == "retry-after"), None)
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            try:
                retry_time = parsedate_to_datetime(str(raw))
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                return max(0.0, retry_time.timestamp() - self._clock())
            except (TypeError, ValueError, OverflowError):
                return None

    def _backoff(self, attempt: int, headers: Mapping[str, Any] | None = None) -> None:
        self._sleeper(self._backoff_delay(attempt, headers))

    def _backoff_delay(self, attempt: int, headers: Mapping[str, Any] | None = None) -> float:
        retry_after = self._retry_after(headers or {})
        return retry_after if retry_after is not None else jittered_backoff(
            attempt, randomizer=self._randomizer
        )

    def _reserve_request(self, url: str, *, official: bool) -> None:
        coordinator = self._coordinator()
        try:
            reservation = coordinator.reserve_request(
                url,
                api_key_sha256=self._api_key_fingerprint if official else None,
                daily_ceiling=self.daily_request_ceiling,
                now=self._clock(),
            )
        except CoordinationQuotaExceeded:
            raise SteamRequestError("Steam Web API daily safety ceiling reached") from None
        except CoordinationError:
            raise SteamRequestError("Steam request coordination is unavailable") from None
        if reservation.delay > 0:
            self._sleeper(reservation.delay)

    def _persist_cooldown(self, url: str, *, official: bool, delay: float) -> None:
        coordinator = self._coordinator()
        scope = self._api_key_fingerprint if official else "store"
        try:
            coordinator.record_cooldown(
                url,
                scope=scope,
                blocked_until=self._clock() + max(0.0, float(delay)),
            )
        except CoordinationError:
            raise SteamRequestError("Steam request coordination is unavailable") from None

    def _request_json(
        self,
        base_url: str,
        params: Mapping[str, Any],
        *,
        resource: str,
        official: bool,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        query = urlencode([(key, value) for key, value in params.items() if value is not None])
        url = f"{base_url}?{query}"
        for attempt in range(self.max_retries + 1):
            self._reserve_request(url, official=official)
            self._rate_limiter.wait(url)
            try:
                response = self._call_transport(url)
            except (OSError, TimeoutError, ConnectionError, socket.timeout, URLError):
                if attempt >= self.max_retries:
                    raise SteamRequestError(f"{resource} is temporarily unavailable") from None
                self._backoff(attempt)
                continue
            except Exception:
                # Third-party transports may use their own network exception type.
                if attempt >= self.max_retries:
                    raise SteamRequestError(f"{resource} transport failed") from None
                self._backoff(attempt)
                continue

            if response.status == 429:
                delay = self._backoff_delay(attempt, response.headers)
                self._persist_cooldown(url, official=official, delay=delay)
                self._rate_limiter.defer(url, delay)
                if attempt >= self.max_retries:
                    raise SteamRateLimitError(
                        f"{resource} rate limit persisted after bounded retries; retry later"
                    )
                self._sleeper(delay)
                continue
            if 500 <= response.status <= 599:
                if attempt >= self.max_retries:
                    raise SteamRequestError(f"{resource} is temporarily unavailable")
                self._backoff(attempt, response.headers)
                continue
            if response.status in {401, 403}:
                if authenticated:
                    raise SteamAuthenticationError(
                        f"{resource} authentication or authorization failed; "
                        "verify the local Steam Web API key"
                    )
                raise SteamRequestError(f"{resource} returned HTTP {response.status}")
            if not 200 <= response.status <= 299:
                raise SteamRequestError(f"{resource} returned HTTP {response.status}")
            try:
                raw = response.body.decode("utf-8") if isinstance(response.body, bytes) else response.body
                payload = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError, TypeError):
                raise SteamResponseError(f"{resource} returned invalid JSON") from None
            if not isinstance(payload, dict):
                raise SteamResponseError(f"{resource} returned an invalid response shape")
            return payload
        raise SteamRequestError(f"{resource} is temporarily unavailable")

    def _web_api(
        self,
        interface: str,
        method: str,
        version: str,
        params: Mapping[str, Any],
        *,
        include_key: bool = True,
    ) -> dict[str, Any]:
        request_params = dict(params)
        if include_key:
            request_params["key"] = self._api_key
        return self._request_json(
            f"{WEB_API_ROOT}/{interface}/{method}/{version}/",
            request_params,
            resource=method,
            official=True,
            authenticated=include_key,
        )

    def resolve_identity(self, identity: str) -> ResolvedIdentity:
        normalized = normalize_identity(identity)
        if normalized.steamid is not None:
            return ResolvedIdentity(normalized.steamid, "Steam Player")
        value = str(normalized.vanity)
        payload = self._web_api(
            "ISteamUser", "ResolveVanityURL", "v1", {"vanityurl": value}
        )
        response = payload.get("response") or {}
        steamid = response.get("steamid")
        if int(response.get("success") or 0) != 1 or not steamid:
            raise InvalidSteamIdentity("Steam vanity profile could not be resolved")
        return ResolvedIdentity(str(steamid), value)

    def get_owned_games(self, steamid: str) -> list[dict[str, Any]]:
        payload = self._web_api(
            "IPlayerService", "GetOwnedGames", "v1",
            {
                "steamid": steamid, "include_appinfo": "true",
                "include_played_free_games": "true", "format": "json",
            },
        )
        response = payload.get("response")
        if not isinstance(response, Mapping):
            raise SteamResponseError("GetOwnedGames did not return a library")
        games = response.get("games")
        if games is None and "game_count" in response and int(response.get("game_count") or 0) == 0:
            return []
        if not isinstance(games, list):
            raise OwnedGamesUnavailable(
                "GetOwnedGames returned no library; game details may be hidden or the API key "
                "may not match the requested Steam account"
            )
        return [dict(game) for game in games if isinstance(game, Mapping) and game.get("appid") is not None]

    def get_recently_played(self, steamid: str) -> list[dict[str, Any]]:
        payload = self._web_api(
            "IPlayerService", "GetRecentlyPlayedGames", "v1",
            {"steamid": steamid, "count": 0, "format": "json"},
        )
        response = payload.get("response") or {}
        games = response.get("games") or []
        if not isinstance(games, list):
            raise SteamResponseError("GetRecentlyPlayedGames returned an invalid response shape")
        return [dict(game) for game in games if isinstance(game, Mapping)]

    def get_app_details(self, appid: int, language: str = "english") -> dict[str, Any] | None:
        payload = self._request_json(
            STORE_API_ROOT, {"appids": int(appid), "l": str(language)},
            resource="Store appdetails", official=False,
        )
        envelope = payload.get(str(int(appid)))
        if not isinstance(envelope, Mapping) or not envelope.get("success"):
            return None
        data = envelope.get("data")
        return dict(data) if isinstance(data, Mapping) else None

    def get_player_achievements(
        self, steamid: str, appid: int, language: str = "english"
    ) -> list[dict[str, Any]]:
        payload = self._web_api(
            "ISteamUserStats", "GetPlayerAchievements", "v1",
            {"steamid": steamid, "appid": int(appid), "l": str(language)},
        )
        stats = payload.get("playerstats") or {}
        if stats.get("success") is False:
            return []
        achievements = stats.get("achievements") or []
        if not isinstance(achievements, list):
            raise SteamResponseError("GetPlayerAchievements returned an invalid response shape")
        return [dict(item) for item in achievements if isinstance(item, Mapping)]

    def get_achievement_schema(self, appid: int, language: str = "english") -> list[dict[str, Any]]:
        payload = self._web_api(
            "ISteamUserStats", "GetSchemaForGame", "v2",
            {"appid": int(appid), "l": str(language)},
        )
        game = payload.get("game") or {}
        available = game.get("availableGameStats") or {}
        achievements = available.get("achievements") or []
        if not isinstance(achievements, list):
            raise SteamResponseError("GetSchemaForGame returned an invalid response shape")
        return [dict(item) for item in achievements if isinstance(item, Mapping)]

    def get_global_achievement_percentages(self, appid: int) -> list[dict[str, Any]]:
        payload = self._web_api(
            "ISteamUserStats", "GetGlobalAchievementPercentagesForApp", "v2",
            {"gameid": int(appid), "format": "json"}, include_key=False,
        )
        percentages = payload.get("achievementpercentages") or {}
        achievements = percentages.get("achievements") or []
        if not isinstance(achievements, list):
            raise SteamResponseError(
                "GetGlobalAchievementPercentagesForApp returned an invalid response shape"
            )
        return [dict(item) for item in achievements if isinstance(item, Mapping)]


class SteamDataCollector:
    """Collect normalized profiles with a 24-hour user snapshot before source caches."""

    def __init__(
        self,
        api_key: str | None = None,
        cache: CacheDB | None = None,
        *,
        api: Any | None = None,
        api_factory: Callable[[], Any] | None = None,
        transport: Transport | Any | None = None,
        rate_limiter: RateLimiter | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if cache is None:
            raise ValueError("A CacheDB instance is required")
        if api is not None and api_factory is not None:
            raise ValueError("Supply either an API adapter or an API factory, not both")
        if api is None and api_factory is None and not api_key:
            raise ValueError("An API key or API factory is required")
        self.cache = cache
        self.clock = clock
        self.api = api
        self._api_factory = api_factory
        if self.api is None and self._api_factory is None:
            self._api_factory = lambda: SteamAPI(
                str(api_key), transport=transport, rate_limiter=rate_limiter,
                sleeper=sleeper, clock=clock,
            )

    def _get_api(self) -> Any:
        if self.api is None:
            if self._api_factory is None:
                raise SteamDataError("Steam network adapter is unavailable")
            self.api = self._api_factory()
            if self.api is None:
                raise SteamDataError("Steam network adapter factory returned no adapter")
        return self.api

    def _core_api_call(self, method_name: str, *args: Any) -> Any:
        """Call canonical enrichment endpoints with an explicit English locale.

        Small test adapters written for the pre-locale API may not expose the
        keyword; they remain usable without changing the real request contract.
        """

        method = getattr(self._get_api(), method_name)
        try:
            parameters = inspect.signature(method).parameters.values()
            accepts_language = any(
                parameter.name == "language" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_language = True
        if accepts_language:
            return method(*args, language="english")
        return method(*args)

    @staticmethod
    def _iso_time(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _normalise_metadata(raw: Mapping[str, Any], fallback_name: str) -> dict[str, Any]:
        release = raw.get("release_date") or {}
        achievements = raw.get("achievements") or {}
        return {
            "name": str(raw.get("name") or fallback_name),
            "type": raw.get("type"),
            "genres": [item.get("description") for item in raw.get("genres") or [] if isinstance(item, Mapping) and item.get("description")],
            "release_date": release.get("date") if isinstance(release, Mapping) else release,
            "developers": list(raw.get("developers") or []),
            "publishers": list(raw.get("publishers") or []),
            "platforms": dict(raw.get("platforms") or {}),
            "categories": [item.get("description") for item in raw.get("categories") or [] if isinstance(item, Mapping) and item.get("description")],
            "achievement_total": achievements.get("total") if isinstance(achievements, Mapping) else None,
            "header_image_url": raw.get("header_image"),
        }

    @staticmethod
    def _public_metadata(row: Mapping[str, Any] | None) -> dict[str, Any]:
        row = row or {}
        return {
            "type": row.get("type"),
            "genres": list(row.get("genres") or []),
            "release_date": row.get("release_date"),
            "developers": list(row.get("developers") or []),
            "publishers": list(row.get("publishers") or []),
            "platforms": dict(row.get("platforms") or {}),
            "categories": list(row.get("categories") or []),
            "achievement_total": row.get("achievement_total"),
        }

    def _metadata(self, appid: int, fallback_name: str, now: float, force: bool) -> tuple[dict[str, Any], str]:
        cached = self.cache.get_app_metadata(appid)
        ttl = STORE_TTL if cached and cached.get("metadata_status") == "ok" else STORE_NEGATIVE_TTL
        if not force and CacheDB.is_fresh(cached, ttl, now):
            status = "cached" if cached and cached.get("metadata_status") == "ok" else "unavailable"
            return cached or {}, status
        try:
            raw = self._core_api_call("get_app_details", appid)
            if raw is None:
                raise SteamResponseError("Store appdetails did not contain this app")
            metadata = self._normalise_metadata(raw, fallback_name)
            self.cache.upsert_app_metadata(appid, metadata, status="ok", fetched_at=now)
            return self.cache.get_app_metadata(appid) or metadata, "ok"
        except Exception:
            if cached is not None and cached.get("metadata_status") == "ok":
                return cached, "cached_stale"
            negative = {"name": fallback_name}
            self.cache.upsert_app_metadata(appid, negative, status="unavailable", fetched_at=now)
            return self.cache.get_app_metadata(appid) or negative, "unavailable"

    def _player_achievements(
        self,
        steamid: str,
        appid: int,
        playtime: int,
        previous_playtime: int | None,
        recently_played: bool,
        achievement_total: int | None,
        now: float,
        force: bool,
    ) -> tuple[dict[str, Any], str]:
        cached = self.cache.get_user_achievements(steamid, appid)
        if achievement_total == 0:
            if cached is None or cached.get("status") != "unsupported":
                self.cache.replace_user_achievements(
                    steamid, appid, [], playtime_forever=playtime,
                    status="unsupported", fetched_at=now,
                )
            return self.cache.get_user_achievements(steamid, appid) or {"achievements": []}, "unsupported"
        new_play = previous_playtime is None or (previous_playtime == 0 and playtime > 0)
        increased = previous_playtime is not None and playtime > previous_playtime
        stale = not CacheDB.is_fresh(cached, PLAYER_ACHIEVEMENT_TTL, now)
        retry_failed = cached is not None and cached.get("status") == "unavailable"
        refresh = force or cached is None or new_play or increased or recently_played or stale or retry_failed
        if not refresh:
            return cached, "cached"
        try:
            raw = self._core_api_call("get_player_achievements", steamid, appid)
            achievements = [
                {
                    "apiname": item.get("apiname") or item.get("name"),
                    "achieved": bool(item.get("achieved")),
                    "unlocktime": int(item.get("unlocktime") or 0),
                }
                for item in raw if item.get("apiname") or item.get("name")
            ]
            self.cache.replace_user_achievements(
                steamid, appid, achievements, playtime_forever=playtime,
                status="ok", fetched_at=now,
            )
            return self.cache.get_user_achievements(steamid, appid) or {"achievements": []}, "ok"
        except Exception:
            if cached is not None:
                return cached, "cached_stale"
            self.cache.replace_user_achievements(
                steamid, appid, [], playtime_forever=playtime,
                status="unavailable", fetched_at=now,
            )
            return self.cache.get_user_achievements(steamid, appid) or {"achievements": []}, "unavailable"

    def _schema(self, appid: int, now: float, force: bool) -> tuple[dict[str, Any], str]:
        cached = self.cache.get_achievement_schema(appid)
        if (
            not force
            and CacheDB.is_fresh(cached, SCHEMA_TTL, now)
        ):
            return cached or {"achievements": []}, "cached"
        try:
            raw = self._core_api_call("get_achievement_schema", appid)
            rows = [
                {
                    "apiname": item.get("name") or item.get("apiname"),
                    "display_name": item.get("displayName") or item.get("display_name"),
                    "description": item.get("description"),
                    "hidden": bool(int(item.get("hidden") or 0)),
                    "icon_url": item.get("icon") or item.get("icon_url"),
                    "icon_gray_url": item.get("icongray") or item.get("icon_gray_url"),
                }
                for item in raw if item.get("name") or item.get("apiname")
            ]
            self.cache.replace_achievement_schema(appid, rows, fetched_at=now)
            return self.cache.get_achievement_schema(appid) or {"achievements": []}, "ok"
        except Exception:
            return (cached or {"achievements": []}), ("cached_stale" if cached else "unavailable")

    def _global(self, appid: int, now: float, force: bool) -> tuple[dict[str, Any], str]:
        cached = self.cache.get_achievement_global(appid)
        if not force and CacheDB.is_fresh(cached, GLOBAL_RARITY_TTL, now):
            return cached or {"achievements": []}, "cached"
        try:
            raw = self._get_api().get_global_achievement_percentages(appid)
            rows = [
                {
                    "apiname": item.get("name") or item.get("apiname"),
                    "global_percent": float(item.get("percent" if "percent" in item else "global_percent")),
                }
                for item in raw
                if (item.get("name") or item.get("apiname"))
                and item.get("percent" if "percent" in item else "global_percent") is not None
            ]
            self.cache.replace_achievement_global(appid, rows, fetched_at=now)
            return self.cache.get_achievement_global(appid) or {"achievements": []}, "ok"
        except Exception:
            return (cached or {"achievements": []}), ("cached_stale" if cached else "unavailable")

    def _metadata_from_cache(
        self, appid: int, fallback_name: str, now: float
    ) -> tuple[dict[str, Any], str]:
        cached = self.cache.get_app_metadata(appid)
        if cached is None:
            return {"name": fallback_name}, "deferred"
        if cached.get("metadata_status") != "ok":
            return cached, "unavailable"
        status = "cached" if CacheDB.is_fresh(cached, STORE_TTL, now) else "cached_stale"
        return cached, status

    def _achievements_from_cache(
        self,
        steamid: str,
        appid: int,
        playtime: int,
        achievement_total: int | None,
        now: float,
    ) -> tuple[list[dict[str, Any]], str, str, str]:
        if playtime <= 0:
            return [], "not_played", "not_applicable", "not_applicable"
        if achievement_total == 0:
            return [], "unsupported", "not_applicable", "not_applicable"
        player = self.cache.get_user_achievements(steamid, appid)
        if player is None:
            return [], "deferred", "deferred", "deferred"
        if player.get("status") == "unsupported":
            return [], "unsupported", "not_applicable", "not_applicable"
        player_status = (
            "cached" if CacheDB.is_fresh(player, PLAYER_ACHIEVEMENT_TTL, now) else "cached_stale"
        )
        schema = self.cache.get_achievement_schema(appid)
        rarity = self.cache.get_achievement_global(appid)
        schema_status = (
            "deferred" if schema is None
            else "cached" if CacheDB.is_fresh(schema, SCHEMA_TTL, now)
            else "cached_stale"
        )
        rarity_status = (
            "deferred" if rarity is None
            else "cached" if CacheDB.is_fresh(rarity, GLOBAL_RARITY_TTL, now)
            else "cached_stale"
        )
        return (
            self._join_achievements(
                player,
                schema or {"achievements": []},
                rarity or {"achievements": []},
            ),
            player_status,
            schema_status,
            rarity_status,
        )

    @staticmethod
    def _join_achievements(
        player: Mapping[str, Any], schema: Mapping[str, Any], rarity: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        player_rows = {row["apiname"]: row for row in player.get("achievements") or []}
        schema_rows = {row["apiname"]: row for row in schema.get("achievements") or []}
        rarity_rows = {row["apiname"]: row for row in rarity.get("achievements") or []}
        names = sorted(set(player_rows) | set(schema_rows) | set(rarity_rows))
        return [
            {
                "api_name": name,
                "name": schema_rows.get(name, {}).get("display_name") or name,
                "description": schema_rows.get(name, {}).get("description"),
                "hidden": bool(schema_rows.get(name, {}).get("hidden", False)),
                "icon_url": schema_rows.get(name, {}).get("icon_url"),
                "icon_gray_url": schema_rows.get(name, {}).get("icon_gray_url"),
                "achieved": bool(player_rows.get(name, {}).get("achieved", False)),
                "unlock_time": int(player_rows.get(name, {}).get("unlocktime") or 0),
                "global_percent": rarity_rows.get(name, {}).get("global_percent"),
            }
            for name in names
        ]

    def _baseline_game(
        self,
        steamid: str,
        owned_game: Mapping[str, Any],
        now: float,
    ) -> dict[str, Any]:
        appid = int(owned_game["appid"])
        fallback_name = str(owned_game.get("name") or f"App {appid}")
        playtime = int(owned_game.get("playtime_forever") or 0)
        if playtime > 0:
            metadata_row, metadata_status = self._metadata_from_cache(appid, fallback_name, now)
        else:
            metadata_row, metadata_status = {"name": fallback_name}, "excluded_unplayed"
        metadata = self._public_metadata(metadata_row)
        achievements, achievement_status, schema_status, rarity_status = (
            self._achievements_from_cache(
                steamid,
                appid,
                playtime,
                metadata.get("achievement_total"),
                now,
            )
        )
        return {
            "appid": appid,
            "name": str(metadata_row.get("name") or fallback_name),
            "playtime_minutes": playtime,
            "metadata": metadata,
            "artwork_url": metadata_row.get("header_image_url"),
            "achievements": {"status": achievement_status, "items": achievements},
            "data_status": {
                "metadata": metadata_status,
                "achievements": achievement_status,
                "achievement_schema": schema_status,
                "global_rarity": rarity_status,
            },
        }

    def _enrich_game(
        self,
        steamid: str,
        game: Mapping[str, Any],
        now: float,
        force: bool,
        recently_played: bool = False,
    ) -> dict[str, Any]:
        appid = int(game["appid"])
        fallback_name = str(game.get("name") or f"App {appid}")
        playtime = int(game.get("playtime_minutes") or 0)
        metadata_row, metadata_status = self._metadata(appid, fallback_name, now, force)
        metadata = self._public_metadata(metadata_row)
        achievement_status = "not_played"
        schema_status = "not_applicable"
        rarity_status = "not_applicable"
        achievements: list[dict[str, Any]] = []
        if playtime > 0:
            cached_player = self.cache.get_user_achievements(steamid, appid)
            previous_time = (
                int(cached_player.get("playtime_forever") or 0) if cached_player is not None else None
            )
            player, achievement_status = self._player_achievements(
                steamid,
                appid,
                playtime,
                previous_time,
                recently_played,
                metadata.get("achievement_total"),
                now,
                force,
            )
            if achievement_status != "unsupported":
                schema, schema_status = self._schema(appid, now, force)
                rarity, rarity_status = self._global(appid, now, force)
                achievements = self._join_achievements(player, schema, rarity)
        return {
            "appid": appid,
            "name": str(metadata_row.get("name") or fallback_name),
            "playtime_minutes": playtime,
            "metadata": metadata,
            "artwork_url": metadata_row.get("header_image_url"),
            "achievements": {"status": achievement_status, "items": achievements},
            "data_status": {
                "metadata": metadata_status,
                "achievements": achievement_status,
                "achievement_schema": schema_status,
                "global_rarity": rarity_status,
            },
        }

    def _collection_profile(
        self,
        *,
        run_id: str,
        player_alias: str,
        generated_at: float,
        snapshot_id: str,
        collected_at: float,
        source: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "player_alias": player_alias,
            "generated_at": self._iso_time(generated_at),
            "data_snapshot": {
                "id": snapshot_id,
                "collected_at": self._iso_time(collected_at),
                "enriched_at": None,
                "source": source,
            },
            "data_status": deepcopy(payload.get("data_status") or {}),
            "games": deepcopy(payload.get("games") or []),
        }

    def collect(
        self,
        identity: str,
        *,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Collect the library baseline without cold-scanning Store metadata."""

        _report_progress(progress, "Checking the 24-hour player snapshot", 0, 3)
        try:
            normalized = normalize_identity(identity)
        except InvalidSteamIdentity:
            # Test doubles can intentionally use opaque identities; real callers
            # always go through the strict URL/SteamID contract above.
            if self.api is None:
                raise
            normalized = NormalizedIdentity(f"adapter:{str(identity).strip()}", vanity=str(identity).strip())
        normalized_key_hash = hashlib.sha256(normalized.key.encode("utf-8")).hexdigest()
        steamid = normalized.steamid
        player_alias = "Steam Player"
        if steamid is None:
            mapping = self.cache.get_identity_resolution(normalized_key_hash)
            if mapping is not None:
                steamid = str(mapping["steamid"])
                player_alias = str(mapping["player_alias"])

        checked_at = float(self.clock())
        snapshot = (
            self.cache.get_collection_snapshot(steamid, now=checked_at)
            if steamid is not None else None
        )
        if snapshot is not None and not force:
            payload = snapshot["collection_payload"]
            cached_alias = str(payload.get("player_alias") or snapshot["player_alias"])
            run_id = f"sv-{int(checked_at)}-{uuid.uuid4().hex[:8]}"
            _report_progress(progress, "Restoring the cached collection snapshot", 1, 3)
            self.cache.record_run(
                run_id, str(snapshot["steamid"]), cached_alias, len(payload.get("games") or []),
                generated_at=checked_at,
            )
            _report_progress(progress, "Collection snapshot restored", 3, 3)
            return self._collection_profile(
                run_id=run_id,
                player_alias=cached_alias,
                generated_at=checked_at,
                snapshot_id=str(snapshot["snapshot_id"]),
                collected_at=float(snapshot["collected_at"]),
                source="cached_snapshot",
                payload=payload,
            )

        _report_progress(progress, "Resolving the Steam identity", 0, 3)
        if steamid is None:
            resolved = self._get_api().resolve_identity(identity)
            steamid = str(resolved.steamid)
            player_alias = str(resolved.player_alias or "Steam Player")
            self.cache.upsert_identity_resolution(
                normalized_key_hash, steamid, player_alias, resolved_at=checked_at
            )
        run_id = f"sv-{int(checked_at)}-{uuid.uuid4().hex[:8]}"
        _report_progress(progress, "Requesting the visible owned-game library", 1, 3)
        try:
            owned = self._get_api().get_owned_games(steamid)
        except SteamDataError:
            raise
        except Exception:
            raise SteamRequestError("GetOwnedGames failed unexpectedly") from None
        now = float(self.clock())
        _report_progress(progress, "Normalizing and caching the library", 2, 3)
        normalized_games = [
            self._baseline_game(steamid, owned_game, now)
            for owned_game in sorted(owned, key=lambda item: int(item.get("appid") or 0))
        ]

        snapshot_id = f"svdata-{uuid.uuid4().hex}"
        collection_payload = {
            "player_alias": player_alias,
            "games": normalized_games,
            "data_status": {
                "owned_games": "ok",
                "recently_played": "deferred",
                "enrichment": "cached-only",
            },
        }
        self.cache.replace_collection_snapshot(
            steamid,
            snapshot_id,
            player_alias,
            now,
            collection_payload,
        )
        self.cache.replace_user_games(steamid, owned, snapshot_at=now)
        self.cache.record_run(
            run_id, steamid, player_alias, len(normalized_games), generated_at=now
        )
        _report_progress(progress, "Library collection complete", 3, 3)
        return self._collection_profile(
            run_id=run_id,
            player_alias=player_alias,
            generated_at=now,
            snapshot_id=snapshot_id,
            collected_at=now,
            source="network",
            payload=collection_payload,
        )

    def enrich_played_profile(
        self,
        profile: Mapping[str, Any],
        *,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Fetch Store and achievement enrichment for every played game."""
        run_id = str(profile.get("run_id") or "")
        context = self.cache.get_run_identity(run_id)
        if context is None:
            raise SteamDataError("Run identity context is unavailable from the local cache")
        games = [dict(game) for game in profile.get("games", []) if isinstance(game, Mapping)]
        snapshot_info = profile.get("data_snapshot")
        profile_snapshot_id = (
            str(snapshot_info.get("id"))
            if isinstance(snapshot_info, Mapping) and snapshot_info.get("id") else None
        )
        now = float(self.clock())
        cached_enrichment = None
        if not force and profile_snapshot_id is not None:
            cached_enrichment = self.cache.get_enrichment_snapshot(
                str(context["steamid"]), snapshot_id=profile_snapshot_id, now=now
            )
        if cached_enrichment is not None:
            payload = cached_enrichment["enrichment_payload"]
            result = dict(profile)
            result["games"] = deepcopy(payload["games"])
            result["data_status"] = deepcopy(payload["data_status"])
            result["enriched_at"] = self._iso_time(float(cached_enrichment["enriched_at"]))
            result["data_snapshot"] = {
                "id": str(cached_enrichment["snapshot_id"]),
                "collected_at": self._iso_time(float(cached_enrichment["collected_at"])),
                "enriched_at": result["enriched_at"],
                "source": "cached_snapshot",
            }
            prior_enrichment_status = result["data_status"].get("enrichment")
            prior_enrichment_status = (
                dict(prior_enrichment_status)
                if isinstance(prior_enrichment_status, Mapping) else {}
            )
            result["data_status"]["enrichment"] = {
                **prior_enrichment_status,
                "source": "cached_snapshot",
                "reused": len(result["games"]),
                "requested": sum(
                    1 for game in result["games"]
                    if int(game.get("playtime_minutes") or 0) > 0
                ),
            }
            _report_progress(progress, "Checking the 24-hour enrichment snapshot", 0, 3)
            _report_progress(progress, "Restoring cached played-game enrichment", 1, 3)
            result["evidence_fingerprint"] = compute_evidence_fingerprint(result)
            _report_progress(progress, "Enrichment snapshot restored", 3, 3)
            return result

        selected = {
            int(game["appid"])
            for game in games
            if game.get("appid") is not None and int(game.get("playtime_minutes") or 0) > 0
        }
        if selected:
            _report_progress(progress, "Loading recently played context", 0, len(selected))
            try:
                recent = self._get_api().get_recently_played(str(context["steamid"]))
                recent_appids = {
                    int(item["appid"]) for item in recent if item.get("appid") is not None
                }
                recent_status = "ok"
            except Exception:
                recent_appids = set()
                recent_status = "unavailable"
        else:
            recent_appids = set()
            recent_status = "not_applicable"
        enriched: list[dict[str, Any]] = []
        completed = 0
        if not selected:
            _report_progress(progress, "No played games require enrichment", 0, 0)
        else:
            _report_progress(progress, "Enriching played-game data", 0, len(selected))
        for game in games:
            if int(game["appid"]) in selected:
                enriched.append(
                    self._enrich_game(
                        str(context["steamid"]),
                        game,
                        now,
                        force,
                        int(game["appid"]) in recent_appids,
                    )
                )
                completed += 1
                _report_progress(
                    progress,
                    "Enriching played-game data",
                    completed,
                    len(selected),
                )
            else:
                enriched.append(game)
        result = dict(profile)
        result["games"] = enriched
        status = dict(result.get("data_status") or {})
        status["recently_played"] = recent_status
        status["enrichment"] = {
            "mode": "played-games",
            "requested": len(selected),
            "source": "network",
            "reused": 0,
        }
        result["data_status"] = status
        enriched_at = float(self.clock())
        result["enriched_at"] = self._iso_time(enriched_at)
        current_snapshot = self.cache.get_acquisition_snapshot(str(context["steamid"]))
        same_snapshot = (
            current_snapshot is not None
            and profile_snapshot_id is not None
            and str(current_snapshot["snapshot_id"]) == profile_snapshot_id
        )
        if same_snapshot:
            result["data_snapshot"] = {
                "id": profile_snapshot_id,
                "collected_at": self._iso_time(float(current_snapshot["collected_at"])),
                "enriched_at": result["enriched_at"],
                "source": "network",
            }
        elif isinstance(snapshot_info, Mapping):
            result["data_snapshot"] = {
                **dict(snapshot_info),
                "source": "network",
            }
        result["evidence_fingerprint"] = compute_evidence_fingerprint(result)
        if same_snapshot:
            self.cache.replace_enrichment_snapshot(
                str(context["steamid"]),
                profile_snapshot_id,
                enriched_at,
                {
                    "games": deepcopy(result["games"]),
                    "data_status": deepcopy(result["data_status"]),
                    "enriched_at": result["enriched_at"],
                },
            )
        return result
