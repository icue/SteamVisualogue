from __future__ import annotations

from typing import Any


class FakeTime:
    """Deterministic clock and sleeper shared by API/data-layer tests."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = float(now)
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        delay = float(seconds)
        self.sleeps.append(delay)
        self.now += delay


def close_apis(*apis: Any) -> None:
    """Close test API clients without requiring every fake to implement close."""

    for api in apis:
        close = getattr(api, "close", None)
        if callable(close):
            close()
