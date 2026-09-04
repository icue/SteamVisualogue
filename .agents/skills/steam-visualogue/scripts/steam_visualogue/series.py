"""Conservative same-series discovery from normalized Steam game titles.

This module intentionally produces candidates, not authoritative franchise
labels.  It only groups played games whose normalized titles share a stable
leading prefix.  Semantic review remains responsible for confirming that a
candidate is actually one series before it becomes reader-facing copy.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence


_GENERIC_PREFIXES = {
    "adventure",
    "battle",
    "call",
    "dark",
    "dead",
    "dungeon",
    "dragon",
    "dream",
    "fate",
    "final",
    "ghost",
    "hero",
    "king",
    "kingdom",
    "legend",
    "life",
    "magic",
    "night",
    "project",
    "state",
    "shadow",
    "star",
    "tower",
    "the",
    "world",
}

_GENERIC_MULTIWORD_PREFIXES = {
    "sid meier",
}

_CONNECTOR_TOKENS = {
    "a",
    "and",
    "at",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
}


def _title_tokens(value: Any) -> list[str]:
    raw_text = str(value or "").replace("™", "").replace("®", "").replace("©", "")
    text = unicodedata.normalize("NFKC", raw_text).casefold()
    text = re.sub(r"['’]s\b", "", text)
    text = text.replace("’", "'")
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    return tokens


def _prefix_candidates(tokens: Sequence[str]) -> list[tuple[str, int]]:
    if not tokens:
        return []
    candidates: list[tuple[str, int]] = []
    for length in range(min(4, len(tokens)), 0, -1):
        prefix = " ".join(tokens[:length])
        if prefix in _GENERIC_MULTIWORD_PREFIXES or tokens[length - 1] in _CONNECTOR_TOKENS:
            continue
        if length == 1:
            token = tokens[0]
            if token in _GENERIC_PREFIXES or token.isdigit() or len(token) < 4:
                continue
        candidates.append((prefix, length))
    return candidates


def _playtime_minutes(game: Mapping[str, Any]) -> float:
    value = game.get("playtime_minutes")
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number)


def _release_year(game: Mapping[str, Any]) -> int | None:
    metadata = game.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("release_year")
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1 <= year <= 9999 else None


def _game_row(game: Mapping[str, Any]) -> dict[str, Any]:
    appid = str(game.get("appid", "")).strip()
    minutes = round(_playtime_minutes(game), 6)
    row: dict[str, Any] = {
        "game_id": f"game:{appid}",
        "appid": appid,
        "name": str(game.get("name") or f"App {appid}"),
        "playtime_minutes": minutes,
        "playtime_hours": round(minutes / 60, 3),
    }
    release_year = _release_year(game)
    if release_year is not None:
        row["release_year"] = release_year

    achievements = game.get("achievements")
    items = achievements.get("items") if isinstance(achievements, Mapping) else None
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
        achievement_items = [item for item in items if isinstance(item, Mapping)]
        total = len(achievement_items)
        unlocked = sum(1 for item in achievement_items if bool(item.get("achieved")))
        row["achievements_total"] = total
        row["achievements_unlocked"] = unlocked
        if total:
            row["achievement_completion"] = round(unlocked / total, 6)
    return row


def discover_series_groups(
    profile: Mapping[str, Any],
    *,
    min_games: int = 3,
    max_games: int = 4,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Return deterministic 3–4 game same-series candidates.

    The detector uses played titles only and deliberately favors precision:
    one-word prefixes must be distinctive, while generic words such as
    ``shadow`` or ``world`` require a multi-word prefix.  Groups larger than
    ``max_games`` are reduced to the most-played members so downstream page
    contracts never name more than four games.
    """

    if not isinstance(profile, Mapping):
        raise TypeError("profile must be a mapping")
    raw_games = profile.get("games", [])
    games = [game for game in raw_games if isinstance(game, Mapping)] if isinstance(raw_games, Sequence) else []
    candidates: dict[str, dict[str, Any]] = {}
    for game in games:
        if _playtime_minutes(game) <= 0:
            continue
        appid = str(game.get("appid", "")).strip()
        name = str(game.get("name") or "").strip()
        if not appid.isdigit() or int(appid) <= 0 or not name:
            continue
        tokens = _title_tokens(name)
        for prefix, prefix_length in _prefix_candidates(tokens):
            bucket = candidates.setdefault(
                prefix,
                {"prefix": prefix, "prefix_length": prefix_length, "games": []},
            )
            bucket["games"].append(game)

    groups: list[dict[str, Any]] = []
    minimum = max(3, int(min_games))
    maximum = max(minimum, min(4, int(max_games)))
    for prefix, candidate in candidates.items():
        members = {
            str(game.get("appid")): game
            for game in candidate["games"]
            if str(game.get("appid", "")).isdigit()
        }
        if len(members) < minimum:
            continue
        ranked = sorted(
            members.values(),
            key=lambda game: (-_playtime_minutes(game), str(game.get("appid", "")), str(game.get("name", ""))),
        )
        selected = ranked[:maximum]
        if len(selected) < minimum:
            continue
        rows = [_game_row(game) for game in selected]
        rows.sort(key=lambda row: (row.get("release_year", 9999), row["appid"], row["name"]))
        prefix_length = int(candidate["prefix_length"])
        strength = 0.72 + 0.08 * (len(rows) - minimum)
        if prefix_length >= 2:
            strength += 0.12
        if len(members) > len(rows):
            strength += 0.03
        playtimes = [float(row.get("playtime_minutes", 0.0) or 0.0) for row in rows]
        if len(playtimes) >= 2:
            if playtimes[-1] >= playtimes[0] * 2.0 and playtimes[-1] >= 300:
                growth_arc = "breakthrough_sequel"
            elif playtimes[0] >= playtimes[-1] * 2.0 and playtimes[0] >= 300:
                growth_arc = "diminishing_engagement"
            elif all(p >= 600 for p in playtimes):
                growth_arc = "sustained_devotion"
            else:
                growth_arc = "steady_progression"
        else:
            growth_arc = "single_entry"
        groups.append(
            {
                "series_key": re.sub(r"[^a-z0-9]+", "_", prefix).strip("_"),
                "series_prefix": prefix,
                "prefix_length": prefix_length,
                "game_count": len(rows),
                "candidate_pool_size": len(members),
                "growth_arc": growth_arc,
                "games": rows,
                "strength": round(min(0.98, strength), 6),
                "scope": "played games sharing a conservative normalized title prefix; semantic confirmation required",
            }
        )

    # Different conservative prefixes can describe the exact same member
    # set (for example ``nioh`` and ``nioh complete``).  Keep only the
    # strongest representation of each stable member-set key.
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for group in groups:
        member_key = tuple(sorted(str(row.get("game_id")) for row in group["games"]))
        previous = unique.get(member_key)
        if previous is None or (
            -float(group["strength"]),
            -int(group["prefix_length"]),
            str(group["series_key"]),
        ) < (
            -float(previous["strength"]),
            -int(previous["prefix_length"]),
            str(previous["series_key"]),
        ):
            unique[member_key] = group
    groups = list(unique.values())
    groups.sort(
        key=lambda group: (
            -float(group["strength"]),
            -int(group["game_count"]),
            -sum(float(row["playtime_minutes"]) for row in group["games"]),
            str(group["series_key"]),
        )
    )
    return groups[: min(12, max(0, int(limit)))]


__all__ = ["discover_series_groups"]
