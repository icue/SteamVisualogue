"""Deterministic analytics for a normalized Steam Visualogue profile.

The module deliberately produces factual signals rather than editorial or
personality claims.  Every public metric carries its own provenance and
coverage so downstream agents can decide whether it is safe to use.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
import re
from statistics import fmean, median, pvariance
from typing import Any, Mapping, Sequence

from .cross_game import discover_cross_game_patterns
from .measurements import clamp
from .series import discover_series_groups
from .time_utils import parse_timestamp


DEFAULT_CONFIG: dict[str, Any] = {
    "meaningful_playtime_minutes": 120,
    "comeback_gap_days": 90,
    "burst_window_hours": 24,
    "burst_min_unlocks": 3,
    "recent_years": 5,
    "old_game_age_years": 15,
    "rarity_probability_floor": 1e-6,
    "surprise_limit": 10,
    "tension_owned_minimum": 20,
    "tension_low_engagement": 0.35,
    "tension_high_top1_share": 0.50,
    "tension_low_completion": 0.35,
    "tension_release_year_gap": 5.0,
}

SOURCE = "derived:normalized_profile"

_ATTRIBUTE_ALIASES = {
    "indie": "Indie",
    "independent": "Indie",
    "free to play": "Free to Play",
    "free-to-play": "Free to Play",
    "f2p": "Free to Play",
    "early access": "Early Access",
    "early-access": "Early Access",
}

_GENRE_ALIASES = {
    "action": "Action",
    "action adventure": "Action",
    "action-adventure": "Action",
    "adventure": "Adventure",
    "casual": "Casual",
    "fighting": "Fighting",
    "horror": "Horror",
    "massively multiplayer": "Massively Multiplayer",
    "mmo": "Massively Multiplayer",
    "platformer": "Platformer",
    "platform": "Platformer",
    "puzzle": "Puzzle",
    "racing": "Racing",
    "rhythm": "Rhythm",
    "role playing": "RPG",
    "role-playing": "RPG",
    "role playing game": "RPG",
    "rpg": "RPG",
    "sandbox": "Sandbox",
    "shooter": "Shooter",
    "simulation": "Simulation",
    "simulator": "Simulation",
    "sports": "Sports",
    "strategy": "Strategy",
    "survival": "Survival",
    "visual novel": "Visual Novel",
}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _playtime(game: Mapping[str, Any]) -> float:
    value = _finite_number(game.get("playtime_minutes"))
    return max(0.0, value) if value is not None else 0.0


def _coverage(titles: float, playtime: float | None = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"titles": round(clamp(float(titles)), 6)}
    if playtime is not None:
        result["playtime"] = round(clamp(float(playtime)), 6)
    result.update(extra)
    return result


def _coverage_score(coverage: Mapping[str, Any]) -> float:
    # Achievement support is an eligibility condition, not missing data.  A
    # title that exposes no achievements must not lower confidence in the
    # supported-title corpus, so those signals use the explicit eligible-game
    # coverage and any relevant item-level coverage.
    keys = (
        {"achievements", "timestamps", "rarity", "genres"}
        if "achievements" in coverage
        else {"titles", "playtime"}
    )
    numeric = [
        float(value)
        for key, value in coverage.items()
        if key in keys and isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return min(numeric) if numeric else 0.0


def _confidence(coverage: Mapping[str, Any], value: Any) -> str:
    if value is None:
        return "low"
    score = _coverage_score(coverage)
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


def _signal(
    value: Any,
    coverage: Mapping[str, Any],
    *,
    definition: str | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "value": value,
        "coverage": dict(coverage),
        "source": SOURCE,
        "confidence": _confidence(coverage, value),
    }
    if definition:
        result["definition"] = definition
    if unit:
        result["unit"] = unit
    return result


def _distribution(weights: Mapping[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(value)) for value in weights.values())
    if total <= 0:
        return {}
    return {
        key: round(max(0.0, float(value)) / total, 12)
        for key, value in sorted(weights.items())
        if value > 0
    }


def _entropy(distribution: Mapping[str, float] | Sequence[float]) -> float:
    values = distribution.values() if isinstance(distribution, Mapping) else distribution
    return -sum(value * math.log(value) for value in values if value > 0)


def _normalized_entropy(distribution: Mapping[str, float]) -> float:
    if len(distribution) <= 1:
        return 0.0
    return _entropy(distribution) / math.log(len(distribution))


def _gini(values: Sequence[float]) -> float:
    clean = sorted(max(0.0, float(value)) for value in values)
    total = sum(clean)
    size = len(clean)
    if size == 0 or total <= 0:
        return 0.0
    numerator = sum((2 * index - size - 1) * value for index, value in enumerate(clean, 1))
    return clamp(numerator / (size * total))


def _canonical_text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("description") or value.get("name") or value.get("value")
    if not isinstance(value, str):
        return None
    clean = re.sub(r"\s+", " ", value.strip())
    return clean or None


def _normalize_genres(raw_genres: Any) -> tuple[list[str], list[str]]:
    if isinstance(raw_genres, (str, Mapping)):
        raw_genres = [raw_genres]
    if not isinstance(raw_genres, Sequence):
        return [], []
    gameplay: set[str] = set()
    attributes: set[str] = set()
    for raw in raw_genres:
        text = _canonical_text(raw)
        if not text:
            continue
        key = re.sub(r"[_/]+", " ", text.casefold())
        key = re.sub(r"\s+", " ", key).strip()
        if key in _ATTRIBUTE_ALIASES:
            attributes.add(_ATTRIBUTE_ALIASES[key])
            continue
        gameplay.add(_GENRE_ALIASES.get(key, text if text.isupper() else text.title()))
    return sorted(gameplay), sorted(attributes)


def _release_year(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1900 <= value <= 2200:
        return value
    if isinstance(value, datetime):
        return value.year
    if isinstance(value, Mapping):
        value = value.get("date") or value.get("year")
    if not isinstance(value, str):
        return None
    match = re.search(r"(?<!\d)(19\d{2}|20\d{2}|21\d{2})(?!\d)", value)
    return int(match.group(1)) if match else None


def _game_release_year(game: Mapping[str, Any]) -> int | None:
    metadata = game.get("metadata") if isinstance(game.get("metadata"), Mapping) else {}
    return _release_year(metadata.get("release_year")) or _release_year(metadata.get("release_date"))


def _profile_year(profile: Mapping[str, Any], known_years: Sequence[int], config: Mapping[str, Any]) -> int:
    configured = _release_year(config.get("analysis_year"))
    if configured:
        return configured
    generated = _release_year(profile.get("generated_at"))
    if generated:
        return generated
    return max(known_years) if known_years else 1970


def _era(year: int) -> str:
    if year < 2000:
        return "Before 2000"
    decade = (year // 10) * 10
    return f"{decade}s"


def _weighted_median(items: Sequence[tuple[int, float]]) -> float | None:
    positive = sorted((value, weight) for value, weight in items if weight > 0)
    total = sum(weight for _, weight in positive)
    if total <= 0:
        return None
    midpoint = total / 2
    cumulative = 0.0
    for value, weight in positive:
        cumulative += weight
        if cumulative >= midpoint:
            return float(value)
    return float(positive[-1][0])


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _achievement_key(appid: str, item: Mapping[str, Any], index: int) -> str:
    api_name = item.get("api_name")
    if isinstance(api_name, str) and api_name.strip():
        return api_name.strip()
    return f"unnamed-{appid}-{index:04d}"


def _achievement_ref(appid: str, item: Mapping[str, Any], index: int) -> dict[str, Any]:
    percent = _finite_number(item.get("global_percent"))
    percent = clamp(percent, 0.0, 100.0) if percent is not None else None
    return {
        "appid": appid,
        "api_name": _achievement_key(appid, item, index),
        "name": _canonical_text(item.get("name")) or _achievement_key(appid, item, index),
        "global_percent": round(percent, 6) if percent is not None else None,
        "hidden": bool(item.get("hidden", False)),
    }


def _timeline_for_game(
    appid: str,
    name: str,
    indexed_items: Sequence[tuple[int, Mapping[str, Any]]],
    config: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, int, int]:
    achieved_total = 0
    timestamps: list[float] = []
    for _, item in indexed_items:
        if bool(item.get("achieved")):
            achieved_total += 1
            timestamp = parse_timestamp(item.get("unlock_time"))
            if timestamp is not None:
                timestamps.append(timestamp)
    if not timestamps:
        return None, achieved_total, 0
    timestamps.sort()
    first, last = timestamps[0], timestamps[-1]
    gaps = [(right - left) / 86400 for left, right in zip(timestamps, timestamps[1:])]
    comeback_threshold = max(0.0, float(config["comeback_gap_days"]))
    comeback_gaps = [gap for gap in gaps if gap >= comeback_threshold]

    window_seconds = max(0.0, float(config["burst_window_hours"])) * 3600
    max_in_window = 0
    left = 0
    for right, timestamp in enumerate(timestamps):
        while left <= right and timestamp - timestamps[left] > window_seconds:
            left += 1
        max_in_window = max(max_in_window, right - left + 1)

    minimum = max(2, int(config["burst_min_unlocks"]))
    burst_starts: list[float] = []
    cursor = 0
    while cursor < len(timestamps):
        end = cursor
        while end + 1 < len(timestamps) and timestamps[end + 1] - timestamps[cursor] <= window_seconds:
            end += 1
        if end - cursor + 1 >= minimum:
            burst_starts.append(timestamps[cursor])
            cursor = end + 1
        else:
            cursor += 1

    return {
        "appid": appid,
        "name": name,
        "first_achievement_at": _iso(first),
        "last_achievement_at": _iso(last),
        "achievement_span_days": round((last - first) / 86400, 6),
        "largest_gap_days": round(max(gaps), 6) if gaps else 0.0,
        "comeback_count": len(comeback_gaps),
        "activity_year_count": len({datetime.fromtimestamp(ts, tz=timezone.utc).year for ts in timestamps}),
        "max_unlocks_in_24h": max_in_window,
        "burst_dates": [_iso(ts) for ts in burst_starts],
        "burst_count": len(burst_starts),
        "timestamped_unlocks": len(timestamps),
        "unlocks": achieved_total,
    }, achieved_total, len(timestamps)


def _fact(name: str, value: Any, **context: Any) -> dict[str, Any]:
    result = {"name": name, "value": value}
    result.update(context)
    return result


def derive_signals(profile: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Derive deterministic, provenance-carrying signals from ``profile``.

    Invalid numeric values are treated as unavailable, never as negative
    activity.  Input order does not affect rankings or output order.
    """

    if not isinstance(profile, Mapping):
        raise TypeError("profile must be a mapping")
    options = dict(DEFAULT_CONFIG)
    if config:
        options.update(config)
    raw_games = profile.get("games", [])
    games = [game for game in raw_games if isinstance(game, Mapping)] if isinstance(raw_games, Sequence) else []
    games.sort(key=lambda game: (str(game.get("appid", "")), str(game.get("name", ""))))
    owned = len(games)
    playtimes = [_playtime(game) for game in games]
    total_playtime = sum(playtimes)
    played = sum(value > 0 for value in playtimes)
    meaningful_threshold = max(0.0, float(options["meaningful_playtime_minutes"]))
    meaningful = sum(value >= meaningful_threshold for value in playtimes)
    # An explicitly empty normalized list is complete information, not missing
    # information.  Metadata-specific sections compute their own coverage.
    full_coverage = _coverage(1.0, 1.0)
    series_groups = discover_series_groups(profile)
    series_game_ids = {
        str(row.get("game_id"))
        for group in series_groups
        for row in group.get("games", [])
        if isinstance(row, Mapping) and row.get("game_id")
    }
    series_playtime = sum(
        float(row.get("playtime_minutes", 0.0))
        for group in series_groups
        for row in group.get("games", [])
        if isinstance(row, Mapping)
    )
    series_coverage = _coverage(
        len(series_game_ids) / played if played else 1.0,
        series_playtime / total_playtime if total_playtime else 1.0,
    )

    library = {
        "owned_count": _signal(owned, full_coverage, definition="Titles present in the normalized profile", unit="titles"),
        "played_count": _signal(played, full_coverage, definition="Titles with playtime_minutes > 0", unit="titles"),
        "unplayed_count": _signal(owned - played, full_coverage, definition="Titles with playtime_minutes = 0", unit="titles"),
        "meaningfully_played_count": _signal(
            meaningful,
            full_coverage,
            definition=f"Titles with playtime_minutes >= {meaningful_threshold:g}",
            unit="titles",
        ),
        "total_playtime_minutes": _signal(round(total_playtime, 6), full_coverage, unit="minutes"),
        "engagement_ratio": _signal(played / owned if owned else 0.0, full_coverage, definition="played_count / owned_count"),
        "meaningful_engagement_ratio": _signal(
            meaningful / owned if owned else 0.0,
            full_coverage,
            definition="meaningfully_played_count / owned_count",
        ),
    }
    ranked_playtime = sorted(
        ((str(game.get("appid", "")), str(game.get("name", "")), value) for game, value in zip(games, playtimes)),
        key=lambda item: (-item[2], item[0], item[1]),
    )
    shares = [value / total_playtime for _, _, value in ranked_playtime] if total_playtime > 0 else []
    top_share = lambda count: sum(shares[:count]) if shares else 0.0
    shannon = _entropy(shares)
    playtime = {
        "top1_share": _signal(top_share(1), full_coverage),
        "top3_share": _signal(top_share(3), full_coverage),
        "top5_share": _signal(top_share(5), full_coverage),
        "top10_share": _signal(top_share(10), full_coverage),
        "hhi": _signal(sum(share * share for share in shares), full_coverage, definition="sum of squared title playtime shares"),
        "gini": _signal(_gini(playtimes), full_coverage, definition="Gini coefficient across all owned titles"),
        "shannon_entropy": _signal(shannon, full_coverage, definition="-sum(p * ln(p))"),
        "effective_games": _signal(math.exp(shannon) if total_playtime > 0 else 0.0, full_coverage, definition="exp(shannon_entropy)"),
        "ranked_games": _signal(
            [
                {
                    "appid": appid,
                    "name": name,
                    "playtime_minutes": round(value, 6),
                    "share": round(value / total_playtime, 12) if total_playtime else 0.0,
                }
                for appid, name, value in ranked_playtime
                if value > 0
            ],
            full_coverage,
        ),
    }

    played_title_genre_weights: Counter[str] = Counter()
    played_genre_weights: Counter[str] = Counter()
    attribute_titles: Counter[str] = Counter()
    attribute_playtime: Counter[str] = Counter()
    genre_known_titles = 0
    genre_known_playtime = 0.0
    normalized_by_app: dict[str, dict[str, list[str]]] = {}
    for game, minutes in zip(games, playtimes):
        if minutes <= 0:
            continue
        appid = str(game.get("appid", ""))
        metadata = game.get("metadata") if isinstance(game.get("metadata"), Mapping) else {}
        gameplay_genres, attributes = _normalize_genres(metadata.get("genres", []))
        normalized_by_app[appid] = {"gameplay_genres": gameplay_genres, "attributes": attributes}
        if gameplay_genres:
            genre_known_titles += 1
            genre_known_playtime += minutes
            contribution = 1.0 / len(gameplay_genres)
            for genre in gameplay_genres:
                played_title_genre_weights[genre] += contribution
                played_genre_weights[genre] += contribution * minutes
        if gameplay_genres or attributes:
            for attribute in attributes:
                attribute_titles[attribute] += 1
                attribute_playtime[attribute] += minutes

    genre_title_coverage = genre_known_titles / played if played else 0.0
    genre_playtime_coverage = genre_known_playtime / total_playtime if total_playtime else 0.0
    genre_coverage = _coverage(genre_title_coverage, genre_playtime_coverage)
    played_title_genre_distribution = _distribution(played_title_genre_weights)
    played_genre_distribution = _distribution(played_genre_weights)
    played_title_genre_entropy = _entropy(played_title_genre_distribution)
    played_genre_entropy = _entropy(played_genre_distribution)
    dominant = sorted(played_genre_distribution, key=lambda key: (-played_genre_distribution[key], key))[:5]
    attribute_title_denominator = max(genre_known_titles, 1)
    attribute_playtime_denominator = genre_known_playtime
    genres = {
        "normalized_by_game": _signal(normalized_by_app, genre_coverage),
        "played_title_distribution": _signal(played_title_genre_distribution, genre_coverage),
        "playtime_distribution": _signal(played_genre_distribution, genre_coverage),
        "played_title_entropy": _signal(played_title_genre_entropy, genre_coverage),
        "playtime_entropy": _signal(played_genre_entropy, genre_coverage),
        "played_title_normalized_entropy": _signal(_normalized_entropy(played_title_genre_distribution), genre_coverage),
        "playtime_normalized_entropy": _signal(_normalized_entropy(played_genre_distribution), genre_coverage),
        "entropy_gap": _signal(played_title_genre_entropy - played_genre_entropy, genre_coverage, definition="played_title_entropy - playtime_entropy"),
        "dominant_genres": _signal(
            [{"genre": genre, "share": played_genre_distribution[genre]} for genre in dominant],
            genre_coverage,
        ),
        "concentration": _signal(max(played_genre_distribution.values(), default=0.0), genre_coverage, definition="largest playtime-weighted genre share"),
        "played_title_hhi": _signal(sum(value * value for value in played_title_genre_distribution.values()), genre_coverage),
        "playtime_hhi": _signal(sum(value * value for value in played_genre_distribution.values()), genre_coverage),
        "indie_share": _signal(attribute_titles["Indie"] / attribute_title_denominator if genre_known_titles else 0.0, genre_coverage),
        "free_to_play_share": _signal(attribute_titles["Free to Play"] / attribute_title_denominator if genre_known_titles else 0.0, genre_coverage),
        "early_access_share": _signal(attribute_titles["Early Access"] / attribute_title_denominator if genre_known_titles else 0.0, genre_coverage),
        "attribute_title_shares": _signal(
            {key: round(value / attribute_title_denominator, 12) for key, value in sorted(attribute_titles.items())}
            if genre_known_titles
            else {},
            genre_coverage,
        ),
        "attribute_playtime_shares": _signal(
            {key: round(value / attribute_playtime_denominator, 12) for key, value in sorted(attribute_playtime.items())}
            if attribute_playtime_denominator > 0
            else {},
            genre_coverage,
        ),
    }
    years: list[tuple[int, float]] = []
    for game, minutes in zip(games, playtimes):
        if minutes <= 0:
            continue
        metadata = game.get("metadata") if isinstance(game.get("metadata"), Mapping) else {}
        year = _release_year(metadata.get("release_year")) or _release_year(metadata.get("release_date"))
        if year is not None:
            years.append((year, minutes))
    known_years = [year for year, _ in years]
    known_release_playtime = sum(minutes for _, minutes in years)
    release_coverage = _coverage(
        len(years) / played if played else 0.0,
        known_release_playtime / total_playtime if total_playtime else 0.0,
    )
    played_title_release_dist = _distribution(Counter(_era(year) for year, _ in years))
    playtime_release_counter: Counter[str] = Counter()
    for year, minutes in years:
        playtime_release_counter[_era(year)] += minutes
    playtime_release_dist = _distribution(playtime_release_counter)
    weighted_mean_year = (
        sum(year * minutes for year, minutes in years) / known_release_playtime
        if known_release_playtime > 0
        else None
    )
    weighted_median_year = _weighted_median(years)
    weighted_variance = (
        sum(minutes * ((year - weighted_mean_year) ** 2) for year, minutes in years) / known_release_playtime
        if known_release_playtime > 0 and weighted_mean_year is not None
        else None
    )
    analysis_year = _profile_year(profile, known_years, options)
    recent_cutoff = analysis_year - max(1, int(options["recent_years"])) + 1
    old_cutoff = analysis_year - max(0, int(options["old_game_age_years"]))
    old_playtime = sum(minutes for year, minutes in years if year <= old_cutoff)
    recent_playtime = sum(minutes for year, minutes in years if year >= recent_cutoff)
    release_era = {
        "played_title_distribution": _signal(played_title_release_dist, release_coverage),
        "playtime_weighted_distribution": _signal(playtime_release_dist, release_coverage),
        "played_title_mean_release_year": _signal(fmean(known_years) if known_years else None, release_coverage, unit="year"),
        "weighted_mean_release_year": _signal(weighted_mean_year, release_coverage, unit="year"),
        "weighted_median_release_year": _signal(weighted_median_year, release_coverage, unit="year"),
        "release_year_stddev": _signal(math.sqrt(weighted_variance) if weighted_variance is not None else None, release_coverage, unit="years"),
        "old_game_share": _signal(old_playtime / known_release_playtime if known_release_playtime else None, release_coverage, definition=f"playtime share released <= {old_cutoff}"),
        "recent_game_share": _signal(recent_playtime / known_release_playtime if known_release_playtime else None, release_coverage, definition=f"playtime share released >= {recent_cutoff}"),
        "played_title_old_game_share": _signal(sum(year <= old_cutoff for year in known_years) / len(known_years) if known_years else None, release_coverage),
        "played_title_recent_game_share": _signal(sum(year >= recent_cutoff for year in known_years) / len(known_years) if known_years else None, release_coverage),
        "era_span": _signal(max(known_years) - min(known_years) if known_years else None, release_coverage, unit="years"),
        "analysis_year": _signal(analysis_year, release_coverage, unit="year"),
    }
    completion_rows: list[dict[str, Any]] = []
    unlocked_with_rarity: list[dict[str, Any]] = []
    missed_with_rarity: list[dict[str, Any]] = []
    inversions: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    played_games_with_achievements = 0
    played_games_with_player_data = 0
    achieved_total = 0
    timestamped_total = 0
    rarity_relevant_total = 0
    rarity_known_total = 0
    for game, minutes in zip(games, playtimes):
        appid = str(game.get("appid", ""))
        name = str(game.get("name") or appid)
        achievement_block = game.get("achievements") if isinstance(game.get("achievements"), Mapping) else {}
        raw_items = achievement_block.get("items", [])
        items = [item for item in raw_items if isinstance(item, Mapping)] if isinstance(raw_items, Sequence) else []
        indexed_items = list(enumerate(items))
        if items and minutes > 0:
            played_games_with_achievements += 1
        status = str(achievement_block.get("status", "")).casefold()
        data_available = bool(items) and status not in {"unavailable", "error", "private", "missing"}
        if data_available and minutes > 0:
            played_games_with_player_data += 1
        if items and data_available and minutes > 0:
            unlocked_count = sum(bool(item.get("achieved")) for item in items)
            completion_rows.append({
                "appid": appid,
                "name": name,
                "unlocked": unlocked_count,
                "total": len(items),
                "completion": unlocked_count / len(items),
            })
            unlocked_candidates: list[tuple[float, dict[str, Any]]] = []
            missed_candidates: list[tuple[float, dict[str, Any]]] = []
            for index, item in indexed_items:
                percent = _finite_number(item.get("global_percent"))
                achieved = bool(item.get("achieved"))
                rarity_relevant_total += 1
                if percent is None:
                    continue
                rarity_known_total += 1
                percent = clamp(percent, 0.0, 100.0)
                ref = _achievement_ref(appid, item, index)
                probability = clamp(percent / 100.0, float(options["rarity_probability_floor"]), 1.0)
                if achieved:
                    score = -math.log(probability)
                    row = {**ref, "surprise": score, "rarity_weight": score, "unlock_time": item.get("unlock_time")}
                    unlocked_with_rarity.append(row)
                    unlocked_candidates.append((percent, row))
                else:
                    score = -math.log(max(float(options["rarity_probability_floor"]), 1.0 - percent / 100.0))
                    row = {**ref, "surprise": score}
                    missed_with_rarity.append(row)
                    missed_candidates.append((percent, row))
            if unlocked_candidates and missed_candidates:
                rarest = min(unlocked_candidates, key=lambda pair: (pair[0], pair[1]["api_name"]))[1]
                easiest_missing = max(missed_candidates, key=lambda pair: (pair[0], pair[1]["api_name"]))[1]
                if rarest["global_percent"] < easiest_missing["global_percent"]:
                    inversions.append({
                        "appid": appid,
                        "name": name,
                        "rarest_unlocked": rarest,
                        "easiest_missing": easiest_missing,
                        "gap_percentage_points": round(easiest_missing["global_percent"] - rarest["global_percent"], 6),
                        "semantic_status": "unchecked",
                    })
            timeline, game_achieved, game_timestamped = _timeline_for_game(appid, name, indexed_items, options)
            achieved_total += game_achieved
            timestamped_total += game_timestamped
            if timeline:
                timelines.append(timeline)

    played_denominator = played if played else 0
    timestamp_coverage = timestamped_total / achieved_total if achieved_total else 0.0
    rarity_coverage = rarity_known_total / rarity_relevant_total if rarity_relevant_total else 0.0
    completion_coverage = _coverage(
        played_games_with_player_data / played_denominator if played_denominator else 0.0,
        None,
        achievements=played_games_with_player_data / played_games_with_achievements if played_games_with_achievements else 0.0,
        played_games=played,
        games_with_achievements=played_games_with_achievements,
        games_with_player_achievement_data=played_games_with_player_data,
    )
    rarity_signal_coverage = dict(completion_coverage)
    rarity_signal_coverage["rarity"] = round(rarity_coverage, 6)
    timeline_signal_coverage = dict(completion_coverage)
    timeline_signal_coverage["timestamps"] = round(timestamp_coverage, 6)
    achievement_coverage = dict(completion_coverage)
    achievement_coverage.update(
        rarity=round(rarity_coverage, 6),
        timestamps=timestamp_coverage,
    )
    completion_values = [row["completion"] for row in completion_rows]
    unlocked_with_rarity.sort(key=lambda row: (-row["surprise"], row["appid"], row["api_name"]))
    missed_with_rarity.sort(key=lambda row: (-row["surprise"], row["appid"], row["api_name"]))
    inversions.sort(key=lambda row: (-row["gap_percentage_points"], row["appid"]))
    timelines.sort(key=lambda row: row["appid"])
    longest_lived = max(timelines, key=lambda row: (row["achievement_span_days"], row["appid"]), default=None)
    limit = max(0, int(options["surprise_limit"]))
    rare_count = sum(row["global_percent"] < 5 for row in unlocked_with_rarity)
    unlocked_percents = [
        float(row["global_percent"])
        for row in unlocked_with_rarity
        if _finite_number(row.get("global_percent")) is not None
    ]
    mean_unlocked_percent = fmean(unlocked_percents) if unlocked_percents else None
    anti_mainstream_val = (
        round(clamp((50.0 - mean_unlocked_percent) / 45.0) * 100, 1)
        if mean_unlocked_percent is not None
        else None
    )

    unlock_dates_counter: Counter[str] = Counter()
    unlock_dates_games: dict[str, set[str]] = {}
    for game in games:
        appid = str(game.get("appid", ""))
        block = game.get("achievements") if isinstance(game.get("achievements"), Mapping) else {}
        items = block.get("items", []) if isinstance(block, Mapping) else []
        for item in items:
            if isinstance(item, Mapping) and item.get("achieved"):
                ts = _finite_number(item.get("unlock_time"))
                if ts and ts > 0:
                    dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    unlock_dates_counter[dt_str] += 1
                    unlock_dates_games.setdefault(dt_str, set()).add(appid)

    peak_burst_info = None
    if unlock_dates_counter:
        peak_date, peak_count = unlock_dates_counter.most_common(1)[0]
        peak_games = sorted(unlock_dates_games.get(peak_date, set()))
        peak_burst_info = {
            "date": peak_date,
            "unlock_count": peak_count,
            "game_count": len(peak_games),
            "appids": peak_games,
        }

    achievements = {
        "coverage": _signal(
            {
                "played_games": played,
                "games_with_achievements": played_games_with_achievements,
                "games_with_player_achievement_data": played_games_with_player_data,
                "timestamped_unlocks": timestamped_total,
                "unlocks": achieved_total,
            },
            achievement_coverage,
        ),
        "completion_by_game": _signal(completion_rows, completion_coverage),
        "perfected_games": _signal(sum(value == 1.0 for value in completion_values), completion_coverage, unit="games"),
        "completion_80_plus_games": _signal(sum(0.8 <= value < 1.0 for value in completion_values), completion_coverage, unit="games"),
        "completion_20_to_80_games": _signal(sum(0.2 <= value < 0.8 for value in completion_values), completion_coverage, unit="games"),
        "completion_below_20_games": _signal(sum(value < 0.2 for value in completion_values), completion_coverage, unit="games"),
        "completion_mean": _signal(fmean(completion_values) if completion_values else None, completion_coverage),
        "completion_median": _signal(median(completion_values) if completion_values else None, completion_coverage),
        "completion_variance": _signal(pvariance(completion_values) if completion_values else None, completion_coverage),
        "rarest_unlock": _signal(unlocked_with_rarity[0] if unlocked_with_rarity else None, rarity_signal_coverage),
        "ultra_rare_count": _signal(sum(row["global_percent"] < 1 for row in unlocked_with_rarity), rarity_signal_coverage, unit="achievements"),
        "rare_count": _signal(rare_count, rarity_signal_coverage, definition="Unlocked achievements with global_percent < 5", unit="achievements"),
        "anti_mainstream_score": _signal(anti_mainstream_val, rarity_signal_coverage, definition="Calculated divergence of unlocked achievements from global baseline", unit="score"),
        "peak_daily_burst": _signal(peak_burst_info, timeline_signal_coverage, definition="Highest single-day achievement unlock count recorded"),
        "top_surprising_unlocks": _signal(unlocked_with_rarity[:limit], rarity_signal_coverage),
        "top_surprising_misses": _signal(missed_with_rarity[:limit], rarity_signal_coverage),
        "inversion_candidates": _signal(inversions, rarity_signal_coverage, definition="Statistical candidates; semantic relationship unchecked"),
        "timelines": _signal(timelines, timeline_signal_coverage, definition="Observable achievement activity only"),
        "longest_observable_achievement_span": _signal(longest_lived, timeline_signal_coverage, definition="Observable achievement activity span, not gameplay span"),
        "comeback_games": _signal(
            sorted((row for row in timelines if row["comeback_count"] > 0), key=lambda row: (-row["largest_gap_days"], row["appid"])),
            timeline_signal_coverage,
            definition=f"Adjacent timestamped unlocks separated by at least {options['comeback_gap_days']} days",
        ),
        "burst_games": _signal(
            sorted((row for row in timelines if row["burst_count"] > 0), key=lambda row: (-row["max_unlocks_in_24h"], row["appid"])),
            timeline_signal_coverage,
            definition=f"At least {options['burst_min_unlocks']} unlocks inside {options['burst_window_hours']} hours",
        ),
    }
    cross_game_patterns = discover_cross_game_patterns(
        profile,
        {"achievements": achievements, "thresholds": options},
    )
    cross_game_coverage = (
        dict(cross_game_patterns[0].get("coverage", {}))
        if cross_game_patterns
        else _coverage(0.0, 0.0)
    )
    tensions: list[dict[str, Any]] = []
    for group in series_groups:
        group_key = str(group.get("series_key") or "series")
        related_ids = [
            str(row.get("game_id"))
            for row in group.get("games", [])
            if isinstance(row, Mapping) and row.get("game_id")
        ]
        tensions.append({
            "id": f"pattern:same-series-group:{group_key}",
            "type": "same_series_group",
            "facts": [
                _fact("series_key", group_key),
                _fact("series_prefix", group.get("series_prefix")),
                _fact("game_count", group.get("game_count")),
                _fact("candidate_pool_size", group.get("candidate_pool_size")),
                _fact("growth_arc", group.get("growth_arc", "steady_progression")),
                _fact("game_rows", group.get("games", [])),
                _fact("scope", group.get("scope")),
            ],
            "related_ids": related_ids,
            "strength": group.get("strength", 0.0),
            "coverage": series_coverage,
            "source": SOURCE,
            "confidence": _confidence(series_coverage, True),
        })
    for pattern in cross_game_patterns:
        pattern_key = str(pattern.get("pattern_key") or "cross-game-pattern")
        related_ids = [
            str(row.get("game_id"))
            for row in pattern.get("games", [])
            if isinstance(row, Mapping) and row.get("game_id")
        ]
        tensions.append({
            "id": f"pattern:cross-game-atlas:{pattern_key}",
            "type": "cross_game_pattern_atlas",
            "facts": [
                _fact("pattern_key", pattern_key),
                _fact("game_count", pattern.get("game_count")),
                _fact("game_rows", pattern.get("games", [])),
                _fact("thresholds", pattern.get("thresholds", {})),
                _fact("scope", pattern.get("scope")),
            ],
            "related_ids": related_ids,
            "strength": pattern.get("strength", 0.0),
            "coverage": pattern.get("coverage", {}),
            "source": SOURCE,
            "confidence": _confidence(pattern.get("coverage", {}), True),
        })
    genre_title_norm = genres["played_title_normalized_entropy"]["value"]
    genre_play_norm = genres["playtime_normalized_entropy"]["value"]
    if (
        genre_title_norm >= 0.75
        and genre_play_norm <= 0.55
        and genre_title_norm - genre_play_norm >= 0.20
        and _coverage_score(genre_coverage) >= 0.5
    ):
        tensions.append({
            "id": "pattern:genre-played-title-playtime-gap",
            "type": "played_title_genre_distribution_gap",
            "facts": [_fact("played_title_normalized_entropy", genre_title_norm), _fact("playtime_normalized_entropy", genre_play_norm)],
            "strength": round(clamp(genre_title_norm - genre_play_norm), 6),
            "coverage": genre_coverage,
            "source": SOURCE,
            "confidence": _confidence(genre_coverage, True),
        })
    engagement = library["engagement_ratio"]["value"]
    if owned >= int(options["tension_owned_minimum"]) and engagement <= float(options["tension_low_engagement"]):
        strength = clamp((1 - engagement) * min(1.0, owned / 100))
        tensions.append({
            "id": "pattern:library-engagement-gap",
            "type": "library_engagement_gap",
            "facts": [_fact("owned_count", owned), _fact("engagement_ratio", engagement)],
            "strength": round(strength, 6),
            "coverage": full_coverage,
            "source": SOURCE,
            "confidence": _confidence(full_coverage, True),
        })

    effective_games = float(playtime["effective_games"]["value"])
    top10 = float(playtime["top10_share"]["value"])
    gini = float(playtime["gini"]["value"])
    if effective_games >= 25 and top10 <= 0.50 and gini >= 0.50:
        breadth_strength = (
            clamp(effective_games / 100)
            + clamp(1 - top10)
            + clamp(gini)
        ) / 3
        tensions.append({
            "id": "pattern:library-attention-breadth-contrast",
            "type": "library_attention_breadth_contrast",
            "facts": [
                _fact("effective_games", effective_games),
                _fact("top10_share", top10),
                _fact("gini", gini),
                _fact("owned_count", owned),
                _fact("played_count", played),
                _fact("scope", "library-wide recorded playtime"),
            ],
            "strength": round(breadth_strength, 6),
            "coverage": full_coverage,
            "source": SOURCE,
            "confidence": _confidence(full_coverage, True),
        })

    completion_low = int(achievements["completion_below_20_games"]["value"])
    completion_mid = int(achievements["completion_20_to_80_games"]["value"])
    completion_high = int(achievements["completion_80_plus_games"]["value"])
    perfected = int(achievements["perfected_games"]["value"])
    completion_high_total = completion_high + perfected
    completion_total = completion_low + completion_mid + completion_high_total
    if (
        _coverage_score(completion_coverage) >= 0.5
        and completion_low >= 5
        and completion_high_total >= 2
    ):
        polarity_strength = (
            clamp(completion_low / 10) * 0.6
            + clamp(completion_high_total / 3) * 0.4
        )
        tensions.append({
            "id": "pattern:selective-completion-contrast",
            "type": "selective_completion_contrast",
            "facts": [
                _fact("games_with_player_achievement_data", completion_total),
                _fact("completion_below_20_games", completion_low),
                _fact("completion_20_to_80_games", completion_mid),
                _fact("completion_80_plus_games", completion_high),
                _fact("perfected_games", perfected),
                _fact("scope", "games with available player achievement data"),
            ],
            "strength": round(polarity_strength, 6),
            "coverage": completion_coverage,
            "source": SOURCE,
            "confidence": _confidence(completion_coverage, True),
        })

    completion_mean = achievements["completion_mean"]["value"]
    top1 = playtime["top1_share"]["value"]
    if completion_mean is not None and completion_mean <= float(options["tension_low_completion"]) and top1 >= float(options["tension_high_top1_share"]):
        combined_coverage = dict(completion_coverage)
        combined_coverage["playtime"] = full_coverage["playtime"]
        tensions.append({
            "id": "pattern:completion-playtime-depth-gap",
            "type": "completion_playtime_depth_gap",
            "facts": [_fact("completion_mean", completion_mean), _fact("top1_share", top1)],
            "strength": round(clamp((1 - completion_mean) * top1), 6),
            "coverage": combined_coverage,
            "source": SOURCE,
            "confidence": _confidence(combined_coverage, True),
        })
    played_title_mean_year = release_era["played_title_mean_release_year"]["value"]
    if weighted_mean_year is not None and played_title_mean_year is not None:
        year_gap = played_title_mean_year - weighted_mean_year
        if year_gap >= float(options["tension_release_year_gap"]):
            tensions.append({
                "id": "pattern:played-title-playtime-release-gap",
                "type": "played_title_playtime_release_gap",
                "facts": [_fact("played_title_mean_release_year", played_title_mean_year), _fact("weighted_mean_release_year", weighted_mean_year), _fact("year_gap", year_gap)],
                "strength": round(clamp(year_gap / 15), 6),
                "coverage": release_coverage,
                "source": SOURCE,
                "confidence": _confidence(release_coverage, True),
            })

    all_activity_games_by_era: dict[int, set[str]] = {}
    genre_activity_games_by_era: dict[int, set[str]] = {}
    activity_genres_by_era: dict[int, Counter[str]] = {}
    for game, minutes in zip(games, playtimes):
        if minutes <= 0:
            continue
        appid = str(game.get("appid", ""))
        achievement_block = game.get("achievements") if isinstance(game.get("achievements"), Mapping) else {}
        raw_items = achievement_block.get("items", [])
        items = [item for item in raw_items if isinstance(item, Mapping)] if isinstance(raw_items, Sequence) else []
        active_eras = {
            datetime.fromtimestamp(timestamp, tz=timezone.utc).year // 5 * 5
            for item in items
            if bool(item.get("achieved"))
            for timestamp in [_finite_number(item.get("unlock_time"))]
            if timestamp is not None and timestamp > 0
        }
        if not active_eras:
            continue
        genres_for_game = normalized_by_app.get(appid, {}).get("gameplay_genres", [])
        for era in active_eras:
            all_activity_games_by_era.setdefault(era, set()).add(appid)
            if not genres_for_game:
                continue
            genre_activity_games_by_era.setdefault(era, set()).add(appid)
            counter = activity_genres_by_era.setdefault(era, Counter())
            weight = 1 / len(genres_for_game)
            for genre in genres_for_game:
                counter[genre] += weight

    eligible_activity_eras = sorted(
        era
        for era, appids in genre_activity_games_by_era.items()
        if len(appids) >= 5
    )
    if len(eligible_activity_eras) >= 2:
        earlier_era = eligible_activity_eras[0]
        later_era = eligible_activity_eras[-1]
        earlier_counts = activity_genres_by_era[earlier_era]
        later_counts = activity_genres_by_era[later_era]
        earlier_total = sum(earlier_counts.values())
        later_total = sum(later_counts.values())
        earlier_shares = {
            genre: value / earlier_total for genre, value in earlier_counts.items()
        }
        later_shares = {
            genre: value / later_total for genre, value in later_counts.items()
        }
        genre_names = sorted(set(earlier_shares) | set(later_shares))
        changes = {
            genre: later_shares.get(genre, 0.0) - earlier_shares.get(genre, 0.0)
            for genre in genre_names
        }
        variation = sum(abs(value) for value in changes.values()) / 2
        metadata_games = (
            len(genre_activity_games_by_era[earlier_era])
            + len(genre_activity_games_by_era[later_era])
        )
        all_games_in_eras = (
            len(all_activity_games_by_era[earlier_era])
            + len(all_activity_games_by_era[later_era])
        )
        genre_metadata_coverage = metadata_games / all_games_in_eras if all_games_in_eras else 0.0
        if variation >= 0.20 and genre_metadata_coverage >= 0.5:
            increased = max(genre_names, key=lambda genre: (changes[genre], genre))
            decreased = min(genre_names, key=lambda genre: (changes[genre], genre))
            shift_coverage = dict(timeline_signal_coverage)
            shift_coverage["genres"] = round(genre_metadata_coverage, 6)
            tensions.append({
                "id": f"pattern:achievement-activity-genre-shift:{earlier_era}:{later_era}",
                "type": "achievement_activity_genre_shift",
                "facts": [
                    _fact("earlier_era", f"{earlier_era}\u2013{earlier_era + 4}"),
                    _fact("later_era", f"{later_era}\u2013{later_era + 4}"),
                    _fact("earlier_active_games", len(genre_activity_games_by_era[earlier_era])),
                    _fact("later_active_games", len(genre_activity_games_by_era[later_era])),
                    _fact("most_decreased_genre", decreased),
                    _fact("earlier_decreased_genre_share", earlier_shares.get(decreased, 0.0)),
                    _fact("later_decreased_genre_share", later_shares.get(decreased, 0.0)),
                    _fact("most_increased_genre", increased),
                    _fact("earlier_increased_genre_share", earlier_shares.get(increased, 0.0)),
                    _fact("later_increased_genre_share", later_shares.get(increased, 0.0)),
                    _fact("distribution_shift", variation),
                    _fact("scope", "distinct games with timestamped achievement activity per five-year era"),
                ],
            "strength": round(clamp(variation), 6),
                "coverage": shift_coverage,
                "source": SOURCE,
                "confidence": _confidence(shift_coverage, True),
            })

    burst_rhythm_candidates = sorted(
        (row for row in timelines if row["burst_count"] > 0),
        key=lambda row: (
            -row["max_unlocks_in_24h"],
            -row["burst_count"],
            row["appid"],
        ),
    )
    long_return_candidates = sorted(
        (
            row
            for row in timelines
            if row["activity_year_count"] >= 2 and row["comeback_count"] > 0
        ),
        key=lambda row: (
            -row["achievement_span_days"],
            -row["activity_year_count"],
            -row["comeback_count"],
            row["appid"],
        ),
    )
    rhythm_pair = next(
        (
            (burst, long_running)
            for burst in burst_rhythm_candidates
            for long_running in long_return_candidates
            if burst["appid"] != long_running["appid"]
        ),
        None,
    )
    if rhythm_pair is not None:
        burst, long_running = rhythm_pair
        burst_strength = clamp(float(burst["max_unlocks_in_24h"]) / 10)
        long_strength = clamp(
            math.log1p(max(0.0, float(long_running["achievement_span_days"])))
            / math.log1p(3650)
        )
        tensions.append({
            "id": (
                "pattern:achievement-rhythm-contrast:"
                f"{burst['appid']}:{long_running['appid']}"
            ),
            "type": "achievement_rhythm_contrast",
            "facts": [
                _fact("burst_game_appid", burst["appid"]),
                _fact(
                    "burst_max_unlocks_in_24h",
                    burst["max_unlocks_in_24h"],
                ),
                _fact("burst_count", burst["burst_count"]),
                _fact("burst_dates", burst["burst_dates"]),
                _fact("long_running_game_appid", long_running["appid"]),
                _fact(
                    "long_achievement_span_days",
                    long_running["achievement_span_days"],
                ),
                _fact(
                    "long_activity_year_count",
                    long_running["activity_year_count"],
                ),
                _fact("long_comeback_count", long_running["comeback_count"]),
                _fact("long_largest_gap_days", long_running["largest_gap_days"]),
                _fact("scope", "observable achievement activity"),
            ],
            "related_ids": [
                f"game:{burst['appid']}",
                f"game:{long_running['appid']}",
            ],
            "strength": round((burst_strength + long_strength) / 2, 6),
            "coverage": timeline_signal_coverage,
            "source": SOURCE,
            "confidence": _confidence(timeline_signal_coverage, True),
        })

    if anti_mainstream_val is not None and anti_mainstream_val >= 50.0 and len(unlocked_percents) >= 3:
        tensions.append({
            "id": "pattern:anti-mainstream-divergence",
            "type": "anti_mainstream_divergence",
            "facts": [
                _fact("anti_mainstream_score", anti_mainstream_val),
                _fact("mean_unlocked_global_percent", round(mean_unlocked_percent, 2)),
                _fact("unlocked_achievement_count", len(unlocked_percents)),
                _fact("rarest_unlock_name", unlocked_with_rarity[0].get("name") if unlocked_with_rarity else ""),
                _fact("rarest_unlock_global_percent", unlocked_with_rarity[0].get("global_percent") if unlocked_with_rarity else None),
                _fact("scope", "player unlocked achievements with global rarity data"),
            ],
            "strength": round(clamp(anti_mainstream_val / 100.0), 6),
            "coverage": rarity_signal_coverage,
            "source": SOURCE,
            "confidence": _confidence(rarity_signal_coverage, True),
        })

    if peak_burst_info and peak_burst_info["unlock_count"] >= 5:
        tensions.append({
            "id": f"pattern:peak-daily-burst:{peak_burst_info['date']}",
            "type": "peak_daily_burst",
            "facts": [
                _fact("peak_date", peak_burst_info["date"]),
                _fact("unlock_count", peak_burst_info["unlock_count"]),
                _fact("game_count", peak_burst_info["game_count"]),
                _fact("appids", peak_burst_info["appids"]),
                _fact("scope", "single-day UTC achievement unlock cluster"),
            ],
            "related_ids": [f"game:{aid}" for aid in peak_burst_info["appids"][:4]],
            "strength": round(clamp(float(peak_burst_info["unlock_count"]) / 20.0), 6),
            "coverage": timeline_signal_coverage,
            "source": SOURCE,
            "confidence": _confidence(timeline_signal_coverage, True),
        })

    seq_breakers = []
    for inv in inversions:
        missing = inv.get("easiest_missing", {})
        unlocked = inv.get("rarest_unlocked", {})
        m_pct = _finite_number(missing.get("global_percent"))
        u_pct = _finite_number(unlocked.get("global_percent"))
        if m_pct is not None and u_pct is not None and m_pct >= 70.0 and u_pct <= 10.0:
            gap = float(inv.get("gap_percentage_points", m_pct - u_pct))
            seq_breakers.append((gap, inv))
    if seq_breakers:
        seq_breakers.sort(key=lambda item: (-item[0], str(item[1].get("appid"))))
        best_gap, best_inv = seq_breakers[0]
        b_appid = str(best_inv.get("appid", ""))
        b_name = str(best_inv.get("name", b_appid))
        b_miss = best_inv.get("easiest_missing", {})
        b_unl = best_inv.get("rarest_unlocked", {})
        tensions.append({
            "id": f"pattern:sequence-breaker-anomaly:{b_appid}",
            "type": "sequence_breaker_anomaly",
            "facts": [
                _fact("appid", b_appid),
                _fact("game_name", b_name),
                _fact("easiest_missing_name", str(b_miss.get("name") or b_miss.get("api_name"))),
                _fact("easiest_missing_global_percent", b_miss.get("global_percent")),
                _fact("rarest_unlocked_name", str(b_unl.get("name") or b_unl.get("api_name"))),
                _fact("rarest_unlocked_global_percent", b_unl.get("global_percent")),
                _fact("gap_percentage_points", best_gap),
                _fact("scope", "unlocked rare challenge with missing common progression"),
            ],
            "related_ids": [f"game:{b_appid}"],
            "strength": round(clamp(best_gap / 100.0), 6),
            "coverage": rarity_signal_coverage,
            "source": SOURCE,
            "confidence": _confidence(rarity_signal_coverage, True),
        })

    near_complete_candidates = []
    for game in games:
        appid = str(game.get("appid", ""))
        mins = _playtime(game)
        if mins < 1800:
            continue
        block = game.get("achievements") if isinstance(game.get("achievements"), Mapping) else {}
        items = block.get("items", []) if isinstance(block, Mapping) else []
        if not items or len(items) < 10:
            continue
        unlocked_count = sum(1 for it in items if isinstance(it, Mapping) and it.get("achieved"))
        total_count = len(items)
        comp = unlocked_count / total_count
        locked_count = total_count - unlocked_count
        if 0.85 <= comp < 1.0 and 1 <= locked_count <= 5:
            near_complete_candidates.append((mins, comp, locked_count, game))
    if near_complete_candidates:
        near_complete_candidates.sort(key=lambda item: (-item[0], -item[1]))
        nc_mins, nc_comp, nc_locked, nc_game = near_complete_candidates[0]
        nc_appid = str(nc_game.get("appid", ""))
        nc_name = str(nc_game.get("name", nc_appid))
        tensions.append({
            "id": f"pattern:near-complete-plateau:{nc_appid}",
            "type": "near_complete_plateau",
            "facts": [
                _fact("appid", nc_appid),
                _fact("game_name", nc_name),
                _fact("playtime_minutes", nc_mins),
                _fact("completion_ratio", round(nc_comp, 4)),
                _fact("remaining_locked_count", nc_locked),
                _fact("scope", "high-playtime anchor plateauing near perfection"),
            ],
            "related_ids": [f"game:{nc_appid}"],
            "strength": round(clamp(nc_mins / 6000.0) * 0.5 + clamp(nc_comp) * 0.5, 6),
            "coverage": completion_coverage,
            "source": SOURCE,
            "confidence": _confidence(completion_coverage, True),
        })

    high_friction_genres = {"Action", "Shooter", "Fighting", "Survival", "RPG"}
    low_friction_genres = {"Casual", "Puzzle", "Simulation", "Visual Novel"}
    high_friction_mins = 0.0
    low_friction_mins = 0.0
    high_games_list = []
    low_games_list = []
    for game in games:
        appid = str(game.get("appid", ""))
        mins = _playtime(game)
        if mins <= 0:
            continue
        g_genres = set(normalized_by_app.get(appid, {}).get("gameplay_genres", []))
        name = str(game.get("name", appid))
        if g_genres.intersection(high_friction_genres):
            high_friction_mins += mins
            high_games_list.append((mins, appid, name))
        if g_genres.intersection(low_friction_genres):
            low_friction_mins += mins
            low_games_list.append((mins, appid, name))
    if high_friction_mins >= 600.0 and low_friction_mins >= 300.0:
        balance_ratio = min(high_friction_mins, low_friction_mins) / max(high_friction_mins, low_friction_mins)
        if balance_ratio >= 0.10:
            high_games_list.sort(key=lambda x: -x[0])
            low_games_list.sort(key=lambda x: -x[0])
            top_h = high_games_list[0]
            top_l = low_games_list[0]
            f_strength = clamp(balance_ratio * 1.5) * 0.5 + clamp((high_friction_mins + low_friction_mins) / 6000.0) * 0.5
            tensions.append({
                "id": "pattern:flow-friction-contrast",
                "type": "flow_friction_contrast",
                "facts": [
                    _fact("high_friction_playtime_minutes", round(high_friction_mins, 1)),
                    _fact("low_friction_playtime_minutes", round(low_friction_mins, 1)),
                    _fact("high_friction_top_game", top_h[2]),
                    _fact("low_friction_top_game", top_l[2]),
                    _fact("balance_ratio", round(balance_ratio, 4)),
                    _fact("scope", "coexistence of high-friction challenge and low-friction loops"),
                ],
                "related_ids": [f"game:{top_h[1]}", f"game:{top_l[1]}"],
                "strength": round(f_strength, 6),
                "coverage": full_coverage,
                "source": SOURCE,
                "confidence": _confidence(full_coverage, True),
            })

    coop_genres = {"Massively Multiplayer"}
    solo_genres = {"Strategy", "RPG", "Simulation", "Visual Novel"}
    coop_mins = 0.0
    solo_mins = 0.0
    coop_games_list = []
    solo_games_list = []
    for game in games:
        appid = str(game.get("appid", ""))
        mins = _playtime(game)
        if mins <= 0:
            continue
        g_genres = set(normalized_by_app.get(appid, {}).get("gameplay_genres", []))
        name = str(game.get("name", appid))
        is_coop = bool(g_genres.intersection(coop_genres)) or any(k in name.lower() for k in ("left 4 dead", "sniper elite", "borderlands", "co-op", "multiplayer"))
        if is_coop:
            coop_mins += mins
            coop_games_list.append((mins, appid, name))
        elif g_genres.intersection(solo_genres):
            solo_mins += mins
            solo_games_list.append((mins, appid, name))
    if coop_mins >= 600.0 and solo_mins >= 600.0:
        coop_games_list.sort(key=lambda x: -x[0])
        solo_games_list.sort(key=lambda x: -x[0])
        top_c = coop_games_list[0]
        top_s = solo_games_list[0]
        cs_strength = clamp(min(coop_mins, solo_mins) / 3000.0)
        tensions.append({
            "id": "pattern:coop-vs-solo-polarization",
            "type": "coop_vs_solo_polarization",
            "facts": [
                _fact("coop_playtime_minutes", round(coop_mins, 1)),
                _fact("solo_playtime_minutes", round(solo_mins, 1)),
                _fact("top_coop_game", top_c[2]),
                _fact("top_solo_game", top_s[2]),
                _fact("scope", "bimodal engagement between social co-op and solitary depth"),
            ],
            "related_ids": [f"game:{top_c[1]}", f"game:{top_s[1]}"],
            "strength": round(cs_strength, 6),
            "coverage": full_coverage,
            "source": SOURCE,
            "confidence": _confidence(full_coverage, True),
        })

    era_span_val = release_era.get("era_span", {}).get("value")
    if era_span_val is not None and era_span_val >= 14:
        old_games_data = [
            (g, _playtime(g))
            for g in games
            if _game_release_year(g) is not None and _game_release_year(g) <= 2010 and _playtime(g) > 0
        ]
        modern_games_data = [
            (g, _playtime(g))
            for g in games
            if _game_release_year(g) is not None and _game_release_year(g) >= 2020 and _playtime(g) > 0
        ]
        if len(old_games_data) >= 1 and len(modern_games_data) >= 1:
            old_mins = sum(item[1] for item in old_games_data)
            modern_mins = sum(item[1] for item in modern_games_data)
            if old_mins >= 120.0 and modern_mins >= 120.0:
                min_y = min(_game_release_year(g) for g in games if _game_release_year(g) is not None and _playtime(g) > 0)
                max_y = max(_game_release_year(g) for g in games if _game_release_year(g) is not None and _playtime(g) > 0)
                tensions.append({
                    "id": "pattern:era-evolution-strata",
                    "type": "era_evolution_strata",
                    "facts": [
                        _fact("era_span_years", int(era_span_val)),
                        _fact("earliest_release_year", min_y),
                        _fact("latest_release_year", max_y),
                        _fact("classic_era_played_count", len(old_games_data)),
                        _fact("modern_era_played_count", len(modern_games_data)),
                        _fact("classic_era_playtime_minutes", round(old_mins, 1)),
                        _fact("modern_era_playtime_minutes", round(modern_mins, 1)),
                        _fact("scope", "cross-decade release era span with parallel activity"),
                    ],
                    "strength": round(clamp(era_span_val / 20.0) * 0.6 + clamp(old_mins / 3000.0) * 0.4, 6),
                    "coverage": release_coverage,
                    "source": SOURCE,
                    "confidence": _confidence(release_coverage, True),
                })

    genre_stats: dict[str, dict[str, Any]] = {}
    for game in games:
        appid = str(game.get("appid", ""))
        mins = _playtime(game)
        if mins <= 0:
            continue
        g_genres = normalized_by_app.get(appid, {}).get("gameplay_genres", [])
        comp = next((r["completion"] for r in completion_rows if str(r.get("appid")) == appid), None)
        for g in g_genres:
            st = genre_stats.setdefault(g, {"count": 0, "playtime": 0.0, "completions": []})
            st["count"] += 1
            st["playtime"] += mins
            if comp is not None:
                st["completions"].append(comp)

    spec_cand = None
    tour_cand = None
    for g_name, st in genre_stats.items():
        if st["count"] >= 2 and st["completions"]:
            avg_c = fmean(st["completions"])
            if avg_c >= 0.50 and st["playtime"] >= 600.0:
                if spec_cand is None or avg_c > spec_cand[1]:
                    spec_cand = (g_name, avg_c, st["playtime"])
            if avg_c <= 0.25 and st["count"] >= 2:
                if tour_cand is None or avg_c < tour_cand[1]:
                    tour_cand = (g_name, avg_c, st["count"])

    if spec_cand and tour_cand and spec_cand[0] != tour_cand[0]:
        gst_strength = clamp(spec_cand[1] - tour_cand[1])
        tensions.append({
            "id": f"pattern:genre-specialist-vs-tourist:{spec_cand[0]}:{tour_cand[0]}",
            "type": "genre_specialist_vs_tourist",
            "facts": [
                _fact("specialist_genre", spec_cand[0]),
                _fact("specialist_mean_completion", round(spec_cand[1], 4)),
                _fact("specialist_playtime_minutes", round(spec_cand[2], 1)),
                _fact("tourist_genre", tour_cand[0]),
                _fact("tourist_mean_completion", round(tour_cand[1], 4)),
                _fact("tourist_game_count", tour_cand[2]),
                _fact("scope", "contrast between deep mastery genre and exploratory tourist shelf"),
            ],
            "strength": round(gst_strength, 6),
            "coverage": genre_coverage,
            "source": SOURCE,
            "confidence": _confidence(genre_coverage, True),
        })

    return {
        "schema_version": "1.0",
        "run_id": profile.get("run_id"),
        "player_alias": profile.get("player_alias"),
        "generated_at": profile.get("generated_at"),
        "thresholds": {
            key: options[key]
            for key in (
                "meaningful_playtime_minutes",
                "comeback_gap_days",
                "burst_window_hours",
                "burst_min_unlocks",
                "recent_years",
                "old_game_age_years",
            )
        },
        "library": library,
        "playtime": playtime,
        "genres": genres,
        "release_era": release_era,
        "achievements": achievements,
        "series_groups": _signal(
            series_groups,
            series_coverage,
            definition="Conservative played-title prefix groups; semantic confirmation required",
        ),
        "cross_game_patterns": _signal(
            cross_game_patterns,
            cross_game_coverage,
            definition="Deterministic groups of played games sharing a measurable non-series pattern",
        ),
        "candidate_tensions": sorted(tensions, key=lambda row: (-row["strength"], row["id"])),
    }


__all__ = ["DEFAULT_CONFIG", "derive_signals"]
