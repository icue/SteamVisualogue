"""Materialize display labels for only the entities used by a report."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from .cache_db import CacheDB
from .evidence import evidence_catalog, fact_value
from .io_utils import read_json, require_files, write_json
from .locales import catalog_for, load_run_config, normalize_report_locale
from .planning import validate_schema_document


_GAME_ID = re.compile(r"^game:([1-9][0-9]*)$")
_ACHIEVEMENT_ID = re.compile(r"^achievement:([1-9][0-9]*):(.+)$")
TOKEN = re.compile(r"\{\{([^{}#|]+(?::[^{}#|]+)*)#([^{}|]+)\|([^{}]+)\}\}")
ProgressCallback = Callable[[str, int | None, int | None], None]


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_strings(child)


def scan_label_references(deck_plan: Mapping[str, Any]) -> dict[str, list[str]]:
    """Return entity IDs used by the current deck plan and copy tokens."""

    games: set[str] = set()
    achievements: set[str] = set()
    for value in _walk_strings(deck_plan):
        for match in TOKEN.finditer(value):
            evidence_id, fact_name, _ = (part.strip() for part in match.groups())
            if fact_name not in {"name", "description"}:
                continue
            game_match = _GAME_ID.fullmatch(evidence_id)
            achievement_match = _ACHIEVEMENT_ID.fullmatch(evidence_id)
            if game_match and fact_name == "name":
                games.add(evidence_id)
            elif achievement_match and fact_name in {"name", "description"}:
                achievements.add(evidence_id)
                games.add(f"game:{achievement_match.group(1)}")
    try:
        for _, value in _walk_typed(deck_plan):
            if not isinstance(value, Mapping):
                continue
            game_id = value.get("game_id")
            if isinstance(game_id, str) and _GAME_ID.fullmatch(game_id):
                games.add(game_id)
            achievement_id = value.get("achievement_id")
            if isinstance(achievement_id, str) and _ACHIEVEMENT_ID.fullmatch(achievement_id):
                achievements.add(achievement_id)
                games.add(f"game:{_ACHIEVEMENT_ID.fullmatch(achievement_id).group(1)}")
    except (TypeError, ValueError):
        # The compiler reports the complete contract error; scanning remains
        # useful for a draft that has not passed schema validation yet.
        pass
    return {"games": sorted(games), "achievements": sorted(achievements)}


def _walk_typed(value: Any):
    if isinstance(value, Mapping):
        yield (), value
        for key, child in value.items():
            for path, nested in _walk_typed(child):
                yield (str(key),) + path, nested
    elif isinstance(value, list):
        for index, child in enumerate(value):
            for path, nested in _walk_typed(child):
                yield (str(index),) + path, nested


def _canonical_game(catalog: Mapping[str, Mapping[str, Any]], game_id: str) -> str:
    card = catalog.get(game_id)
    if not isinstance(card, Mapping):
        raise ValueError(f"Evidence '{game_id}' is not available for label localization")
    missing = object()
    value = fact_value(card, "name", missing)
    if value is missing:
        raise ValueError(f"Evidence '{game_id}' has no fact named 'name'")
    return str(value or "").strip()


def _canonical_achievement(catalog: Mapping[str, Mapping[str, Any]], achievement_id: str) -> tuple[str, str]:
    card = catalog.get(achievement_id)
    if not isinstance(card, Mapping):
        raise ValueError(f"Evidence '{achievement_id}' is not available for label localization")
    return str(fact_value(card, "name") or "").strip(), str(fact_value(card, "description") or "").strip()


def compute_label_fingerprint(document: Mapping[str, Any]) -> str:
    """Hash only final display values and provenance, never timestamps."""

    payload = {
        "report_locale": normalize_report_locale(str(document.get("report_locale") or "")),
        "catalog_version": str(document.get("catalog_version") or ""),
        "games": document.get("games", {}),
        "achievements": document.get("achievements", {}),
        "failures": document.get("failures", []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def localized_labels_current(
    document: Mapping[str, Any],
    deck_plan: Mapping[str, Any],
    report_locale: str,
) -> bool:
    """Return whether labels match the current locale, catalog, and plan references."""

    try:
        normalized_locale = normalize_report_locale(report_locale)
        references = scan_label_references(deck_plan)
        if document.get("report_locale") != normalized_locale:
            return False
        if document.get("catalog_version") != catalog_for(normalized_locale).catalog_version:
            return False
        if set(document.get("games", {})) != set(references["games"]):
            return False
        if set(document.get("achievements", {})) != set(references["achievements"]):
            return False
        if compute_label_fingerprint(document) != document.get("label_fingerprint"):
            return False
        validate_schema_document("localized-labels.json", "localized-labels.schema.json", document)
        return True
    except (TypeError, ValueError, KeyError):
        return False


def _failure(entity_id: str, status: str) -> dict[str, str]:
    return {"id": entity_id, "status": status}


def _localized_app_name(api: Any, appid: int, language: str) -> str:
    raw = api.get_app_details(appid, language=language)
    return str(raw.get("name") or "").strip() if isinstance(raw, Mapping) else ""


def _localized_schema(api: Any, appid: int, language: str) -> list[dict[str, Any]]:
    raw = api.get_achievement_schema(appid, language=language)
    return [dict(item) for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def materialize_localized_labels(
    run_dir: str | Path,
    cache: CacheDB,
    *,
    api: Any | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Write a complete localized-labels artifact with per-entity fallback."""

    root = Path(run_dir)
    required = require_files(root, ["run-config.json", "profile.json", "evidence.json", "deck-plan.json"])
    report_locale = load_run_config(root)["report_locale"]
    profile = read_json(required["profile.json"])
    evidence = read_json(required["evidence.json"])
    deck_plan = read_json(required["deck-plan.json"])
    references = scan_label_references(deck_plan)
    catalog = evidence_catalog(evidence)
    games: dict[str, dict[str, str]] = {}
    achievements: dict[str, dict[str, str]] = {}
    failures: list[dict[str, str]] = []
    locale_catalog = catalog_for(report_locale)

    if progress:
        progress("Scanning report entity references", 0, len(references["games"]) + len(references["achievements"]))

    # en-US is deliberately cache- and network-free: canonical evidence is the
    # only English display source.
    for index, game_id in enumerate(references["games"], 1):
        appid = int(_GAME_ID.fullmatch(game_id).group(1))
        games[game_id] = {"display_name": _canonical_game(catalog, game_id), "source": "canonical"}
        if progress:
            progress("Materializing game labels", index, len(references["games"]) + len(references["achievements"]))

    if report_locale == "zh-CN":
        if api is None and (references["games"] or references["achievements"]):
            raise ValueError("A Steam API adapter is required for zh-CN label localization")
        # Each selected game is looked up independently so one Store failure
        # cannot prevent other labels from being localized.
        for index, game_id in enumerate(references["games"], 1):
            appid = int(_GAME_ID.fullmatch(game_id).group(1))
            cached = cache.get_localized_app_label(appid, report_locale, force=force, now=now)
            localized_name = str(cached.get("display_name") or "").strip() if cached else ""
            localized_ok = bool(localized_name) and cached and cached.get("status") == "ok"
            cached_failure = bool(cached and cached.get("status") != "ok" and not force)
            if force or (not localized_ok and not cached_failure):
                try:
                    localized_name = _localized_app_name(api, appid, locale_catalog.steam_language)
                    cache.upsert_localized_app_label(
                        appid, report_locale, localized_name, status="ok" if localized_name else "unavailable", fetched_at=now
                    )
                except Exception:
                    localized_name = ""
                    cache.upsert_localized_app_label(
                        appid, report_locale, None, status="unavailable", fetched_at=now
                    )
            if localized_name:
                games[game_id] = {"display_name": localized_name, "source": "steam-localized"}
            else:
                games[game_id] = {"display_name": _canonical_game(catalog, game_id), "source": "canonical-fallback"}
                failures.append(_failure(game_id, "localized-unavailable"))
            if progress:
                progress("Materializing game labels", index, len(references["games"]) + len(references["achievements"]))

        achievement_groups: dict[int, list[str]] = {}
        for achievement_id in references["achievements"]:
            match = _ACHIEVEMENT_ID.fullmatch(achievement_id)
            achievement_groups.setdefault(int(match.group(1)), []).append(achievement_id)
        for appid, achievement_ids in sorted(achievement_groups.items()):
            state = cache.get_localized_achievement_label_state(
                appid, report_locale, force=force, now=now
            )
            rows_by_name: dict[str, Mapping[str, Any]] = {}
            if state and state.get("status") == "ok" and not force:
                for achievement_id in achievement_ids:
                    apiname = _ACHIEVEMENT_ID.fullmatch(achievement_id).group(2)
                    cached = cache.get_localized_achievement_label(
                        appid, apiname, report_locale, force=force, now=now
                    )
                    if cached:
                        rows_by_name[apiname] = cached
            cached_failure = bool(state and state.get("status") != "ok" and not force)
            if (force or state is None or (state.get("status") != "ok" and not cached_failure)):
                try:
                    localized_rows = _localized_schema(api, appid, locale_catalog.steam_language)
                    cache.replace_localized_achievement_labels(
                        appid, report_locale, localized_rows, status="ok", fetched_at=now
                    )
                    rows_by_name = {
                        str(row.get("apiname") or row.get("name")): row
                        for row in localized_rows
                        if row.get("apiname") or row.get("name")
                    }
                except Exception:
                    cache.replace_localized_achievement_labels(
                        appid, report_locale, [], status="unavailable", fetched_at=now
                    )
                    rows_by_name = {}
            for achievement_id in achievement_ids:
                apiname = _ACHIEVEMENT_ID.fullmatch(achievement_id).group(2)
                row = rows_by_name.get(apiname)
                localized_name = str((row or {}).get("display_name") or (row or {}).get("displayName") or "").strip()
                localized_description = str((row or {}).get("description") or "").strip()
                canonical_name, canonical_description = _canonical_achievement(catalog, achievement_id)
                description_needs_fallback = bool(canonical_description and not localized_description)
                if localized_name and not description_needs_fallback:
                    achievements[achievement_id] = {
                        "display_name": localized_name,
                        "description": localized_description,
                        "source": "steam-localized",
                    }
                else:
                    achievements[achievement_id] = {
                        "display_name": canonical_name,
                        "description": canonical_description,
                        "source": "canonical-fallback",
                    }
                    failures.append(_failure(achievement_id, "localized-unavailable"))
    else:
        for achievement_id in references["achievements"]:
            name, description = _canonical_achievement(catalog, achievement_id)
            achievements[achievement_id] = {
                "display_name": name,
                "description": description,
                "source": "canonical",
            }

    document = {
        "format": "steam-visualogue-localized-labels",
        "run_id": str(profile.get("run_id") or ""),
        "report_locale": report_locale,
        "catalog_version": locale_catalog.catalog_version,
        "games": games,
        "achievements": achievements,
        "failures": sorted(failures, key=lambda item: (item["id"], item["status"])),
    }
    document["label_fingerprint"] = compute_label_fingerprint(document)
    validate_schema_document("localized-labels.json", "localized-labels.schema.json", document)
    write_json(root / "localized-labels.json", document)
    return document


__all__ = [
    "compute_label_fingerprint",
    "localized_labels_current",
    "materialize_localized_labels",
    "scan_label_references",
]
