"""Deterministic candidate selection for bounded achievement semantics.

The selector is deliberately independent from the Agent packet builder.  It
reads the complete normalized profile and deterministic evidence, then writes
an internal candidate ledger.  Aggregate statistics remain owned by
``analytics.py`` and ``evidence.py``; this module only chooses examples.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from .paths import REFERENCES_ROOT, SCHEMA_ROOT

from .context_budget import (
    ASSIGNMENT_ENVELOPE_RESERVE_BYTES,
    MAX_ACHIEVEMENT_CANDIDATE_GAMES,
    MAX_ACHIEVEMENT_PACKETS,
    MAX_ACHIEVEMENTS_PER_GAME_PER_PACKET,
    PACKET_MAX_ESTIMATED_TOKENS,
    PACKET_MAX_UTF8_BYTES,
    canonical_json_bytes,
    sha256_bytes,
    sha256_path,
)
from .fingerprint import _year, compute_evidence_fingerprint
from .evidence import fact_value
from .io_utils import read_json
from .time_utils import parse_timestamp


ACHIEVEMENT_ALLOWED_CLASSIFICATIONS = (
    "rare-unlock",
    "common-miss",
    "inversion",
    "milestone",
    "activity-span",
    "burst",
    "ordinary",
)
ACHIEVEMENT_COMPLETION_CRITERION = (
    "Classify only the bounded candidate achievements and return observable, safe claims."
)
ACHIEVEMENT_CANDIDATE_CONTRACT = "steam-visualogue-achievement-candidates"

_ACHIEVEMENT_ID = re.compile(r"^achievement:([1-9][0-9]*):(.+)$")
_BLOCKED_ACHIEVEMENT_STATUSES = {
    "unavailable",
    "private",
    "missing",
    "unsupported",
    "error",
}
_REASON_ORDER = (
    "rare-unlocked",
    "common-miss",
    "inversion",
    "completion-pole",
    "high-playtime-low-completion",
    "low-playtime-high-completion",
    "dated-activity",
    "comeback",
    "burst",
    "series-linked",
    "verified-cross-game",
)


class CandidateSelectionError(ValueError):
    """The deterministic candidate contract cannot be satisfied safely."""


def _digest(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _stable_signal_value(value: Any) -> Any:
    """Canonicalize signal collections whose order is not semantic here."""

    if isinstance(value, Mapping):
        return {str(key): _stable_signal_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, list):
        normalized = [_stable_signal_value(item) for item in value]
        return sorted(normalized, key=lambda item: canonical_json_bytes(item))
    if isinstance(value, tuple):
        return sorted((_stable_signal_value(item) for item in value), key=lambda item: canonical_json_bytes(item))
    return value


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _appid(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _appid_text(value: Any) -> str:
    parsed = _appid(value)
    return str(parsed) if parsed is not None else ""


def _fact_map(record: Mapping[str, Any]) -> dict[str, Any]:
    facts = record.get("facts")
    if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
        return {}
    return {
        str(fact.get("name")): fact.get("value")
        for fact in facts
        if isinstance(fact, Mapping) and fact.get("name")
    }


def _record_appid(record: Mapping[str, Any]) -> str | None:
    record_id = str(record.get("id") or "")
    match = _ACHIEVEMENT_ID.match(record_id)
    if match:
        return match.group(1)
    for key in ("appid", "game_id"):
        value = record.get(key)
        if isinstance(value, str) and value.startswith("game:"):
            value = value.removeprefix("game:")
        appid = _appid(value)
        if appid is not None:
            return str(appid)
    value = fact_value(record, "appid")
    appid = _appid(value)
    return str(appid) if appid is not None else None


def _era(year: int) -> str:
    if year < 2000:
        return "Before 2000"
    return f"{(year // 10) * 10}s"


def _normalized_genres(game: Mapping[str, Any]) -> list[str]:
    metadata = game.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    raw = metadata.get("genres", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    values = {
        str(item).strip()
        for item in raw
        if isinstance(item, str) and str(item).strip()
    }
    # These are Steam attributes rather than gameplay genres.  Analytics uses
    # the same distinction for its genre distributions.
    values -= {"Indie", "Free to Play", "Early Access"}
    return sorted(values)


def _status(game: Mapping[str, Any]) -> str:
    block = game.get("achievements")
    return str(block.get("status") or "").casefold() if isinstance(block, Mapping) else ""


def _profile_achievement_index(profile: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    games = profile.get("games", [])
    if not isinstance(games, Sequence) or isinstance(games, (str, bytes)):
        return index
    for game in games:
        if not isinstance(game, Mapping):
            continue
        appid = _appid_text(game.get("appid"))
        block = game.get("achievements")
        items = block.get("items", []) if isinstance(block, Mapping) else []
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("api_name") or item.get("name") or "").strip()
            if name:
                index[(appid, name)] = item
    return index


def _evidence_sections(evidence: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    sections: dict[str, list[dict[str, Any]]] = {}
    for section in ("metrics", "games", "achievements", "patterns", "cards"):
        rows = evidence.get(section, [])
        sections[section] = [
            dict(row)
            for row in rows
            if isinstance(row, Mapping) and row.get("id")
        ] if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) else []
    return sections


def _referenced_evidence_fingerprint(
    sections: Mapping[str, Sequence[Mapping[str, Any]]],
    evidence_ids: Sequence[str],
) -> str:
    """Fingerprint only the evidence records exposed for one game."""

    wanted = {str(value) for value in evidence_ids if value}
    records = [
        {"section": str(section), "record": dict(record)}
        for section, rows in sections.items()
        for record in rows
        if str(record.get("id") or "") in wanted
    ]
    records.sort(key=lambda item: (str(item["record"].get("id", "")), item["section"], canonical_json_bytes(item["record"])))
    return _digest({"evidence_ids": sorted(wanted), "records": records})


def _evidence_achievement_rows(
    appid: str,
    sections: Mapping[str, Sequence[Mapping[str, Any]]],
    profile_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in sorted(sections.get("achievements", []), key=lambda item: str(item.get("id", ""))):
        record_id = str(record.get("id") or "")
        match = _ACHIEVEMENT_ID.match(record_id)
        if not match or match.group(1) != appid:
            continue
        api_name = match.group(2)
        source = profile_index.get((appid, api_name), {})
        facts = _fact_map(record)
        state = str(facts.get("state") or ("unlocked" if source.get("achieved") else "missing")).casefold()
        unlocked = state in {"unlocked", "achieved", "true", "1"}
        percent = _finite(facts.get("global_percent", source.get("global_percent")))
        if percent is not None:
            percent = min(100.0, max(0.0, percent))
        timestamp_value = facts.get("unlock_time", source.get("unlock_time"))
        timestamp = parse_timestamp(timestamp_value)
        rows.append(
            {
                "achievement_id": record_id,
                "name": str(facts.get("name") or source.get("name") or api_name),
                "global_percent": round(percent, 6) if percent is not None else None,
                "unlocked": unlocked,
                "unlock_timestamp": int(timestamp) if timestamp is not None else None,
                "evidence_id": record_id,
                "evidence_strength": round(max(0.0, min(1.0, float(record.get("strength", 0.0) or 0.0))), 6),
                "evidence_type": str(record.get("type") or "achievement"),
            }
        )
    # A repeated evidence ID cannot become two semantic inputs.  Keep the
    # strongest deterministic record if malformed source data duplicates it.
    deduplicated: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: (-item["evidence_strength"], item["evidence_id"])):
        deduplicated.setdefault(str(row["evidence_id"]), row)
    return [deduplicated[key] for key in sorted(deduplicated)]


def _signal_value(signals: Mapping[str, Any], section: str, key: str) -> Any:
    container = signals.get(section)
    if not isinstance(container, Mapping):
        return None
    value = container.get(key)
    if isinstance(value, Mapping) and "value" in value:
        return value.get("value")
    return value


def _achievement_signal_ids(signals: Mapping[str, Any]) -> dict[str, set[str]]:
    output: dict[str, set[str]] = {}

    def add(family: str, appid: Any, api_name: Any) -> None:
        app = _appid_text(appid)
        name = str(api_name or "").strip()
        if app and name:
            output.setdefault(family, set()).add(f"achievement:{app}:{name}")

    section = signals.get("achievements")
    if isinstance(section, Mapping):
        for key, family in (
            ("top_surprising_unlocks", "rare-unlocked"),
            ("top_surprising_misses", "common-miss"),
        ):
            rows = _signal_value(signals, "achievements", key)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                for row in rows:
                    if isinstance(row, Mapping):
                        add(family, row.get("appid"), row.get("api_name"))
        inversions = _signal_value(signals, "achievements", "inversion_candidates")
        if isinstance(inversions, Sequence) and not isinstance(inversions, (str, bytes)):
            for inversion in inversions:
                if not isinstance(inversion, Mapping):
                    continue
                for key, family in (("rarest_unlocked", "inversion"), ("easiest_missing", "inversion")):
                    row = inversion.get(key)
                    if isinstance(row, Mapping):
                        add(family, row.get("appid") or inversion.get("appid"), row.get("api_name"))
    return output


def _pattern_links(
    signals: Mapping[str, Any],
    sections: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, set[str]]:
    links: dict[str, set[str]] = {}

    def add(family: str, appid: Any) -> None:
        value = _appid_text(appid)
        if value:
            links.setdefault(family, set()).add(value)

    achievement_section = signals.get("achievements")
    if isinstance(achievement_section, Mapping):
        for key, family in (
            ("timelines", "dated-activity"),
            ("comeback_games", "comeback"),
            ("burst_games", "burst"),
        ):
            rows = _signal_value(signals, "achievements", key)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                for row in rows:
                    if isinstance(row, Mapping):
                        add(family, row.get("appid"))

    series = _signal_value(signals, "series_groups", "value")
    if series is None:
        series = signals.get("series_groups")
    if isinstance(series, Sequence) and not isinstance(series, (str, bytes)):
        for group in series:
            if not isinstance(group, Mapping):
                continue
            for game in group.get("games", []) if isinstance(group.get("games"), Sequence) else []:
                if isinstance(game, Mapping):
                    add("series-linked", game.get("appid") or str(game.get("game_id", "")).removeprefix("game:"))

    patterns = _signal_value(signals, "cross_game_patterns", "value")
    if patterns is None:
        patterns = signals.get("cross_game_patterns")
    if isinstance(patterns, Sequence) and not isinstance(patterns, (str, bytes)):
        for pattern in patterns:
            if not isinstance(pattern, Mapping):
                continue
            for game in pattern.get("games", []) if isinstance(pattern.get("games"), Sequence) else []:
                if isinstance(game, Mapping):
                    add("verified-cross-game", game.get("appid") or str(game.get("game_id", "")).removeprefix("game:"))

    for record in sections.get("patterns", []):
        record_type = str(record.get("type") or "").casefold()
        family = None
        for token, value in (
            ("inversion", "inversion"),
            ("comeback", "comeback"),
            ("burst", "burst"),
            ("span", "dated-activity"),
            ("rhythm", "dated-activity"),
            ("series", "series-linked"),
            ("cross_game", "verified-cross-game"),
        ):
            if token in record_type:
                family = value
                break
        if family:
            add(family, _record_appid(record))
    return links


def _completion_by_app(signals: Mapping[str, Any]) -> dict[str, float | None]:
    rows = _signal_value(signals, "achievements", "completion_by_game")
    result: dict[str, float | None] = {}
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return result
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        appid = _appid_text(row.get("appid"))
        value = _finite(row.get("completion"))
        if appid:
            result[appid] = min(1.0, max(0.0, value)) if value is not None else None
    return result


def _release_year(game: Mapping[str, Any]) -> int | None:
    metadata = game.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    return _year(metadata.get("release_year")) or _year(metadata.get("release_date"))


def _source_fingerprints(
    profile: Mapping[str, Any],
    signals: Mapping[str, Any],
    evidence: Mapping[str, Any],
    run_dir: Path | None = None,
) -> dict[str, str]:
    evidence_value = str(evidence.get("evidence_fingerprint") or compute_evidence_fingerprint(profile))
    if run_dir is not None and (run_dir / "evidence.json").is_file():
        evidence_value = str(evidence.get("evidence_fingerprint") or sha256_path(run_dir / "evidence.json"))
    # Exclude run identity and generation timestamps so a repeated source with
    # the same report inputs can reuse semantic work.
    signal_payload = {
        str(key): _stable_signal_value(value)
        for key, value in signals.items()
        if key not in {"run_id", "generated_at", "evidence_fingerprint"}
    }
    return {
        "profile": compute_evidence_fingerprint(profile),
        "signals": _digest(signal_payload),
        "evidence": evidence_value,
    }


def achievement_analysis_contract_fingerprint(
    report_locale: str = "en-US",
    *,
    schema_root: str | Path | None = None,
    context_path: str | Path | None = None,
) -> str:
    """Fingerprint every current rule that can change achievement semantics."""

    root = Path(schema_root) if schema_root is not None else SCHEMA_ROOT
    context = Path(context_path) if context_path is not None else REFERENCES_ROOT / "agent-context.md"
    schemas = {
        name: json.loads((root / name).read_text(encoding="utf-8"))
        for name in ("achievement-analysis-packet.schema.json", "achievement-analysis-result.schema.json")
    }
    context_text = context.read_text(encoding="utf-8")
    fresh_context = "\n".join(
        line.strip()
        for line in context_text.splitlines()
        if line.lstrip().casefold().startswith("| achievement analysis |")
    )
    return _digest(
        {
            "contract": ACHIEVEMENT_CANDIDATE_CONTRACT,
            "format": "steam-visualogue-agent-packet",
            "result_format": "steam-visualogue-agent-result",
            "report_locale": str(report_locale),
            "packet_schema": schemas["achievement-analysis-packet.schema.json"],
            "result_schema": schemas["achievement-analysis-result.schema.json"],
            "allowed_classifications": ACHIEVEMENT_ALLOWED_CLASSIFICATIONS,
            "completion_criterion": ACHIEVEMENT_COMPLETION_CRITERION,
            "fresh_context": fresh_context,
            "budgets": {
                "packet_utf8_bytes": PACKET_MAX_UTF8_BYTES,
                "packet_estimated_tokens": PACKET_MAX_ESTIMATED_TOKENS,
                "assignment_envelope_reserve_bytes": ASSIGNMENT_ENVELOPE_RESERVE_BYTES,
                "candidate_games": MAX_ACHIEVEMENT_CANDIDATE_GAMES,
                "achievement_packets": MAX_ACHIEVEMENT_PACKETS,
                "achievements_per_game_per_packet": MAX_ACHIEVEMENTS_PER_GAME_PER_PACKET,
            },
        }
    )


def achievement_game_input_fingerprint(
    game: Mapping[str, Any],
    *,
    evidence_fingerprint: str,
) -> str:
    """Return the content identity of one exposed achievement packet game."""

    achievements = []
    for item in game.get("achievements", []) if isinstance(game.get("achievements"), Sequence) else []:
        if not isinstance(item, Mapping):
            continue
        achievements.append(
            {
                "achievement_id": str(item.get("achievement_id") or ""),
                "name": str(item.get("name") or ""),
                "global_percent": item.get("global_percent"),
                "unlocked": bool(item.get("unlocked")),
                "unlock_timestamp": item.get("unlock_timestamp"),
                "evidence_id": str(item.get("evidence_id") or ""),
            }
        )
    achievements.sort(key=lambda item: item["achievement_id"])
    payload = {
        "appid": _appid(game.get("game_id", "").removeprefix("game:")) if isinstance(game.get("game_id"), str) else game.get("appid"),
        "canonical_name": str(game.get("canonical_name") or game.get("name") or ""),
        "playtime_minutes": game.get("playtime_minutes", 0),
        "completion": game.get("completion"),
        "achievements": achievements,
        "evidence_fingerprint": str(evidence_fingerprint),
    }
    return _digest(payload)


def _selection_score(
    *,
    playtime_minutes: float,
    completion: float | None,
    achievement_rows: Sequence[Mapping[str, Any]],
    reasons: Sequence[str],
    strata: Sequence[str],
) -> tuple[dict[str, float], float]:
    rarity = [
        -math.log(max(float(row["global_percent"]) / 100.0, 0.001))
        for row in achievement_rows
        if row.get("unlocked") and _finite(row.get("global_percent")) is not None
    ]
    evidence_strength = max(
        [float(row.get("evidence_strength", 0.0) or 0.0) for row in achievement_rows]
        or [0.0]
    )
    components = {
        "evidence_strength": round(evidence_strength, 6),
        "rarity_signal": round(min(1.0, max(rarity or [0.0]) / 8.0), 6),
        "family_count": round(min(1.0, len(set(reasons)) / 4.0), 6),
        "representation_count": round(min(1.0, len(set(strata)) / 5.0), 6),
        "completion_pole": round(
            1.0 - abs((completion if completion is not None else 0.5) - 0.5) * 2,
            6,
        ),
        "playtime_signal": round(min(1.0, math.log1p(max(0.0, playtime_minutes)) / math.log1p(60_000)), 6),
    }
    score = (
        components["evidence_strength"] * 0.30
        + components["rarity_signal"] * 0.25
        + components["family_count"] * 0.20
        + components["representation_count"] * 0.10
        + components["completion_pole"] * 0.10
        + components["playtime_signal"] * 0.05
    )
    return components, round(score, 9)


def _summary(
    *,
    all_played: Sequence[Mapping[str, Any]],
    eligible_played: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    selected: Sequence[Mapping[str, Any]],
    excluded: Mapping[str, int],
    source_fingerprints: Mapping[str, str],
    contract_fingerprint: str,
) -> dict[str, Any]:
    selected_ids = {str(row.get("game_id")) for row in selected}
    eligible_playtime = sum(float(row.get("playtime_minutes", 0.0) or 0.0) for row in eligible_played)
    selected_playtime = sum(float(row.get("playtime_minutes", 0.0) or 0.0) for row in selected)
    eligible_achievements = sum(len(row.get("achievements", [])) for row in candidates)
    selected_achievements = sum(len(row.get("achievements", [])) for row in selected)

    def counts(key: str) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for row in selected:
            for value in row.get(key, []) if isinstance(row.get(key), list) else []:
                counter[str(value)] += 1
        return {name: counter[name] for name in sorted(counter)}

    return {
        "eligible_playtime_minutes": round(eligible_playtime, 3),
        "selected_playtime_minutes": round(selected_playtime, 3),
        "eligible_game_count": len(candidates),
        "eligible_played_game_count": len(eligible_played),
        "selected_game_count": len(selected),
        "eligible_achievement_count": eligible_achievements,
        "selected_achievement_count": selected_achievements,
        "selected_fraction_of_eligible_played_titles": round(
            len(selected) / len(eligible_played) if eligible_played else 0.0, 9
        ),
        "selected_fraction_of_eligible_playtime": round(
            selected_playtime / eligible_playtime if eligible_playtime else 0.0, 9
        ),
        "selected_game_ids": sorted(selected_ids, key=lambda value: int(value.removeprefix("game:"))),
        "counts_by_selection_reason": counts("selection_reasons"),
        "counts_by_genre_stratum": counts("genre_strata"),
        "counts_by_release_era_stratum": counts("release_era_strata"),
        "counts_by_playtime_stratum": counts("playtime_strata"),
        "counts_by_completion_stratum": counts("completion_strata"),
        "counts_by_achievement_family": counts("evidence_families"),
        "excluded_counts": {str(key): int(value) for key, value in sorted(excluded.items())},
        "all_played_game_count": len(all_played),
        "source_fingerprints": dict(source_fingerprints),
        "candidate_contract_fingerprint": contract_fingerprint,
    }


def select_achievement_candidates(
    profile: Mapping[str, Any],
    signals: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    report_locale: str = "en-US",
    max_games: int = MAX_ACHIEVEMENT_CANDIDATE_GAMES,
    run_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build a deterministic, evidence-backed candidate artifact in memory."""

    limit = int(max_games)
    if limit < 0:
        raise ValueError("max_games must be non-negative")
    limit = min(MAX_ACHIEVEMENT_CANDIDATE_GAMES, limit)
    root = Path(run_dir) if run_dir is not None else None
    source_fingerprints = _source_fingerprints(profile, signals, evidence, root)
    contract_fingerprint = achievement_analysis_contract_fingerprint(report_locale)
    sections = _evidence_sections(evidence)
    profile_index = _profile_achievement_index(profile)
    signal_achievement_families = _achievement_signal_ids(signals)
    signal_game_families = _pattern_links(signals, sections)
    completion_by_app = _completion_by_app(signals)

    raw_games = profile.get("games", [])
    games = [game for game in raw_games if isinstance(game, Mapping)] if isinstance(raw_games, Sequence) else []
    games = sorted(games, key=lambda game: (_appid(game.get("appid")) or 0, str(game.get("name") or "")))
    played = [game for game in games if (_finite(game.get("playtime_minutes")) or 0.0) > 0]
    # Deciles are assigned over the complete played library, never the
    # shortlist.  Ties are settled numerically by AppID and then name.
    ranked_for_decile = sorted(
        played,
        key=lambda game: (
            _finite(game.get("playtime_minutes")) or 0.0,
            _appid(game.get("appid")) or 0,
            str(game.get("name") or ""),
        ),
    )
    decile_by_app = {
        _appid_text(game.get("appid")): min(10, max(1, math.ceil((index + 1) * 10 / len(ranked_for_decile))))
        for index, game in enumerate(ranked_for_decile)
        if _appid_text(game.get("appid"))
    }

    candidates: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    eligible_played: list[dict[str, Any]] = []
    for game in games:
        appid = _appid_text(game.get("appid"))
        if not appid:
            excluded["invalid_appid"] += 1
            continue
        playtime = _finite(game.get("playtime_minutes")) or 0.0
        if playtime <= 0:
            excluded["unplayed"] += 1
            continue
        status = _status(game)
        if status in _BLOCKED_ACHIEVEMENT_STATUSES:
            excluded[f"{status}_achievements"] += 1
            continue
        eligible_played.append({"appid": appid, "playtime_minutes": playtime})
        achievement_block = game.get("achievements")
        profile_items = achievement_block.get("items", []) if isinstance(achievement_block, Mapping) else []
        if not isinstance(profile_items, Sequence) or isinstance(profile_items, (str, bytes)) or not any(
            isinstance(item, Mapping) and str(item.get("api_name") or item.get("name") or "").strip()
            for item in profile_items
        ):
            excluded["no_usable_achievement_candidate"] += 1
            continue
        achievement_rows = _evidence_achievement_rows(appid, sections, profile_index)
        if not achievement_rows:
            excluded["no_usable_achievement_candidate"] += 1
            continue

        achievement_ids = {str(row["evidence_id"]) for row in achievement_rows}
        reasons: set[str] = set()
        for family, ids in signal_achievement_families.items():
            if achievement_ids.intersection(ids):
                reasons.add(family)
        for family, appids in signal_game_families.items():
            if appid in appids:
                reasons.add(family)
        for row in achievement_rows:
            percent = _finite(row.get("global_percent"))
            evidence_type = str(row.get("evidence_type") or "").casefold()
            if row.get("unlocked") and percent is not None and percent < 5:
                reasons.add("rare-unlocked")
            if not row.get("unlocked") and percent is not None and percent >= 50:
                reasons.add("common-miss")
            if "inversion" in evidence_type:
                reasons.add("inversion")
            if parse_timestamp(row.get("unlock_timestamp")) is not None:
                reasons.add("dated-activity")
        completion = completion_by_app.get(appid)
        if completion is None:
            items = (game.get("achievements") or {}).get("items", []) if isinstance(game.get("achievements"), Mapping) else []
            if isinstance(items, Sequence) and items:
                unlocked = sum(bool(item.get("achieved")) for item in items if isinstance(item, Mapping))
                completion = unlocked / len(items)
        if completion is not None and (completion <= 0.2 or completion >= 0.8):
            reasons.add("completion-pole")
        if not reasons:
            excluded["no_observable_semantic_reason"] += 1
            continue

        genres = _normalized_genres(game)
        release_year = _release_year(game)
        genre_strata = [f"genre:{genre}" for genre in genres]
        release_era_strata = [f"release-era:{_era(release_year)}"] if release_year is not None else []
        playtime_decile = decile_by_app.get(appid, 1)
        playtime_strata = [f"playtime-decile:{playtime_decile:02d}"]
        if completion is not None and playtime_decile >= 8 and completion <= 0.2:
            reasons.add("high-playtime-low-completion")
        if completion is not None and playtime_decile <= 3 and completion >= 0.8:
            reasons.add("low-playtime-high-completion")
        completion_strata = []
        if completion is not None:
            pole = "low" if completion <= 0.2 else "high" if completion >= 0.8 else "mid"
            completion_strata.append(f"completion:{pole}")
        evidence_families = sorted(reasons, key=lambda value: (_REASON_ORDER.index(value) if value in _REASON_ORDER else len(_REASON_ORDER), value))
        strata = genre_strata + release_era_strata + playtime_strata + completion_strata
        components, score = _selection_score(
            playtime_minutes=playtime,
            completion=completion,
            achievement_rows=achievement_rows,
            reasons=evidence_families,
            strata=strata,
        )
        packet_game = {
            "game_id": f"game:{appid}",
            "canonical_name": str(game.get("name") or appid),
            "playtime_minutes": round(playtime, 3),
            "completion": round(completion, 9) if completion is not None else None,
            "coverage": {
                "titles": 1.0,
                "playtime": 1.0,
                "achievements": 1.0,
            },
            "achievements": [
                {
                    key: value
                    for key, value in row.items()
                    if key in {
                        "achievement_id",
                        "name",
                        "global_percent",
                        "unlocked",
                        "unlock_timestamp",
                        "evidence_id",
                    }
                }
                for row in sorted(
                    achievement_rows,
                    key=lambda item: (
                        -float(item.get("evidence_strength", 0.0) or 0.0),
                        -float(item.get("global_percent") or 0.0) if item.get("unlocked") else float(item.get("global_percent") or 0.0),
                        str(item.get("achievement_id")),
                    ),
                )[:MAX_ACHIEVEMENTS_PER_GAME_PER_PACKET]
            ],
        }
        if not packet_game["achievements"]:
            excluded["no_bounded_achievement_record"] += 1
            continue
        referenced_evidence_fingerprint = _referenced_evidence_fingerprint(
            sections,
            [str(row["evidence_id"]) for row in packet_game["achievements"]],
        )
        packet_game["referenced_evidence_fingerprint"] = referenced_evidence_fingerprint
        packet_game["game_input_fingerprint"] = achievement_game_input_fingerprint(
            packet_game,
            evidence_fingerprint=referenced_evidence_fingerprint,
        )
        candidates.append(
            {
                **packet_game,
                "appid": int(appid),
                "evidence_ids": [str(row["evidence_id"]) for row in packet_game["achievements"]],
                "selection_reasons": evidence_families,
                "evidence_families": evidence_families,
                "genre_strata": genre_strata,
                "release_era_strata": release_era_strata,
                "playtime_strata": playtime_strata,
                "completion_strata": completion_strata,
                "representation_strata": sorted(strata),
                "score_components": components,
                "deterministic_score": score,
                "source_fingerprints": dict(source_fingerprints),
            }
        )

    candidates.sort(key=lambda row: (-float(row.get("deterministic_score", 0.0)), int(row.get("appid", 0)), str(row.get("canonical_name", ""))))
    required_labels = sorted(
        {
            str(label)
            for row in candidates
            for label in list(row.get("evidence_families", [])) + list(row.get("representation_strata", []))
        }
    )
    by_label: dict[str, list[dict[str, Any]]] = {label: [] for label in required_labels}
    for row in candidates:
        for label in set(row.get("evidence_families", [])) | set(row.get("representation_strata", [])):
            if label in by_label:
                by_label[label].append(row)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    covered: set[str] = set()
    reservations: dict[str, list[str]] = {}
    for label in required_labels:
        if label in covered:
            continue
        options = by_label.get(label, [])
        if not options:
            raise CandidateSelectionError(f"required candidate reservation is unavailable: {label}")
        chosen = options[0]
        game_id = str(chosen["game_id"])
        if game_id not in selected_ids:
            selected.append(chosen)
            selected_ids.add(game_id)
        covered.update(set(chosen.get("evidence_families", [])) | set(chosen.get("representation_strata", [])))
        reservations.setdefault(game_id, []).append(label)

    if len(selected) > limit:
        raise CandidateSelectionError(
            f"required candidate reservations exceed the {limit}-game ceiling"
        )
    for row in candidates:
        if len(selected) >= limit:
            break
        if str(row["game_id"]) not in selected_ids:
            selected.append(row)
            selected_ids.add(str(row["game_id"]))
    selected.sort(key=lambda row: (-float(row.get("deterministic_score", 0.0)), int(row.get("appid", 0)), str(row.get("canonical_name", ""))))
    mandatory_ids = sorted(
        reservations,
        key=lambda value: int(value.removeprefix("game:")),
    )
    candidate_identity = [
        {
            "game_id": row["game_id"],
            "game_input_fingerprint": row["game_input_fingerprint"],
            "selection_reasons": row["selection_reasons"],
            "representation_strata": row["representation_strata"],
        }
        for row in selected
    ]
    selected_set_fingerprint = _digest(
        {
            "contract": contract_fingerprint,
            "source_fingerprints": source_fingerprints,
            "selected": candidate_identity,
        }
    )
    summary = _summary(
        all_played=[{"appid": _appid_text(game.get("appid")), "playtime_minutes": game.get("playtime_minutes", 0)} for game in played],
        eligible_played=eligible_played,
        candidates=candidates,
        selected=selected,
        excluded=excluded,
        source_fingerprints=source_fingerprints,
        contract_fingerprint=contract_fingerprint,
    )
    artifact_without_fingerprint = {
        "format": ACHIEVEMENT_CANDIDATE_CONTRACT,
        "source_fingerprints": dict(source_fingerprints),
        "analysis_contract_fingerprint": contract_fingerprint,
        "selected_set_fingerprint": selected_set_fingerprint,
        "mandatory_game_ids": mandatory_ids,
        "candidates": candidates,
        "selected": selected,
        "summary": summary,
    }
    candidate_fingerprint = _digest(artifact_without_fingerprint)
    return {"candidate_fingerprint": candidate_fingerprint, **artifact_without_fingerprint}


def load_run_inputs(run_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = Path(run_dir)
    return (
        read_json(root / "profile.json"),
        read_json(root / "signals.json"),
        read_json(root / "evidence.json"),
    )


def write_candidate_artifact(run_dir: str | Path, artifact: Mapping[str, Any]) -> Path:
    """Write one internal candidate artifact using canonical JSON semantics."""

    root = Path(run_dir)
    path = root / ".agent-work" / "candidates" / "achievement-analysis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(dict(artifact))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def build_candidate_artifact(
    run_dir: str | Path,
    *,
    report_locale: str = "en-US",
    max_games: int = MAX_ACHIEVEMENT_CANDIDATE_GAMES,
) -> dict[str, Any]:
    profile, signals, evidence = load_run_inputs(run_dir)
    artifact = select_achievement_candidates(
        profile,
        signals,
        evidence,
        report_locale=report_locale,
        max_games=max_games,
        run_dir=run_dir,
    )
    write_candidate_artifact(run_dir, artifact)
    return artifact


def candidate_artifact_is_current(
    run_dir: str | Path,
    artifact: Mapping[str, Any],
    *,
    report_locale: str = "en-US",
) -> bool:
    root = Path(run_dir)
    try:
        profile, signals, evidence = load_run_inputs(root)
        expected_sources = _source_fingerprints(profile, signals, evidence, root)
        expected_contract = achievement_analysis_contract_fingerprint(report_locale)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    fingerprint_payload = dict(artifact)
    candidate_fingerprint = str(fingerprint_payload.pop("candidate_fingerprint", ""))
    return (
        candidate_fingerprint.startswith("sha256:")
        and candidate_fingerprint == _digest(fingerprint_payload)
        and artifact.get("source_fingerprints") == expected_sources
        and artifact.get("analysis_contract_fingerprint") == expected_contract
    )


def finalize_selected_candidates(
    artifact: Mapping[str, Any],
    selected_game_ids: Sequence[str],
    *,
    excluded_reason: str = "packet_budget",
) -> dict[str, Any]:
    """Rebind an artifact after the packet packer admits a bounded subset."""

    allowed = {str(value) for value in selected_game_ids}
    candidates = [dict(row) for row in artifact.get("candidates", []) if isinstance(row, Mapping)]
    selected = [row for row in candidates if str(row.get("game_id")) in allowed]
    selected.sort(key=lambda row: (-float(row.get("deterministic_score", 0.0)), int(row.get("appid", 0)), str(row.get("canonical_name", ""))))
    mandatory = {str(value) for value in artifact.get("mandatory_game_ids", [])}
    if not mandatory.issubset(allowed):
        missing = ", ".join(sorted(mandatory - allowed))
        raise CandidateSelectionError(f"required candidate reservations were dropped: {missing}")
    excluded = Counter()
    summary = dict(artifact.get("summary") or {})
    old_selected = {str(row.get("game_id")) for row in artifact.get("selected", []) if isinstance(row, Mapping)}
    for _ in sorted(old_selected - allowed):
        excluded[excluded_reason] += 1
    summary["selected_game_count"] = len(selected)
    summary["selected_achievement_count"] = sum(len(row.get("achievements", [])) for row in selected)
    eligible_playtime = float(summary.get("eligible_playtime_minutes", 0.0) or 0.0)
    selected_playtime = sum(float(row.get("playtime_minutes", 0.0) or 0.0) for row in selected)
    eligible_played_count = int(summary.get("eligible_played_game_count", 0) or 0)
    summary["selected_fraction_of_eligible_played_titles"] = round(
        len(selected) / eligible_played_count if eligible_played_count else 0.0, 9
    )
    summary["selected_playtime_minutes"] = round(selected_playtime, 3)
    summary["selected_fraction_of_eligible_playtime"] = round(
        selected_playtime / eligible_playtime if eligible_playtime else 0.0, 9
    )
    for field, summary_key in (
        ("selection_reasons", "counts_by_selection_reason"),
        ("genre_strata", "counts_by_genre_stratum"),
        ("release_era_strata", "counts_by_release_era_stratum"),
        ("playtime_strata", "counts_by_playtime_stratum"),
        ("completion_strata", "counts_by_completion_stratum"),
        ("evidence_families", "counts_by_achievement_family"),
    ):
        counter: Counter[str] = Counter()
        for row in selected:
            values = row.get(field, [])
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                for value in values:
                    counter[str(value)] += 1
        summary[summary_key] = {key: counter[key] for key in sorted(counter)}
    summary["selected_game_ids"] = sorted(
        {str(row.get("game_id")) for row in selected},
        key=lambda value: int(value.removeprefix("game:")),
    )
    previous_excluded = summary.get("excluded_counts", {})
    merged_excluded = Counter({str(key): int(value) for key, value in previous_excluded.items()})
    merged_excluded.update(excluded)
    summary["excluded_counts"] = {key: merged_excluded[key] for key in sorted(merged_excluded)}
    identity = [
        {
            "game_id": row["game_id"],
            "game_input_fingerprint": row["game_input_fingerprint"],
            "selection_reasons": row["selection_reasons"],
            "representation_strata": row["representation_strata"],
        }
        for row in selected
    ]
    selected_set = _digest(
        {
            "contract": artifact.get("analysis_contract_fingerprint"),
            "source_fingerprints": artifact.get("source_fingerprints", {}),
            "selected": identity,
        }
    )
    result = {
        **dict(artifact),
        "selected": selected,
        "selected_set_fingerprint": selected_set,
        "summary": summary,
    }
    result.pop("candidate_fingerprint", None)
    result["candidate_fingerprint"] = _digest(result)
    return result


__all__ = [
    "ACHIEVEMENT_ALLOWED_CLASSIFICATIONS",
    "ACHIEVEMENT_CANDIDATE_CONTRACT",
    "ACHIEVEMENT_COMPLETION_CRITERION",
    "CandidateSelectionError",
    "MAX_ACHIEVEMENT_CANDIDATE_GAMES",
    "MAX_ACHIEVEMENT_PACKETS",
    "achievement_analysis_contract_fingerprint",
    "achievement_game_input_fingerprint",
    "build_candidate_artifact",
    "candidate_artifact_is_current",
    "finalize_selected_candidates",
    "load_run_inputs",
    "select_achievement_candidates",
    "write_candidate_artifact",
]
