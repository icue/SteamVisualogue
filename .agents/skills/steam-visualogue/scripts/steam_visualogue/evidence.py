"""Build a compact, factual evidence ledger from deterministic signals."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from .fingerprint import compute_evidence_fingerprint
from .measurements import clamp


SOURCE = "derived:normalized_profile"
CARD_LIMIT = 40
EVIDENCE_SECTIONS = ("metrics", "games", "achievements", "patterns")


def _fact(name: str, value: Any, **context: Any) -> dict[str, Any]:
    result = {"name": name, "value": value}
    result.update(context)
    return result


def _signal_value(section: Mapping[str, Any], key: str, default: Any = None) -> Any:
    signal = section.get(key, {})
    return signal.get("value", default) if isinstance(signal, Mapping) else default


def _signal_coverage(section: Mapping[str, Any], key: str) -> dict[str, Any]:
    signal = section.get(key, {})
    coverage = signal.get("coverage", {}) if isinstance(signal, Mapping) else {}
    return dict(coverage) if isinstance(coverage, Mapping) else {}


def _coverage_score(coverage: Mapping[str, Any]) -> float:
    keys = (
        {"achievements", "timestamps", "rarity"}
        if "achievements" in coverage
        else {"titles", "playtime"}
    )
    values = [
        float(value)
        for key, value in coverage.items()
        if key in keys and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ]
    return min(values) if values else 0.0


def _slug_or_digest(value: Any, fallback: Mapping[str, Any] | None = None) -> str:
    if isinstance(value, str) and value.strip():
        clean = re.sub(r"\s+", "_", value.strip())
        return clean.replace(":", "_")
    encoded = json.dumps(fallback or {}, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
    return "unnamed-" + hashlib.sha256(encoded).hexdigest()[:12]


def _metric_strength(path: str, value: Any, coverage: Mapping[str, Any]) -> float:
    base = 0.45 + 0.35 * _coverage_score(coverage)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if any(token in path for token in ("share", "ratio", "completion", "gini", "hhi")):
            base += 0.15 * abs(float(value) - 0.5) * 2
        elif float(value) != 0:
            base += 0.08
    return round(clamp(base), 6)


def _record(
    evidence_id: str,
    evidence_type: str,
    facts: list[dict[str, Any]],
    strength: float,
    coverage: Mapping[str, Any],
    *,
    confidence: str | None = None,
    related_ids: Sequence[str] = (),
) -> dict[str, Any]:
    score = _coverage_score(coverage)
    if confidence is None:
        confidence = "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
    result = {
        "id": evidence_id,
        "type": evidence_type,
        "facts": facts,
        "strength": round(clamp(float(strength)), 6),
        "coverage": dict(coverage),
        "source": SOURCE,
        "confidence": confidence,
    }
    if related_ids:
        result["related_ids"] = sorted(set(related_ids))
    return result


def _metric_id(section: str, key: str) -> str:
    if section == "library":
        return f"metric:{key}"
    aliases = {"genres": "genre", "release_era": "release", "achievements": "achievement"}
    return f"metric:{aliases.get(section, section)}:{key}"


def _metric_records(signals: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    excluded = {
        "ranked_games",
        "normalized_by_game",
        "completion_by_game",
        "top_surprising_unlocks",
        "top_surprising_misses",
        "inversion_candidates",
        "timelines",
        "comeback_games",
        "burst_games",
        "peak_daily_burst",
        "career_timeline",
    }
    for section_name in ("library", "playtime", "genres", "release_era", "achievements"):
        section = signals.get(section_name, {})
        if not isinstance(section, Mapping):
            continue
        for key in sorted(section):
            signal = section[key]
            if key in excluded or not isinstance(signal, Mapping) or "value" not in signal:
                continue
            value = signal.get("value")
            # Large object-valued signals are represented by dedicated records.
            if isinstance(value, (list, dict)) and key not in {"dominant_genres", "coverage"}:
                continue
            coverage = signal.get("coverage", {})
            if not isinstance(coverage, Mapping):
                coverage = {}
            facts = [_fact(f"{section_name}.{key}", value)]
            if signal.get("definition"):
                facts.append(_fact("definition", signal["definition"]))
            records.append(
                _record(
                    _metric_id(section_name, key),
                    "metric",
                    facts,
                    _metric_strength(f"{section_name}.{key}", value, coverage),
                    coverage,
                    confidence=str(signal.get("confidence", "low")),
                )
            )
    threshold = signals.get("thresholds", {}).get("meaningful_playtime_minutes")
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        records.append(
            _record(
                "metric:meaningful_played_threshold",
                "threshold",
                [
                    _fact("minutes", threshold),
                    _fact("scope", "minimum playtime for the meaningfully played classification"),
                ],
                0.9,
                {"titles": 1.0, "playtime": 1.0},
                confidence="high",
                related_ids=["metric:meaningfully_played_count"],
            )
        )
    return sorted(records, key=lambda row: row["id"])


def _profile_indexes(profile: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[tuple[str, str], Mapping[str, Any]]]:
    games: dict[str, Mapping[str, Any]] = {}
    achievements: dict[tuple[str, str], Mapping[str, Any]] = {}
    raw_games = profile.get("games", [])
    if not isinstance(raw_games, Sequence):
        return games, achievements
    for game in raw_games:
        if not isinstance(game, Mapping):
            continue
        appid = str(game.get("appid", ""))
        games[appid] = game
        block = game.get("achievements") if isinstance(game.get("achievements"), Mapping) else {}
        items = block.get("items", [])
        if not isinstance(items, Sequence):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            api_name = _slug_or_digest(item.get("api_name"), item)
            achievements[(appid, api_name)] = item
    return games, achievements


def _game_records(profile: Mapping[str, Any], signals: Mapping[str, Any]) -> list[dict[str, Any]]:
    playtime = signals.get("playtime", {})
    genres = signals.get("genres", {})
    achievements = signals.get("achievements", {})
    ranked = _signal_value(playtime, "ranked_games", [])
    rank_by_app = {str(row.get("appid")): row for row in ranked if isinstance(row, Mapping)} if isinstance(ranked, Sequence) else {}
    genre_by_app = _signal_value(genres, "normalized_by_game", {})
    genre_by_app = genre_by_app if isinstance(genre_by_app, Mapping) else {}
    completion_rows = _signal_value(achievements, "completion_by_game", [])
    completion_by_app = {
        str(row.get("appid")): row for row in completion_rows if isinstance(row, Mapping)
    } if isinstance(completion_rows, Sequence) else {}
    records: list[dict[str, Any]] = []
    raw_games = profile.get("games", [])
    if not isinstance(raw_games, Sequence):
        return records
    for game in raw_games:
        if not isinstance(game, Mapping):
            continue
        appid = str(game.get("appid", ""))
        name = str(game.get("name") or appid)
        ranking = rank_by_app.get(appid, {})
        playtime_minutes = float(
            ranking.get("playtime_minutes", game.get("playtime_minutes", 0))
            if isinstance(ranking, Mapping)
            else game.get("playtime_minutes", 0)
        )
        if playtime_minutes <= 0:
            continue
        share = float(ranking.get("share", 0.0)) if isinstance(ranking, Mapping) else 0.0
        normalized = genre_by_app.get(appid, {}) if isinstance(genre_by_app, Mapping) else {}
        completion = completion_by_app.get(appid)
        metadata = game.get("metadata") if isinstance(game.get("metadata"), Mapping) else {}
        release_value = metadata.get("release_year") or metadata.get("release_date")
        facts = [
            _fact("appid", appid),
            _fact("name", name),
            _fact("playtime_minutes", playtime_minutes),
            _fact("playtime_share", share),
        ]
        if isinstance(normalized, Mapping):
            facts.append(_fact("gameplay_genres", normalized.get("gameplay_genres", [])))
            facts.append(_fact("attributes", normalized.get("attributes", [])))
        if release_value is not None:
            facts.append(_fact("release_date", release_value))
        related_ids: list[str] = []
        if isinstance(completion, Mapping):
            facts.extend([
                _fact("achievement_completion", completion.get("completion")),
                _fact("achievements_unlocked", completion.get("unlocked")),
                _fact("achievements_total", completion.get("total")),
            ])
        strength = 0.25 + 0.65 * math.sqrt(max(0.0, share))
        coverage = {"titles": 1.0, "playtime": 1.0}
        records.append(_record(f"game:{appid}", "game", facts, strength, coverage, related_ids=related_ids))
    return sorted(records, key=lambda row: row["id"])


def _selected_achievement_rows(signals: Mapping[str, Any]) -> dict[tuple[str, str], tuple[str, Mapping[str, Any]]]:
    section = signals.get("achievements", {})
    if not isinstance(section, Mapping):
        return {}
    selected: dict[tuple[str, str], tuple[str, Mapping[str, Any]]] = {}
    for key, state in (("top_surprising_unlocks", "unlocked"), ("top_surprising_misses", "missing")):
        rows = _signal_value(section, key, [])
        if not isinstance(rows, Sequence):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            appid = str(row.get("appid", ""))
            api_name = _slug_or_digest(row.get("api_name"), row)
            selected[(appid, api_name)] = (state, row)
    inversions = _signal_value(section, "inversion_candidates", [])
    if isinstance(inversions, Sequence):
        for inversion in inversions:
            if not isinstance(inversion, Mapping):
                continue
            for field, state in (("rarest_unlocked", "unlocked"), ("easiest_missing", "missing")):
                row = inversion.get(field)
                if not isinstance(row, Mapping):
                    continue
                appid = str(row.get("appid") or inversion.get("appid", ""))
                api_name = _slug_or_digest(row.get("api_name"), row)
                selected[(appid, api_name)] = (state, row)
    return selected


def _achievement_records(profile: Mapping[str, Any], signals: Mapping[str, Any]) -> list[dict[str, Any]]:
    _, source_items = _profile_indexes(profile)
    selected = _selected_achievement_rows(signals)
    achievement_section = signals.get("achievements", {})
    coverage = _signal_coverage(achievement_section, "top_surprising_unlocks") if isinstance(achievement_section, Mapping) else {}
    records: list[dict[str, Any]] = []
    for (appid, api_name), (state, row) in sorted(selected.items()):
        source = source_items.get((appid, api_name), {})
        percent = row.get("global_percent")
        surprise = float(row.get("surprise", 0.0) or 0.0)
        facts = [
            _fact("appid", appid),
            _fact("api_name", api_name),
            _fact("name", row.get("name") or source.get("name") or api_name),
            _fact("state", state),
            _fact("global_percent", percent),
            _fact("surprise_score", surprise),
            _fact("hidden", bool(row.get("hidden", source.get("hidden", False)))),
        ]
        description = source.get("description") if isinstance(source, Mapping) else None
        if isinstance(description, str) and description.strip():
            facts.append(_fact("description", description.strip()))
        strength = 0.4 + 0.6 * clamp(surprise / 8.0)
        records.append(
            _record(
                f"achievement:{appid}:{api_name}",
                "achievement",
                facts,
                strength,
                coverage,
                related_ids=[f"game:{appid}"],
            )
        )
    return sorted(records, key=lambda row: row["id"])


def _pattern_records(signals: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    tensions = signals.get("candidate_tensions", [])
    if isinstance(tensions, Sequence):
        for tension in tensions:
            if not isinstance(tension, Mapping):
                continue
            evidence_id = str(tension.get("id") or "")
            if not evidence_id.startswith("pattern:"):
                continue
            facts = tension.get("facts", [])
            related_ids = tension.get("related_ids", [])
            records.append(
                _record(
                    evidence_id,
                    str(tension.get("type") or "cross_signal_candidate"),
                    list(facts) if isinstance(facts, Sequence) else [],
                    float(tension.get("strength", 0.0)),
                    tension.get("coverage", {}) if isinstance(tension.get("coverage"), Mapping) else {},
                    related_ids=(
                        [str(item) for item in related_ids if item]
                        if isinstance(related_ids, list)
                        else []
                    ),
                    confidence=str(tension.get("confidence", "low")),
                )
            )
            if str(tension.get("type") or "") == "cross_game_pattern_atlas":
                pattern_key = next(
                    (
                        str(fact.get("value"))
                        for fact in facts
                        if isinstance(fact, Mapping) and fact.get("name") == "pattern_key"
                    ),
                    "pattern",
                )
                game_rows = next(
                    (
                        fact.get("value")
                        for fact in facts
                        if isinstance(fact, Mapping) and fact.get("name") == "game_rows"
                    ),
                    [],
                )
                if isinstance(game_rows, Sequence):
                    for row in game_rows:
                        if not isinstance(row, Mapping):
                            continue
                        appid = str(row.get("appid", ""))
                        if not appid.isdigit():
                            continue
                        row_facts = [
                            _fact(str(name), value)
                            for name, value in row.items()
                            if name != "evidence_ids"
                        ]
                        records.append(
                            _record(
                                f"{evidence_id}:{appid}",
                                f"cross_game_{pattern_key}",
                                row_facts,
                                float(tension.get("strength", 0.0)),
                                tension.get("coverage", {}) if isinstance(tension.get("coverage"), Mapping) else {},
                                related_ids=[f"game:{appid}", evidence_id],
                                confidence=str(tension.get("confidence", "low")),
                            )
                        )

    section = signals.get("achievements", {})
    if not isinstance(section, Mapping):
        return sorted(records, key=lambda row: row["id"])
    inversion_coverage = _signal_coverage(section, "inversion_candidates")
    inversions = _signal_value(section, "inversion_candidates", [])
    if isinstance(inversions, Sequence):
        for row in inversions:
            if not isinstance(row, Mapping):
                continue
            appid = str(row.get("appid", ""))
            unlocked = row.get("rarest_unlocked", {})
            missing = row.get("easiest_missing", {})
            if not isinstance(unlocked, Mapping) or not isinstance(missing, Mapping):
                continue
            unlocked_id = f"achievement:{appid}:{_slug_or_digest(unlocked.get('api_name'), unlocked)}"
            missing_id = f"achievement:{appid}:{_slug_or_digest(missing.get('api_name'), missing)}"
            gap = float(row.get("gap_percentage_points", 0.0))
            facts = [
                _fact("appid", appid),
                _fact("rarest_unlocked_global_percent", unlocked.get("global_percent"), achievement_id=unlocked_id),
                _fact("easiest_missing_global_percent", missing.get("global_percent"), achievement_id=missing_id),
                _fact("gap_percentage_points", gap),
                _fact("semantic_status", "unchecked"),
            ]
            records.append(
                _record(
                    f"pattern:achievement_inversion:{appid}",
                    "achievement_inversion_candidate",
                    facts,
                    0.45 + 0.55 * clamp(gap / 100),
                    inversion_coverage,
                    related_ids=[f"game:{appid}", unlocked_id, missing_id],
                )
            )

    timeline_coverage = _signal_coverage(section, "timelines")
    longest = _signal_value(section, "longest_observable_achievement_span")
    if isinstance(longest, Mapping):
        appid = str(longest.get("appid", ""))
        days = float(longest.get("achievement_span_days", 0.0))
        records.append(
            _record(
                f"pattern:longest_achievement_span:{appid}",
                "observable_achievement_span",
                [
                    _fact("appid", appid),
                    _fact("first_achievement_at", longest.get("first_achievement_at")),
                    _fact("last_achievement_at", longest.get("last_achievement_at")),
                    _fact("achievement_span_days", days),
                    _fact("scope", "observable achievement activity"),
                ],
                    0.45 + 0.55 * clamp(math.log1p(max(0.0, days)) / math.log1p(3650)),
                timeline_coverage,
                related_ids=[f"game:{appid}"],
            )
        )
    for key, evidence_type, score_key, id_prefix in (
        ("comeback_games", "achievement_comeback", "largest_gap_days", "achievement_comeback"),
        ("burst_games", "achievement_burst", "max_unlocks_in_24h", "achievement_burst"),
    ):
        rows = _signal_value(section, key, [])
        if not isinstance(rows, Sequence):
            continue
        coverage = _signal_coverage(section, key)
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            appid = str(row.get("appid", ""))
            if key == "comeback_games":
                facts = [
                    _fact("appid", appid),
                    _fact("largest_gap_days", row.get("largest_gap_days")),
                    _fact("comeback_count", row.get("comeback_count")),
                    _fact("threshold_days", signals.get("thresholds", {}).get("comeback_gap_days")),
                ]
                strength = 0.45 + 0.55 * clamp(float(row.get(score_key, 0.0)) / 730)
            else:
                facts = [
                    _fact("appid", appid),
                    _fact("max_unlocks_in_24h", row.get("max_unlocks_in_24h")),
                    _fact("burst_count", row.get("burst_count")),
                    _fact("burst_dates", row.get("burst_dates", [])),
                ]
                strength = 0.45 + 0.55 * clamp(float(row.get(score_key, 0.0)) / 10)
            records.append(
                _record(
                    f"pattern:{id_prefix}:{appid}",
                    evidence_type,
                    facts,
                    strength,
                    coverage,
                    related_ids=[f"game:{appid}"],
                )
            )
    deduplicated = {record["id"]: record for record in records}
    return [deduplicated[key] for key in sorted(deduplicated)]


def _condensed_cards(
    metrics: Sequence[Mapping[str, Any]],
    games: Sequence[Mapping[str, Any]],
    achievements: Sequence[Mapping[str, Any]],
    patterns: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    # Reserve equal room for each evidence family, then fill unused capacity
    # using the original information-density priority.
    groups = (
        (0, patterns),
        (1, achievements),
        (2, games),
        (3, metrics),
    )
    quota = CARD_LIMIT // len(groups)
    selected: list[tuple[int, Mapping[str, Any]]] = []
    remaining: list[tuple[int, Mapping[str, Any]]] = []
    for priority, records in groups:
        ranked = sorted(
            records,
            key=lambda record: (
                -float(record.get("strength", 0.0)),
                str(record.get("id", "")),
            ),
        )
        if priority == 0:
            strongest_by_type: list[Mapping[str, Any]] = []
            repeated_types: list[Mapping[str, Any]] = []
            seen_types: set[str] = set()
            for record in ranked:
                evidence_type = str(record.get("type", ""))
                if evidence_type not in seen_types:
                    seen_types.add(evidence_type)
                    strongest_by_type.append(record)
                else:
                    repeated_types.append(record)
            ranked = strongest_by_type + repeated_types
        selected.extend((priority, record) for record in ranked[:quota])
        remaining.extend((priority, record) for record in ranked[quota:])
    remaining.sort(
        key=lambda pair: (
            pair[0],
            -float(pair[1].get("strength", 0.0)),
            str(pair[1].get("id", "")),
        )
    )
    selected.extend(remaining[: max(0, CARD_LIMIT - len(selected))])
    selected.sort(
        key=lambda pair: (
            pair[0],
            -float(pair[1].get("strength", 0.0)),
            str(pair[1].get("id", "")),
        )
    )
    return [dict(record) for _, record in selected]


def fact_value(record: Mapping[str, Any], name: str, default: Any = None) -> Any:
    """Return a fact by its qualified or short name without exposing the record."""

    facts = record.get("facts")
    if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
        return default
    requested = str(name).strip()
    short = requested.rsplit(".", 1)[-1]
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        fact_name = str(fact.get("name") or "").strip()
        if fact_name == requested or fact_name == short or fact_name.rsplit(".", 1)[-1] == short:
            return fact.get("value", default)
    return default


def evidence_catalog(evidence: Mapping[str, Any], *, include_cards: bool = False) -> dict[str, dict[str, Any]]:
    """Index the complete evidence ledger; condensed cards are not authoritative."""

    sections = EVIDENCE_SECTIONS + ("cards",) if include_cards else EVIDENCE_SECTIONS
    missing = [section for section in sections if not isinstance(evidence.get(section), list)]
    if missing:
        raise ValueError(
            "evidence.json must contain complete evidence sections: "
            + ", ".join(EVIDENCE_SECTIONS)
        )
    catalog: dict[str, dict[str, Any]] = {}
    for section in sections:
        for record in evidence[section]:
            if isinstance(record, dict) and record.get("id"):
                catalog[str(record["id"])] = record
    return catalog


def build_evidence(profile: Mapping[str, Any], signals: Mapping[str, Any]) -> dict[str, Any]:
    """Build the complete opaque evidence ledger and bounded candidate cards.

    The packet contains only observable facts and statistical candidates.  It
    intentionally does not convert those facts into player personality claims.
    Agent handoff packets are constructed later by :mod:`agent_packets`.
    """

    if not isinstance(profile, Mapping):
        raise TypeError("profile must be a mapping")
    if not isinstance(signals, Mapping):
        raise TypeError("signals must be a mapping")
    metrics = _metric_records(signals)
    games = _game_records(profile, signals)
    achievements = _achievement_records(profile, signals)
    patterns = _pattern_records(signals)
    cards = _condensed_cards(metrics, games, achievements, patterns)
    return {
        "schema_version": "1.0",
        "run_id": profile.get("run_id"),
        "generated_at": profile.get("generated_at"),
        "evidence_fingerprint": profile.get("evidence_fingerprint")
        or compute_evidence_fingerprint(profile),
        "metrics": metrics,
        "games": games,
        "achievements": achievements,
        "patterns": patterns,
        "cards": cards,
        "card_count": len(cards),
        "card_limit": CARD_LIMIT,
    }


__all__ = ["CARD_LIMIT", "EVIDENCE_SECTIONS", "build_evidence", "evidence_catalog", "fact_value"]
