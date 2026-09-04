"""Conservative independent request pacing for each remote host."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit


DEFAULT_HOST_INTERVALS = {
    "api.steampowered.com": 0.250,
    "store.steampowered.com": 1.000,
}


def jittered_backoff(
    attempt: int,
    *,
    randomizer: Callable[[], float],
    cap: float = 30.0,
) -> float:
    """Return equal-jitter exponential backoff in ``[base/2, base]``."""
    base = min(max(0.0, float(cap)), float(2 ** max(0, int(attempt))))
    try:
        sample = float(randomizer())
    except (TypeError, ValueError):
        sample = 0.5
    sample = min(1.0, max(0.0, sample))
    return base * (0.5 + sample * 0.5)


class RateLimiter:
    """Serialize and pace calls independently per host.

    ``wait`` reserves the current request slot before returning.  Supplying a
    fake clock and sleeper makes the behavior fully testable without wall time.
    """

    def __init__(
        self,
        host_intervals: Mapping[str, float] | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        intervals = DEFAULT_HOST_INTERVALS if host_intervals is None else host_intervals
        self.host_intervals = {
            key.lower(): max(0.0, float(value))
            for key, value in intervals.items()
        }
        self.sleeper = sleeper
        self.clock = clock
        self._last_request: dict[str, float] = {}
        self._not_before: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def interval_for(self, host_or_url: str) -> float:
        host = self._host(host_or_url)
        return self.host_intervals.get(host, 0.0)

    @staticmethod
    def _host(host_or_url: str) -> str:
        parsed = urlsplit(host_or_url)
        return (parsed.hostname or host_or_url.split(":", 1)[0]).lower()

    def wait(self, host_or_url: str) -> float:
        host = self._host(host_or_url)
        with self._guard:
            lock = self._locks.setdefault(host, threading.Lock())
        with lock:
            now = self.clock()
            last = self._last_request.get(host)
            paced_at = now if last is None else last + self.interval_for(host)
            allowed_at = max(paced_at, self._not_before.get(host, now))
            delay = max(0.0, allowed_at - now)
            if delay:
                self.sleeper(delay)
                now = self.clock()
            self._last_request[host] = max(now, allowed_at)
            return delay

    def defer(self, host_or_url: str, delay: float) -> float:
        """Share a server-requested cooldown with every worker for one host."""
        host = self._host(host_or_url)
        with self._guard:
            lock = self._locks.setdefault(host, threading.Lock())
        with lock:
            deadline = self.clock() + max(0.0, float(delay))
            self._not_before[host] = max(self._not_before.get(host, 0.0), deadline)
            return self._not_before[host]


__all__ = ["RateLimiter", "jittered_backoff"]
