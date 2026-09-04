"""Stable fingerprints for report-relevant acquisition and visual inputs."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any


EVIDENCE_FINGERPRINT_VERSION = "steam-visualogue:evidence-input:v1"
VISUAL_FINGERPRINT_VERSION = "steam-visualogue:visual-input"
ASSET_MANIFEST_FINGERPRINT_VERSION = "steam-visualogue:asset-manifest:v1"
VISUAL_BRIEF_FINGERPRINT_VERSION = "steam-visualogue:visual-brief:v1"
LAYOUT_INPUT_FINGERPRINT_VERSION = "steam-visualogue:layout-input:v1"


def _year(value: Any) -> int | None:
    if isinstance(value, int) and 1900 <= value <= 2200:
        return value
    if not isinstance(value, str):
        return None
    match = re.search(r"(?<!\d)(19\d{2}|20\d{2}|21\d{2})(?!\d)", value)
    return int(match.group(1)) if match else None


def _analysis_year(profile: Mapping[str, Any]) -> int:
    generated = _year(profile.get("generated_at"))
    if generated is not None:
        return generated
    releases = [
        year
        for game in profile.get("games", [])
        if isinstance(game, Mapping)
        for year in [_year((game.get("metadata") or {}).get("release_date"))]
        if year is not None
    ]
    return max(releases) if releases else 1970


def _semantic_status(value: Any) -> str:
    status = str(value or "missing").casefold()
    if status in {"ok", "cached"}:
        return "available"
    if status == "cached_stale":
        return "available"
    return status


def _normal(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normal(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normal(item) for item in value]
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float) and value == 0:
        return 0.0
    return value


def _digest(prefix: str, payload: Any) -> str:
    encoded = json.dumps(
        {"version": prefix, "payload": _normal(payload)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _canonical_game(game: Mapping[str, Any]) -> dict[str, Any]:
    achievement_block = game.get("achievements") or {}
    items = [
        _normal(item)
        for item in achievement_block.get("items", [])
        if isinstance(item, Mapping)
    ]
    items.sort(key=lambda item: (str(item.get("api_name", "")), json.dumps(item, sort_keys=True)))
    raw_status = game.get("data_status") or {}
    metadata = _normal(game.get("metadata") or {})
    for key in ("genres", "developers", "publishers", "categories"):
        if isinstance(metadata.get(key), list):
            metadata[key] = sorted(
                metadata[key],
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
            )
    return {
        "appid": int(game.get("appid") or 0),
        "name": str(game.get("name") or ""),
        "playtime_minutes": game.get("playtime_minutes", 0),
        "metadata": metadata,
        "artwork_url": game.get("artwork_url"),
        "achievements": {
            "status": _semantic_status(achievement_block.get("status")),
            "items": items,
        },
        "source_status": {
            key: _semantic_status(raw_status.get(key))
            for key in ("metadata", "achievements", "achievement_schema", "global_rarity")
        },
    }


def compute_evidence_fingerprint(profile: Mapping[str, Any]) -> str:
    """Hash every report-relevant profile input, excluding run identity and timestamps.

    The analysis year is retained explicitly because year-relative analytics can
    change across a calendar boundary even when the Steam account does not.
    """

    games = [
        _canonical_game(game)
        for game in profile.get("games", [])
        if isinstance(game, Mapping)
    ]
    games.sort(key=lambda game: (game["appid"], game["name"]))
    return _digest(
        EVIDENCE_FINGERPRINT_VERSION,
        {
            "analysis_year": _analysis_year(profile),
            "player_alias": str(profile.get("player_alias") or ""),
            "games": games,
        },
    )


def compute_visual_fingerprint(visual_signals: Mapping[str, Any]) -> str:
    """Hash stable visual evidence while ignoring run-local identifiers."""

    payload = {
        str(key): value
        for key, value in visual_signals.items()
        if key not in {"run_id", "evidence_fingerprint", "visual_fingerprint", "failures"}
    }
    payload["failures"] = sorted(
        [
            {"appid": item.get("appid"), "status": item.get("status")}
            for item in visual_signals.get("failures", [])
            if isinstance(item, Mapping)
            and item.get("status") != "cached-stale"
        ],
        key=lambda item: (int(item.get("appid") or 0), str(item.get("status") or "")),
    )
    return _digest(VISUAL_FINGERPRINT_VERSION, payload)


def compute_asset_manifest_fingerprint(asset_manifest: Mapping[str, Any]) -> str:
    """Hash the canonical asset manifest contents."""

    return _digest(ASSET_MANIFEST_FINGERPRINT_VERSION, asset_manifest)


def compute_visual_brief_fingerprint(visual_brief: Mapping[str, Any]) -> str:
    """Hash a visual brief without including its self-referential fingerprint."""

    payload = {
        key: value
        for key, value in visual_brief.items()
        if key != "visual_brief_fingerprint"
    }
    return _digest(VISUAL_BRIEF_FINGERPRINT_VERSION, payload)


def compute_layout_input_fingerprint(
    compiled_deck_fingerprint: str,
    art_direction: Mapping[str, Any],
    visual_brief_fingerprint: str,
    asset_manifest: Mapping[str, Any],
) -> str:
    """Hash the four canonical inputs consumed by layout composition."""

    return _digest(
        LAYOUT_INPUT_FINGERPRINT_VERSION,
        {
            "compiled_deck_fingerprint": compiled_deck_fingerprint,
            "art_direction": art_direction,
            "visual_brief_fingerprint": visual_brief_fingerprint,
            "asset_manifest": asset_manifest,
        },
    )


def image_pixel_sha256(path: Path) -> str | None:
    """Return a deterministic pixel hash for a single RGB/RGBA image."""

    try:
        from PIL import Image

        with Image.open(path) as image:
            if getattr(image, "n_frames", 1) != 1:
                return None
            if image.mode not in {"RGB", "RGBA"}:
                return None
            image.load()
            digest = hashlib.sha256()
            digest.update(f"{image.mode}:{image.width}x{image.height}\0".encode("ascii"))
            digest.update(image.tobytes())
            return digest.hexdigest()
    except Exception:
        return None


__all__ = [
    "EVIDENCE_FINGERPRINT_VERSION",
    "VISUAL_FINGERPRINT_VERSION",
    "ASSET_MANIFEST_FINGERPRINT_VERSION",
    "VISUAL_BRIEF_FINGERPRINT_VERSION",
    "LAYOUT_INPUT_FINGERPRINT_VERSION",
    "compute_asset_manifest_fingerprint",
    "compute_evidence_fingerprint",
    "compute_layout_input_fingerprint",
    "compute_visual_brief_fingerprint",
    "compute_visual_fingerprint",
    "image_pixel_sha256",
]
