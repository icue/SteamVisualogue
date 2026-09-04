"""Shared, bounded timestamp parsing for analytics and semantic candidates."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any


def parse_timestamp(value: Any) -> float | None:
    """Parse a positive Unix or ISO timestamp without leaking platform errors."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = None
    if number is not None:
        if not math.isfinite(number) or number <= 0:
            return None
        try:
            datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return number
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


__all__ = ["parse_timestamp"]
