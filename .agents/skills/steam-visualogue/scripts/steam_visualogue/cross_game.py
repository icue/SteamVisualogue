"""Deterministic non-series pattern candidates for multi-game pages."""

from __future__ import annotations

from statistics import median
from typing import Any, Mapping, Sequence


def _signal_value(section: Mapping[str, Any], key: str, default: Any) -> Any:
    signal = section.get(key)
    if isinstance(signal, Mapping) and "value" in signal:
        return signal.get("value")
    return default


def _signal_coverage(section: Mapping[str, Any], key: str) -> dict[str, Any]:
    signal = section.get(key)
    coverage = signal.get("coverage") if isinstance(signal, Mapping) else None
    return dict(coverage) if isinstance(coverage, Mapping) else {}


def _minutes(game: Mapping[str, Any]) -> float:
    value = game.get("playtime_minutes")
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _game_index(profile: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_games = profile.get("games", [])
    games = raw_games if isinstance(raw_games, Sequence) else []
    return {
        str(game.get("appid")): game
        for game in games
        if isinstance(game, Mapping) and str(game.get("appid", "")).isdigit()
    }


def _base_row(
    appid: Any,
    name: Any,
    game_by_app: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    appid_text = str(appid)
    game = game_by_app.get(appid_text, {})
    minutes = round(_minutes(game), 6)
    return {
        "game_id": f"game:{appid_text}",
        "appid": appid_text,
        "name": str(name or game.get("name") or appid_text),
        "playtime_minutes": minutes,
        "playtime_hours": round(minutes / 60, 3),
    }


def _group(
    pattern_key: str,
    rows: list[dict[str, Any]],
    *,
    strength: float,
    scope: str,
    thresholds: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    for row in rows:
        appid = str(row.get("appid", ""))
        row["evidence_ids"] = [
            f"pattern:cross-game-atlas:{pattern_key}:{appid}",
            f"game:{appid}",
        ]
    return {
        "pattern_key": pattern_key,
        "game_count": len(rows),
        "games": rows,
        "strength": round(max(0.0, min(0.98, float(strength))), 6),
        "scope": scope,
        "thresholds": dict(thresholds),
        "coverage": dict(coverage),
    }


def discover_cross_game_patterns(
    profile: Mapping[str, Any],
    signals: Mapping[str, Any],
    *,
    max_games: int = 4,
) -> list[dict[str, Any]]:
    """Return 3–4 game groups sharing a measurable, non-series pattern."""

    if not isinstance(profile, Mapping):
        raise TypeError("profile must be a mapping")
    if not isinstance(signals, Mapping):
        raise TypeError("signals must be a mapping")
    game_by_app = _game_index(profile)
    maximum = max(3, min(4, int(max_games)))
    achievements = signals.get("achievements", {})
    if not isinstance(achievements, Mapping):
        return []
    groups: list[dict[str, Any]] = []

    timelines = _signal_value(achievements, "timelines", [])
    timeline_rows = [row for row in timelines if isinstance(row, Mapping)] if isinstance(timelines, Sequence) else []
    long_return = [
        row
        for row in timeline_rows
        if int(row.get("comeback_count", 0) or 0) > 0
        and float(row.get("achievement_span_days", 0.0) or 0.0) >= 730
    ]
    if len(long_return) >= 3:
        selected = sorted(
            long_return,
            key=lambda row: (
                -float(row.get("achievement_span_days", 0.0) or 0.0),
                -float(row.get("largest_gap_days", 0.0) or 0.0),
                str(row.get("appid", "")),
            ),
        )[:maximum]
        rows: list[dict[str, Any]] = []
        for source in selected:
            row = _base_row(source.get("appid"), source.get("name"), game_by_app)
            row.update({
                "achievement_span_days": round(float(source.get("achievement_span_days", 0.0) or 0.0), 6),
                "largest_gap_days": round(float(source.get("largest_gap_days", 0.0) or 0.0), 6),
                "comeback_count": int(source.get("comeback_count", 0) or 0),
                "activity_year_count": int(source.get("activity_year_count", 0) or 0),
            })
            rows.append(row)
        average_span = sum(row["achievement_span_days"] for row in rows) / len(rows)
        average_comebacks = sum(row["comeback_count"] for row in rows) / len(rows)
        groups.append(_group(
            "long_return",
            rows,
            strength=0.55 + min(0.28, average_span / (365 * 10)) + min(0.12, average_comebacks / 20),
            scope="distinct games with timestamped achievement activity spanning at least two years and at least one comeback",
            thresholds={"minimum_span_days": 730, "minimum_comeback_count": 1},
            coverage=_signal_coverage(achievements, "timelines"),
        ))

    burst_minimum = 3
    threshold_signal = signals.get("thresholds", {})
    if isinstance(threshold_signal, Mapping):
        try:
            burst_minimum = max(1, int(threshold_signal.get("burst_min_unlocks", burst_minimum)))
        except (TypeError, ValueError):
            pass
    burst_rows = [
        row
        for row in timeline_rows
        if int(row.get("burst_count", 0) or 0) > 0
        and float(row.get("max_unlocks_in_24h", 0.0) or 0.0) >= burst_minimum
    ]
    if len(burst_rows) >= 3:
        selected = sorted(
            burst_rows,
            key=lambda row: (
                -float(row.get("max_unlocks_in_24h", 0.0) or 0.0),
                -int(row.get("burst_count", 0) or 0),
                str(row.get("appid", "")),
            ),
        )[:maximum]
        rows = []
        for source in selected:
            row = _base_row(source.get("appid"), source.get("name"), game_by_app)
            row.update({
                "max_unlocks_in_24h": round(float(source.get("max_unlocks_in_24h", 0.0) or 0.0), 6),
                "burst_count": int(source.get("burst_count", 0) or 0),
                "burst_dates": list(source.get("burst_dates", [])) if isinstance(source.get("burst_dates"), list) else [],
            })
            rows.append(row)
        average_burst = sum(row["max_unlocks_in_24h"] for row in rows) / len(rows)
        groups.append(_group(
            "achievement_burst",
            rows,
            strength=0.54 + min(0.4, average_burst / 60),
            scope="distinct games with timestamped achievement bursts inside a 24-hour window",
            thresholds={"minimum_unlocks": burst_minimum, "window_hours": 24},
            coverage=_signal_coverage(achievements, "timelines"),
        ))

    completion_rows = _signal_value(achievements, "completion_by_game", [])
    completion_rows = [row for row in completion_rows if isinstance(row, Mapping)] if isinstance(completion_rows, Sequence) else []
    attention_rows = [
        {
            "appid": str(row.get("appid", "")),
            "name": row.get("name"),
            "completion": float(row.get("completion", 0.0) or 0.0),
            "playtime_minutes": _minutes(game_by_app.get(str(row.get("appid", "")), {})),
        }
        for row in completion_rows
        if str(row.get("appid", "")).isdigit()
        and _minutes(game_by_app.get(str(row.get("appid", "")), {})) > 0
    ]
    if len(attention_rows) >= 3:
        midpoint = median(row["playtime_minutes"] for row in attention_rows)
        fast_finishers = [
            row for row in attention_rows
            if row["completion"] >= 0.70 and row["playtime_minutes"] <= midpoint
        ]
        deep_partial = [
            row for row in attention_rows
            if row["completion"] <= 0.50 and row["playtime_minutes"] > midpoint
        ]
        if fast_finishers and deep_partial:
            fast_finishers.sort(key=lambda row: (-row["completion"], row["playtime_minutes"], row["appid"]))
            deep_partial.sort(key=lambda row: (-row["playtime_minutes"], row["completion"], row["appid"]))
            selected = (fast_finishers[:2] + deep_partial[:2])[:maximum]
            rows = []
            for source in selected:
                row = _base_row(source["appid"], source["name"], game_by_app)
                row.update({
                    "completion": round(source["completion"], 6),
                    "contrast_role": "fast_finish" if source in fast_finishers else "deep_partial",
                })
                rows.append(row)
            if len(rows) >= 3:
                completion_gap = max(row["completion"] for row in rows) - min(row["completion"] for row in rows)
                groups.append(_group(
                    "completion_attention_divergence",
                    rows,
                    strength=0.62 + min(0.3, completion_gap),
                    scope="played games with player achievement data, contrasting lower-attention higher-completion titles with deeper lower-completion titles",
                    thresholds={
                        "fast_finish_completion": 0.70,
                        "deep_partial_completion": 0.50,
                        "attention_midpoint_minutes": round(midpoint, 6),
                    },
                    coverage=_signal_coverage(achievements, "completion_by_game"),
                ))

    groups.sort(key=lambda group: (-float(group["strength"]), str(group["pattern_key"])))
    return groups[:12]


__all__ = ["discover_cross_game_patterns"]
