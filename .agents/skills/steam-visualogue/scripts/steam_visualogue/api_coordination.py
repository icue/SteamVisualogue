"""Persistent request accounting and host pacing for Steam API clients.

The coordinator is intentionally separate from the user-data cache.  It is a
small, machine-local ledger shared by every run of one Skill installation.
Reservations are committed before network I/O so a process crash cannot make
the local request count optimistic.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit


COORDINATION_BUSY_TIMEOUT_MS = 5_000
DEFAULT_DAILY_REQUEST_CEILING = 90_000
DEFAULT_HOST_INTERVALS = {
    "api.steampowered.com": 0.250,
    "store.steampowered.com": 1.000,
}


class CoordinationError(RuntimeError):
    """The persistent coordination ledger could not safely be used."""


class CoordinationQuotaExceeded(CoordinationError):
    """A keyed request reservation would exceed the daily safety ceiling."""


@dataclass(frozen=True)
class RequestReservation:
    host: str
    scope: str
    delay: float
    utc_date: str
    count: int | None


def host_from_url(host_or_url: str) -> str:
    parsed = urlsplit(str(host_or_url))
    return (parsed.hostname or str(host_or_url).split(":", 1)[0]).casefold()


class APICoordination:
    """A fail-closed SQLite request ledger with short write transactions."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        host_intervals: Mapping[str, float] | None = None,
        busy_timeout_ms: int = COORDINATION_BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = str(path)
        self.clock = clock
        intervals = DEFAULT_HOST_INTERVALS if host_intervals is None else host_intervals
        self.host_intervals = {
            str(host).casefold(): max(0.0, float(interval))
            for host, interval in intervals.items()
        }
        if self.path != ":memory:":
            try:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CoordinationError("Steam request coordination storage is unavailable") from exc
        self._connection = None
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=max(0.0, float(busy_timeout_ms)) / 1000.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute(f"PRAGMA busy_timeout = {max(0, int(busy_timeout_ms))}")
            if self.path != ":memory:":
                journal_mode = str(self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).casefold()
                if journal_mode != "wal":
                    raise CoordinationError("Steam request coordination storage is unavailable")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_request_ledger (
                    api_key_sha256 TEXT NOT NULL,
                    utc_date TEXT NOT NULL,
                    host TEXT NOT NULL,
                    request_count INTEGER NOT NULL CHECK (request_count >= 0),
                    PRIMARY KEY (api_key_sha256, utc_date, host)
                );

                CREATE TABLE IF NOT EXISTS api_host_state (
                    scope TEXT NOT NULL,
                    host TEXT NOT NULL,
                    next_allowed_at REAL NOT NULL,
                    blocked_until REAL NOT NULL,
                    PRIMARY KEY (scope, host)
                );
                """
            )
        except (CoordinationError, OSError, sqlite3.Error) as exc:
            try:
                if self._connection is not None:
                    self._connection.close()
            except Exception:
                pass
            if isinstance(exc, CoordinationError):
                raise
            raise CoordinationError("Steam request coordination storage is unavailable") from exc

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error as exc:
            raise CoordinationError("Steam request coordination storage is unavailable") from exc

    def __enter__(self) -> "APICoordination":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _finite_timestamp(value: float) -> float:
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ValueError("coordination timestamp must be finite")
        return timestamp

    def interval_for(self, host_or_url: str) -> float:
        return self.host_intervals.get(host_from_url(host_or_url), 0.0)

    def _transaction(self, callback):
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                result = callback()
            except Exception:
                self._connection.rollback()
                raise
            self._connection.commit()
            return result
        except CoordinationQuotaExceeded:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise
        except (OSError, sqlite3.Error) as exc:
            try:
                self._connection.rollback()
            except Exception:
                pass
            raise CoordinationError("Steam request coordination storage is unavailable") from exc

    def reserve_request(
        self,
        host_or_url: str,
        *,
        api_key_sha256: str | None,
        daily_ceiling: int = DEFAULT_DAILY_REQUEST_CEILING,
        now: float | None = None,
    ) -> RequestReservation:
        """Atomically reserve quota and a host slot before network I/O."""

        host = host_from_url(host_or_url)
        scope = str(api_key_sha256 or "store").strip()
        if not host or not scope:
            raise CoordinationError("Steam request coordination scope is invalid")
        if api_key_sha256 and not re.fullmatch(r"[0-9a-f]{64}", scope):
            raise CoordinationError("Steam request coordination scope is invalid")
        timestamp = self._finite_timestamp(self.clock() if now is None else now)
        ceiling = min(DEFAULT_DAILY_REQUEST_CEILING, int(daily_ceiling))
        if ceiling < 0:
            raise ValueError("daily request ceiling must be non-negative")
        utc_date = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        interval = self.interval_for(host)

        def reserve() -> RequestReservation:
            count: int | None = None
            if api_key_sha256:
                row = self._connection.execute(
                    """
                    SELECT request_count FROM api_request_ledger
                    WHERE api_key_sha256 = ? AND utc_date = ? AND host = ?
                    """,
                    (scope, utc_date, host),
                ).fetchone()
                count = int(row["request_count"]) if row is not None else 0
                if count >= ceiling:
                    raise CoordinationQuotaExceeded("Steam Web API daily safety ceiling reached")
                count += 1
                self._connection.execute(
                    """
                    INSERT INTO api_request_ledger (api_key_sha256, utc_date, host, request_count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(api_key_sha256, utc_date, host) DO UPDATE SET
                        request_count = excluded.request_count
                    """,
                    (scope, utc_date, host, count),
                )

            row = self._connection.execute(
                """
                SELECT next_allowed_at, blocked_until FROM api_host_state
                WHERE scope = ? AND host = ?
                """,
                (scope, host),
            ).fetchone()
            existing_next = float(row["next_allowed_at"]) if row is not None else 0.0
            existing_blocked = float(row["blocked_until"]) if row is not None else 0.0
            allowed_at = max(timestamp, existing_next, existing_blocked)
            next_allowed = allowed_at + interval
            self._connection.execute(
                """
                INSERT INTO api_host_state (scope, host, next_allowed_at, blocked_until)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope, host) DO UPDATE SET
                    next_allowed_at = excluded.next_allowed_at,
                    blocked_until = max(api_host_state.blocked_until, excluded.blocked_until)
                """,
                (scope, host, next_allowed, existing_blocked),
            )
            return RequestReservation(
                host=host,
                scope=scope,
                delay=max(0.0, allowed_at - timestamp),
                utc_date=utc_date,
                count=count,
            )

        return self._transaction(reserve)

    def record_cooldown(
        self,
        host_or_url: str,
        *,
        scope: str,
        blocked_until: float,
    ) -> float:
        """Extend shared cooldowns without shortening an existing deadline."""

        host = host_from_url(host_or_url)
        deadline = self._finite_timestamp(blocked_until)
        scope = str(scope).strip()
        if not host or not scope:
            raise CoordinationError("Steam request cooldown scope is invalid")

        def update() -> float:
            row = self._connection.execute(
                """
                SELECT next_allowed_at, blocked_until FROM api_host_state
                WHERE scope = ? AND host = ?
                """,
                (scope, host),
            ).fetchone()
            existing_next = float(row["next_allowed_at"]) if row is not None else 0.0
            existing_blocked = float(row["blocked_until"]) if row is not None else 0.0
            new_blocked = max(existing_blocked, deadline)
            new_next = max(existing_next, deadline)
            self._connection.execute(
                """
                INSERT INTO api_host_state (scope, host, next_allowed_at, blocked_until)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope, host) DO UPDATE SET
                    next_allowed_at = max(api_host_state.next_allowed_at, excluded.next_allowed_at),
                    blocked_until = max(api_host_state.blocked_until, excluded.blocked_until)
                """,
                (scope, host, new_next, new_blocked),
            )
            return new_blocked

        return self._transaction(update)


__all__ = [
    "APICoordination",
    "COORDINATION_BUSY_TIMEOUT_MS",
    "CoordinationError",
    "CoordinationQuotaExceeded",
    "DEFAULT_DAILY_REQUEST_CEILING",
    "DEFAULT_HOST_INTERVALS",
    "RequestReservation",
    "host_from_url",
]
