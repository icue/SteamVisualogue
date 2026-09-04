"""Build bounded Agent packets from the current run artifacts.

The packet pipeline opens only the current evidence and visual-signal inputs
needed by an assignment, then writes bounded payloads into ``.agent-work``.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from .context_budget import (
    AgentPacketItemTooLarge,
    BudgetViolation,
    MAX_ACHIEVEMENT_PACKETS,
    MAX_EVIDENCE_CARDS_PER_CURATION_SHARD,
    MAX_FINAL_SEMANTIC_FINDINGS,
    MAX_FINDINGS_PER_CURATION_SHARD,
    MAX_IMAGES_PER_PACKET,
    assert_packet_budget,
    canonical_json_bytes,
    metrics_for_path,
    sha256_bytes,
    sha256_path,
)
from .fingerprint import (
    compute_asset_manifest_fingerprint,
    compute_visual_brief_fingerprint,
    compute_visual_fingerprint,
)
from .io_utils import read_json, require_files
from .asset_paths import artwork_reference, resolve_assets_dir
from .semantic_candidates import (
    ACHIEVEMENT_ALLOWED_CLASSIFICATIONS,
    ACHIEVEMENT_COMPLETION_CRITERION,
    MAX_ACHIEVEMENT_CANDIDATE_GAMES,
    achievement_analysis_contract_fingerprint,
    build_candidate_artifact,
    candidate_artifact_is_current,
    finalize_selected_candidates,
    write_candidate_artifact,
)


PACKET_FORMAT = "steam-visualogue-agent-packet"
PACKET_SET_FORMAT = "steam-visualogue-agent-packet-set"
STAGES = {
    "achievement-analysis",
    "editorial-curation",
    "editorial-synthesis",
    "focused-evidence",
    "artwork-inspection",
}
PACKET_SCHEMAS = {
    "achievement-analysis": "achievement-analysis-packet.schema.json",
    "editorial-curation": "editorial-curation-packet.schema.json",
    "editorial-synthesis": "editorial-synthesis-packet.schema.json",
    "focused-evidence": "focused-evidence-packet.schema.json",
    "artwork-inspection": "artwork-inspection-packet.schema.json",
}
OPAQUE_ARTIFACTS = {
    "profile": "profile.json",
    "signals": "signals.json",
    "evidence": "evidence.json",
    "visual-signals": "visual-signals.json",
    "compiled-deck": "compiled-deck.json",
}
_PRIVATE_KEYS = {"api_key", "steamid", "steam_id", "identity", "identity_input", "canonical_identity", "profile_path", "cache_path"}
_STEAMID_PATTERN = re.compile(r"(?<!\d)\d{16,20}(?!\d)")
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


def agent_work_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / ".agent-work"


def packet_dir(run_dir: str | Path) -> Path:
    return agent_work_dir(run_dir) / "packets"


def result_dir(run_dir: str | Path) -> Path:
    return agent_work_dir(run_dir) / "results"


def receipt_dir(run_dir: str | Path) -> Path:
    return agent_work_dir(run_dir) / "receipts"


def _resolve_confined(path: str | Path, root: Path) -> Path:
    """Resolve a path and reject absolute, parent, and symlink escapes."""

    candidate = Path(path)
    if candidate.is_absolute():
        raise ValueError("absolute agent artifact paths are not allowed")
    try:
        relative_parts = candidate.parts
        if ".." in relative_parts:
            raise ValueError("parent traversal in agent artifact path is not allowed")
        resolved_root = root.resolve()
        resolved = (root / candidate).resolve(strict=False)
        if not resolved.is_relative_to(resolved_root):
            raise ValueError("agent artifact path escapes its confined directory")
        # Check every existing component.  resolve() catches ordinary symlink
        # escapes; this explicit walk also gives a stable failure for a link
        # that is created between manifest construction and acceptance.
        cursor = root
        for part in relative_parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("symlink agent artifact paths are not allowed")
        return resolved
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("agent artifact path cannot be resolved safely") from exc


def confined_packet_path(run_dir: str | Path, relative_path: str) -> Path:
    root = packet_dir(run_dir)
    path = _resolve_confined(relative_path, root.parent.parent)
    if not path.is_relative_to(root.resolve()):
        raise ValueError("packet path must be inside .agent-work/packets")
    return path


def confined_result_path(run_dir: str | Path, value: str | Path) -> Path:
    root = Path(run_dir).resolve()
    candidate = Path(value)
    if ".." in candidate.parts:
        raise ValueError("agent result path cannot use parent traversal")
    if candidate.is_absolute():
        confined_candidate = candidate.relative_to(root) if candidate.is_relative_to(root) else candidate
    elif candidate.exists():
        existing = candidate.resolve()
        if not existing.is_relative_to(root):
            raise ValueError("agent result must be inside .agent-work/results")
        confined_candidate = existing.relative_to(root)
    else:
        confined_candidate = candidate
    resolved = _resolve_confined(confined_candidate, root)
    result_root = result_dir(root).resolve()
    if not resolved.is_relative_to(result_root) or resolved.is_symlink():
        raise ValueError("agent result must be inside .agent-work/results")
    return resolved


def _safe_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-.")
    return clean[:100] or "packet"


def validate_packet_privacy(payload: Any) -> None:
    """Reject credentials, identities, and absolute/local source paths."""

    def visit(value: Any, key: str = "") -> None:
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
        if normalized_key in _PRIVATE_KEYS or any(token in normalized_key for token in ("api_key", "steamid", "identity_input")):
            raise ValueError("packet contains a prohibited private field")
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str):
            if _SHA256_PATTERN.fullmatch(value):
                return
            if _STEAMID_PATTERN.search(value):
                raise ValueError("packet contains a prohibited private identifier")
            if "base64," in value[:128].casefold() or (len(value) > 512 and re.fullmatch(r"[A-Za-z0-9+/=]+", value)):
                raise ValueError("packet contains embedded base64 content")
            if Path(value).is_absolute() and ("path" in normalized_key or "file" in normalized_key):
                raise ValueError("packet contains an absolute source path")

    visit(payload)


def _report_locale(run_dir: Path) -> str:
    from .locales import load_run_config

    return str(load_run_config(run_dir)["report_locale"])


def _source_fingerprints(run_dir: Path, stage: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if stage == "artwork-inspection":
        asset_manifest = resolve_assets_dir(run_dir) / "manifest.json"
        if asset_manifest.is_file():
            result["asset_manifest"] = compute_asset_manifest_fingerprint(read_json(asset_manifest))
    stage_sources = {
        "achievement-analysis": ("evidence",),
        "editorial-curation": ("evidence",),
        "editorial-synthesis": ("evidence",),
        "focused-evidence": ("evidence",),
        "artwork-inspection": ("evidence", "visual"),
    }
    for key in stage_sources.get(stage, ("evidence",)):
        if key == "evidence":
            path = run_dir / "evidence.json"
        elif key == "visual":
            path = run_dir / "visual-signals.json"
        else:
            continue
        if path.is_file():
            # Bind semantic packets to the evidence contract fingerprint while
            # the packet and receipt still bind the exact serialized packet
            # bytes separately.
            try:
                source_document = read_json(path)
                fingerprint_key = "visual_fingerprint" if key == "visual" else "evidence_fingerprint"
                result[key] = str(source_document.get(fingerprint_key) or sha256_path(path))
            except (OSError, ValueError, json.JSONDecodeError):
                result[key] = sha256_path(path)
    if stage in {"achievement-analysis", "editorial-curation", "editorial-synthesis"}:
        locale = _report_locale(run_dir)
        result["report_locale"] = locale
        result["achievement_contract"] = achievement_analysis_contract_fingerprint(locale)
        candidate_path = agent_work_dir(run_dir) / "candidates" / "achievement-analysis.json"
        if candidate_path.is_file():
            try:
                candidate = read_json(candidate_path)
                if candidate.get("candidate_fingerprint"):
                    result["candidates"] = str(candidate["candidate_fingerprint"])
            except (OSError, ValueError, json.JSONDecodeError):
                result["candidates"] = sha256_path(candidate_path)
        if stage in {"editorial-curation", "editorial-synthesis"}:
            achievement_merge = agent_work_dir(run_dir) / "merged" / "achievement-findings.json"
            if achievement_merge.is_file():
                try:
                    document = read_json(achievement_merge)
                    if document.get("achievement_merge_fingerprint"):
                        result["achievement_merge"] = str(document["achievement_merge_fingerprint"])
                    else:
                        result["achievement_merge"] = sha256_path(achievement_merge)
                except (OSError, ValueError, json.JSONDecodeError):
                    result["achievement_merge"] = sha256_path(achievement_merge)
        if stage == "editorial-synthesis":
            curation_merge = agent_work_dir(run_dir) / "merged" / "editorial-curation-findings.json"
            if curation_merge.is_file():
                try:
                    document = read_json(curation_merge)
                    result["curation_merge"] = str(document.get("curation_merge_fingerprint") or sha256_path(curation_merge))
                except (OSError, ValueError, json.JSONDecodeError):
                    result["curation_merge"] = sha256_path(curation_merge)
    if not result and (run_dir / "profile.json").is_file():
        result["profile"] = sha256_path(run_dir / "profile.json")
    return result


def _record_id(record: Mapping[str, Any]) -> str:
    return str(record.get("id") or record.get("evidence_id") or "record")


def _write_canonical(path: Path, payload: Any) -> tuple[str, dict[str, Any]]:
    validate_packet_privacy(payload)
    data = canonical_json_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    digest = sha256_bytes(data)
    metrics = metrics_for_path(path)
    if not metrics.safe_to_dispatch:
        raise ValueError("packet was written with an unsafe budget verdict")
    return digest, metrics.as_dict()


def _packet_base(
    run_dir: Path,
    stage: str,
    packet_id: str,
    source_fingerprints: Mapping[str, str],
    *,
    completion_criterion: str,
    result_schema: str,
) -> dict[str, Any]:
    return {
        "format": PACKET_FORMAT,
        "stage": stage,
        "packet_id": packet_id,
        "run_id": _run_id_for_dir(str(run_dir.resolve())),
        "source_fingerprints": dict(source_fingerprints),
        "completion_criterion": completion_criterion,
        "result_schema": result_schema,
    }


@lru_cache(maxsize=128)
def _run_id_for_dir(directory: str) -> str:
    root = Path(directory)
    run_config_path = root / "run-config.json"
    if run_config_path.is_file():
        run_id = read_json(run_config_path).get("run_id")
        if run_id:
            return str(run_id)
    profile_path = root / "profile.json"
    if profile_path.is_file():
        run_id = read_json(profile_path).get("run_id")
        if run_id:
            return str(run_id)
    return root.name


def _validate_packet_payload(stage: str, payload: dict[str, Any]) -> None:
    from .planning import validate_schema_document

    validate_packet_privacy(payload)
    validate_schema_document("agent packet", PACKET_SCHEMAS[stage], payload)


def _packet_with_items(
    run_dir: Path,
    stage: str,
    base_name: str,
    item_groups: Sequence[Sequence[Mapping[str, Any]]],
    make_payload: Any,
    *,
    image_counts: Sequence[int] | None = None,
    pixel_counts: Sequence[int] | None = None,
    item_id_key: str = "id",
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    image_counts = image_counts or [0] * len(item_groups)
    pixel_counts = pixel_counts or [0] * len(item_groups)
    source_fingerprints = _source_fingerprints(run_dir, stage)
    for index, items in enumerate(item_groups, 1):
        packet_id = f"{_safe_name(base_name)}-{index:02d}"
        payload = make_payload(packet_id, list(items), source_fingerprints)
        _validate_packet_payload(stage, payload)
        try:
            _ = assert_packet_budget(
                payload,
                item_count=len(items),
                image_count=image_counts[index - 1],
                total_pixels=pixel_counts[index - 1],
            )
        except AgentPacketItemTooLarge:
            raise
        except BudgetViolation as exc:
            # Report only the stable first item identifier; never include the
            # source record or its text in a budget error.
            if len(items) == 1:
                item_id = str(items[0].get(item_id_key) or items[0].get("asset_id") or items[0].get("page") or packet_id)
                raise AgentPacketItemTooLarge(item_id, exc.actual, exc.limit, code=exc.code) from exc
            raise
        path = packet_dir(run_dir) / f"{packet_id}.json"
        digest, actual = _write_canonical(path, payload)
        outputs.append({
            "packet_id": packet_id,
            "path": str(path.relative_to(run_dir)).replace("\\", "/"),
            "sha256": digest,
            "utf8_bytes": actual["utf8_bytes"],
            "estimated_tokens": actual["estimated_tokens"],
            "item_count": len(items),
            "image_count": int(image_counts[index - 1]),
            "total_pixels": int(pixel_counts[index - 1]),
            "safe_to_dispatch": True,
        })
    return outputs


def _achievement_packet_payload(
    run_dir: Path,
    packet_id: str,
    items: Sequence[Mapping[str, Any]],
    source: Mapping[str, str],
) -> dict[str, Any]:
    base = _packet_base(
        run_dir,
        "achievement-analysis",
        packet_id,
        source,
        completion_criterion=ACHIEVEMENT_COMPLETION_CRITERION,
        result_schema="achievement-analysis-result.schema.json",
    )
    base.update({"games": list(items), "allowed_classifications": list(ACHIEVEMENT_ALLOWED_CLASSIFICATIONS)})
    return base


def _semantic_cache_context(
    run_dir: Path,
    cache: Any | None,
    cache_path: str | Path | None,
) -> tuple[Any, bool, str]:
    """Return a cache, ownership flag, and private identity scope."""

    owned = False
    if cache is None:
        from .cache_db import CacheDB
        from .paths import workspace_root

        selected_path = Path(cache_path) if cache_path is not None else workspace_root() / ".steam-visualogue-cache.sqlite"
        cache = CacheDB(selected_path)
        owned = True
    run_id = _run_id_for_dir(str(run_dir.resolve()))
    context = cache.get_run_identity(run_id)
    identity_scope = str(context.get("steamid")) if isinstance(context, Mapping) and context.get("steamid") else f"run:{run_id}"
    return cache, owned, identity_scope


def _cached_game_is_valid(
    candidate: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    result_schema: str = "achievement-analysis-result.schema.json",
) -> bool:
    if not isinstance(payload, Mapping) or str(payload.get("game_id")) != str(candidate.get("game_id")):
        return False
    try:
        from .planning import validate_schema_document

        wrapper = {
            "format": "steam-visualogue-agent-result",
            "stage": "achievement-analysis",
            "packet_id": "cache",
            "games": [dict(payload)],
        }
        validate_schema_document("cached achievement result", result_schema, wrapper)
    except (TypeError, ValueError):
        return False
    allowed = {
        str(item.get("evidence_id"))
        for item in candidate.get("achievements", [])
        if isinstance(item, Mapping) and item.get("evidence_id")
    }
    evidence_ids = {
        str(item)
        for item in payload.get("evidence_ids", [])
        if str(item).startswith("achievement:")
    }
    classifications = payload.get("classifications", [])
    if not isinstance(classifications, list) or any(
        str(value) not in ACHIEVEMENT_ALLOWED_CLASSIFICATIONS
        for value in classifications
    ):
        return False
    return evidence_ids.issubset(allowed) and bool(evidence_ids)


def _achievement_packet_groups(
    run_dir: Path,
    games: Sequence[Mapping[str, Any]],
    source: Mapping[str, str],
) -> list[list[Mapping[str, Any]]]:
    def make(packet_id: str, items: list[Mapping[str, Any]]) -> dict[str, Any]:
        return _achievement_packet_payload(run_dir, packet_id, items, source)

    groups: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for game in games:
        candidate = current + [game]
        try:
            assert_packet_budget(make("probe", candidate), item_count=len(candidate))
        except BudgetViolation as exc:
            if current:
                groups.append(current)
                current = []
                try:
                    assert_packet_budget(make("probe", [game]), item_count=1)
                except BudgetViolation as single:
                    raise AgentPacketItemTooLarge(
                        str(game.get("game_id", "game")),
                        single.actual,
                        single.limit,
                        code=single.code,
                    ) from single
                current = [game]
            else:
                raise AgentPacketItemTooLarge(
                    str(game.get("game_id", "game")),
                    exc.actual,
                    exc.limit,
                    code=exc.code,
                ) from exc
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups


def _achievement_packets(
    run_dir: Path,
    *,
    cache: Any | None = None,
    cache_path: str | Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    from .locales import load_run_config

    locale = load_run_config(run_dir)["report_locale"]
    artifact = build_candidate_artifact(run_dir, report_locale=locale, max_games=MAX_ACHIEVEMENT_CANDIDATE_GAMES)
    cache, owns_cache, identity_scope = _semantic_cache_context(run_dir, cache, cache_path)
    try:
        candidates = [dict(row) for row in artifact.get("selected", []) if isinstance(row, Mapping)]
        mandatory_ids = {str(value) for value in artifact.get("mandatory_game_ids", [])}

        def cache_lookup(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
            hits: dict[str, dict[str, Any]] = {}
            misses: list[dict[str, Any]] = []
            contract = str(artifact.get("analysis_contract_fingerprint"))
            evidence_locale = str(locale)
            for row in rows:
                game_id = str(row.get("game_id"))
                cached = cache.get_achievement_semantic_cache(
                    identity_scope,
                    str(row.get("game_input_fingerprint")),
                    contract,
                    evidence_locale,
                )
                if cached is not None and _cached_game_is_valid(row, cached):
                    hits[game_id] = cached
                else:
                    if cached is not None:
                        cache.delete_achievement_semantic_cache(
                            identity_scope,
                            str(row.get("game_input_fingerprint")),
                            contract,
                            evidence_locale,
                        )
                    misses.append(row)
            return hits, misses

        _, initial_misses = cache_lookup(candidates)
        source = _source_fingerprints(run_dir, "achievement-analysis")
        packet_keys = ("game_id", "canonical_name", "playtime_minutes", "completion", "coverage", "achievements")

        def packet_game(row: Mapping[str, Any]) -> dict[str, Any]:
            return {key: row[key] for key in packet_keys if key in row}

        initial_groups = _achievement_packet_groups(run_dir, [packet_game(row) for row in initial_misses], source)
        if len(initial_groups) > MAX_ACHIEVEMENT_PACKETS:
            mandatory = [row for row in candidates if str(row.get("game_id")) in mandatory_ids]
            mandatory_groups = _achievement_packet_groups(
                run_dir,
                [packet_game(row) for row in mandatory if str(row.get("game_id")) in {str(item.get("game_id")) for item in initial_misses}],
                source,
            )
            if len(mandatory_groups) > MAX_ACHIEVEMENT_PACKETS:
                raise BudgetViolation("achievement_packets", len(mandatory_groups), MAX_ACHIEVEMENT_PACKETS)
            admitted_ids = {str(row.get("game_id")) for row in mandatory}
            for row in candidates:
                game_id = str(row.get("game_id"))
                if game_id in admitted_ids:
                    continue
                proposed = [item for item in candidates if str(item.get("game_id")) in admitted_ids] + [row]
                _, proposed_misses = cache_lookup(proposed)
                try:
                    proposed_groups = _achievement_packet_groups(run_dir, [packet_game(item) for item in proposed_misses], source)
                except AgentPacketItemTooLarge:
                    continue
                if len(proposed_groups) <= MAX_ACHIEVEMENT_PACKETS:
                    admitted_ids.add(game_id)
            artifact = finalize_selected_candidates(artifact, sorted(admitted_ids, key=lambda value: int(value.removeprefix("game:"))))
            write_candidate_artifact(run_dir, artifact)
            candidates = [dict(row) for row in artifact.get("selected", []) if isinstance(row, Mapping)]
            source = _source_fingerprints(run_dir, "achievement-analysis")

        hits, misses = cache_lookup(candidates)
        groups = _achievement_packet_groups(run_dir, [packet_game(row) for row in misses], source)
        if len(groups) > MAX_ACHIEVEMENT_PACKETS:
            raise BudgetViolation("achievement_packets", len(groups), MAX_ACHIEVEMENT_PACKETS)

        def make(packet_id: str, items: list[Mapping[str, Any]], _: Mapping[str, str]) -> dict[str, Any]:
            return _achievement_packet_payload(run_dir, packet_id, items, source)

        rows = _packet_with_items(
            run_dir,
            "achievement-analysis",
            "achievement-analysis",
            groups,
            make,
            item_id_key="game_id",
        )
        details = {
            "selected_game_ids": [str(row.get("game_id")) for row in candidates],
            "cache_hit_game_ids": sorted(hits),
            "dispatched_game_ids": [str(row.get("game_id")) for row in misses],
            "selection_summary": artifact.get("summary", {}),
            "candidate_fingerprint": str(artifact.get("candidate_fingerprint")),
            "selected_set_fingerprint": str(artifact.get("selected_set_fingerprint")),
            "analysis_contract_fingerprint": str(artifact.get("analysis_contract_fingerprint")),
            "semantic_cache_scope": "private-run-cache",
            "selected_achievement_count": sum(len(row.get("achievements", [])) for row in candidates),
        }
        return groups, rows, details
    finally:
        if owns_cache:
            cache.close()


def _load_current_candidate_artifact(run_dir: Path) -> dict[str, Any]:
    from .locales import load_run_config

    path = agent_work_dir(run_dir) / "candidates" / "achievement-analysis.json"
    locale = load_run_config(run_dir)["report_locale"]
    if not path.is_file():
        return build_candidate_artifact(run_dir, report_locale=locale, max_games=MAX_ACHIEVEMENT_CANDIDATE_GAMES)
    artifact = read_json(path)
    if not candidate_artifact_is_current(run_dir, artifact, report_locale=locale):
        raise ValueError("achievement candidate artifact is stale; rebuild achievement-analysis packet set")
    return artifact


def _require_current_achievement_merge(run_dir: Path, artifact: Mapping[str, Any]) -> dict[str, Any]:
    selected_ids = {
        str(row.get("game_id"))
        for row in artifact.get("selected", [])
        if isinstance(row, Mapping) and row.get("game_id")
    }
    merge_path = agent_work_dir(run_dir) / "merged" / "achievement-findings.json"
    if not merge_path.is_file():
        if selected_ids:
            raise ValueError("editorial curation requires a current achievement merge")
        from .agent_results import ensure_empty_achievement_merge

        ensure_empty_achievement_merge(run_dir, artifact)
    document = read_json(merge_path)
    expected_source = _source_fingerprints(run_dir, "achievement-analysis")
    expected_contract = str(artifact.get("analysis_contract_fingerprint"))
    fingerprint_payload = dict(document)
    achievement_merge_fingerprint = str(fingerprint_payload.pop("achievement_merge_fingerprint", ""))
    current = (
        document.get("selected_set_fingerprint") != artifact.get("selected_set_fingerprint")
        or document.get("analysis_contract_fingerprint") != expected_contract
        or document.get("candidate_fingerprint") != artifact.get("candidate_fingerprint")
        or document.get("source_fingerprints") != expected_source
        or set(str(item) for item in document.get("selected_game_ids", [])) != selected_ids
        or not bool((document.get("coverage") or {}).get("complete"))
        or int((document.get("coverage") or {}).get("selected_game_count", -1)) != len(selected_ids)
        or int((document.get("coverage") or {}).get("covered_game_count", -1)) != len(selected_ids)
        or not achievement_merge_fingerprint.startswith("sha256:")
        or achievement_merge_fingerprint != sha256_bytes(canonical_json_bytes(fingerprint_payload))
    )
    if current and not selected_ids:
        from .agent_results import ensure_empty_achievement_merge

        ensure_empty_achievement_merge(run_dir, artifact)
        document = read_json(merge_path)
        current = False
    if current:
        raise ValueError("achievement merge is missing, incomplete, or stale")
    return document


def _curation_packets(run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    artifact = _load_current_candidate_artifact(run_dir)
    achievement_merge = _require_current_achievement_merge(run_dir, artifact)
    evidence = read_json(run_dir / "evidence.json")
    signals = read_json(run_dir / "signals.json") if (run_dir / "signals.json").is_file() else {}
    cards = [dict(item) for item in evidence.get("cards", []) if isinstance(item, Mapping)]
    cards.sort(key=lambda row: (str(row.get("type", "")), -float(row.get("strength", 0.0) or 0.0), str(row.get("id", ""))))
    cards = cards[:40]
    source = _source_fingerprints(run_dir, "editorial-curation")
    achievement_findings = achievement_merge.get("findings", []) if isinstance(achievement_merge.get("findings"), list) else []
    series = signals.get("series_groups", {}).get("value", []) if isinstance(signals.get("series_groups"), Mapping) else []
    patterns = signals.get("cross_game_patterns", {}).get("value", []) if isinstance(signals.get("cross_game_patterns"), Mapping) else []
    series = [dict(item) for item in series if isinstance(item, Mapping)][:12]
    patterns = [dict(item) for item in patterns if isinstance(item, Mapping)][:12]

    def make(packet_id: str, items: list[Mapping[str, Any]], _: Mapping[str, str]) -> dict[str, Any]:
        base = _packet_base(run_dir, "editorial-curation", packet_id, source, completion_criterion="Select up to eight evidence-grounded findings with varied evidence families and no personality overclaim.", result_schema="editorial-curation-result.schema.json")
        allowed_ids = {
            str(item.get("id"))
            for item in items
            if item.get("id")
        }
        for finding in achievement_findings:
            if isinstance(finding, Mapping):
                allowed_ids.update(
                    str(value)
                    for value in finding.get("evidence_ids", [])
                    if value
                )
        base.update({"cards": list(items), "achievement_findings": achievement_findings, "series_candidates": series, "pattern_candidates": patterns, "result_contract": {"max_findings": MAX_FINDINGS_PER_CURATION_SHARD, "allowed_evidence_ids": sorted(allowed_ids)}})
        return base
    groups: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for card in cards:
        if len(current) >= MAX_EVIDENCE_CARDS_PER_CURATION_SHARD:
            groups.append(current)
            current = []
        candidate = current + [card]
        try:
            assert_packet_budget(make("probe", candidate, source), item_count=len(candidate))
            current = candidate
        except BudgetViolation:
            if not current:
                probe = make("probe", [card], source)
                try:
                    assert_packet_budget(probe, item_count=1)
                except BudgetViolation as exc:
                    raise AgentPacketItemTooLarge(_record_id(card), exc.actual, exc.limit, code=exc.code) from exc
                raise AssertionError("unreachable")
            groups.append(current)
            current = [card]
            assert_packet_budget(make("probe", current, source), item_count=1)
    if current:
        groups.append(current)
    if not groups and (achievement_findings or series or patterns):
        # Achievement findings remain a curation input even when the global
        # evidence stream has no cards to shard.
        groups.append([])
    return groups, _packet_with_items(run_dir, "editorial-curation", "editorial-curation", groups, make)


def _synthesis_packets(run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _require_current_curation_merge(run_dir)
    from .agent_results import _finding_sort_key, _invalidate_synthesis_downstream

    _invalidate_synthesis_downstream(run_dir)
    accepted = _accepted_result_documents(run_dir, "editorial-curation")
    findings: list[dict[str, Any]] = []
    for document in accepted:
        findings.extend(item for item in document.get("findings", []) if isinstance(item, Mapping))
    deduped = {str(item.get("id")): dict(item) for item in findings if item.get("id")}
    candidates = sorted(deduped.values(), key=_finding_sort_key)[: MAX_FINAL_SEMANTIC_FINDINGS + 4]
    source = _source_fingerprints(run_dir, "editorial-synthesis")
    packet_id = "editorial-synthesis-01"
    payload = _packet_base(run_dir, "editorial-synthesis", packet_id, source, completion_criterion="Choose at most twenty non-duplicated findings for the compact semantic merge.", result_schema="editorial-synthesis-result.schema.json")
    payload.update({"candidate_findings": candidates, "max_findings": MAX_FINAL_SEMANTIC_FINDINGS})
    return [candidates], _packet_with_items(run_dir, "editorial-synthesis", "editorial-synthesis", [candidates], lambda pid, items, _: {**payload, "packet_id": pid, "candidate_findings": list(items)})


def _require_current_curation_merge(run_dir: Path) -> dict[str, Any]:
    """Require the curation merge that is downstream of achievement work."""

    artifact = _load_current_candidate_artifact(run_dir)
    achievement = _require_current_achievement_merge(run_dir, artifact)
    path = agent_work_dir(run_dir) / "merged" / "editorial-curation-findings.json"
    if not path.is_file():
        raise ValueError("editorial synthesis requires a current curation merge")
    document = read_json(path)
    expected_source = _source_fingerprints(run_dir, "editorial-curation")
    from .planning import validate_schema_document

    validate_schema_document("editorial-curation-findings.json", "semantic-findings.schema.json", document)
    fingerprint_payload = dict(document)
    fingerprint = str(fingerprint_payload.pop("curation_merge_fingerprint", ""))
    if (
        document.get("stage") != "editorial-curation"
        or document.get("source_fingerprints") != expected_source
        or document.get("achievement_merge_fingerprint") != achievement.get("achievement_merge_fingerprint")
        or not fingerprint.startswith("sha256:")
        or fingerprint != sha256_bytes(canonical_json_bytes(fingerprint_payload))
    ):
        raise ValueError("editorial curation merge is missing or stale")
    return document


def _accepted_result_documents(run_dir: Path, stage: str) -> list[dict[str, Any]]:
    manifest_path = agent_work_dir(run_dir) / "packet-sets" / f"{stage}.json"
    if manifest_path.is_file():
        from .agent_results import _accepted_receipts

        manifest = read_json(manifest_path)
        if manifest.get("source_fingerprints") != _source_fingerprints(run_dir, stage):
            raise ValueError(f"{stage} packet set is stale")
        return [document for _, document in _accepted_receipts(run_dir, stage, manifest)]
    directory = receipt_dir(run_dir) / stage
    documents: list[dict[str, Any]] = []
    if not directory.is_dir():
        return documents
    for receipt in sorted(directory.glob("*.json")):
        row = read_json(receipt)
        path = Path(row["result_path"])
        if path.is_file() and row.get("accepted") is True:
            documents.append(read_json(path))
    return documents


def _asset_images(run_dir: Path, select: Sequence[str] | None) -> list[dict[str, Any]]:
    run_dir = run_dir.resolve()
    assets_dir = resolve_assets_dir(run_dir)
    manifest_path = assets_dir / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.is_file() else {}
    records = manifest.get("assets", {}) if isinstance(manifest, Mapping) else {}
    selected = set(str(item) for item in select or [])
    exposure_pages: dict[str, list[int]] = {}
    if not selected and (run_dir / "compiled-deck.json").is_file():
        compiled_deck = read_json(run_dir / "compiled-deck.json")
        pages_list = compiled_deck.get("pages", [])
        for page in pages_list:
            if not isinstance(page, Mapping):
                continue
            page_number = int(page.get("page", 0))
            for asset_id in page.get("asset_ids", []):
                selected.add(str(asset_id))
                exposure_pages.setdefault(str(asset_id), []).append(page_number)
        if isinstance(manifest.get("opening_ribbon_asset_id"), str) and manifest["opening_ribbon_asset_id"] not in exposure_pages:
            selected.add(manifest["opening_ribbon_asset_id"])
            exposure_pages.setdefault(manifest["opening_ribbon_asset_id"], []).append(1)
        if isinstance(manifest.get("closing_ribbon_asset_id"), str) and manifest["closing_ribbon_asset_id"] not in exposure_pages:
            selected.add(manifest["closing_ribbon_asset_id"])
            exposure_pages.setdefault(manifest["closing_ribbon_asset_id"], []).append(len(pages_list) if pages_list else 15)
    rows: list[dict[str, Any]] = []
    for asset_id in sorted(selected):
        record = records.get(asset_id) if isinstance(records, Mapping) else None
        if not isinstance(record, Mapping) or record.get("status") != "ready" or not record.get("path"):
            continue
        path = (assets_dir / str(record["path"])).resolve()
        if not path.is_file() or not path.is_relative_to(assets_dir):
            continue
        try:
            from PIL import Image

            with Image.open(path) as image:
                width, height = image.size
        except (OSError, RuntimeError):
            continue
        target_roles = ["Hero Game", "Hook Cover", "Abstract Portrait", "Closing Note"]
        if asset_id.startswith("game:") and asset_id.endswith(":portrait"):
            target_roles.append("Atlas Portrait")
        rows.append({"asset_id": asset_id, "path": artwork_reference(run_dir, path), "width": width, "height": height, "aspect_ratio": round(width / height, 6), "target_roles": target_roles, "exposure_count": len(exposure_pages.get(asset_id, [])), "exposure_pages": sorted(exposure_pages.get(asset_id, []))})
    if selected - {str(row["asset_id"]) for row in rows}:
        missing = ", ".join(sorted(selected - {str(row["asset_id"]) for row in rows}))
        raise ValueError("selected artwork is not materialized: " + missing)
    return rows


def _artwork_packets(run_dir: Path, select: Sequence[str] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    images = _asset_images(run_dir, select)
    if len(images) <= MAX_IMAGES_PER_PACKET:
        groups = [images] if images else []
    else:
        # Balance the tail so every multi-packet shortlist remains within the
        # documented 4–8 artwork range.
        packet_count = (len(images) + MAX_IMAGES_PER_PACKET - 1) // MAX_IMAGES_PER_PACKET
        base, remainder = divmod(len(images), packet_count)
        groups = []
        cursor = 0
        for index in range(packet_count):
            size = base + (1 if index < remainder else 0)
            groups.append(images[cursor:cursor + size])
            cursor += size
    source = _source_fingerprints(run_dir, "artwork-inspection")

    def make(packet_id: str, items: list[Mapping[str, Any]], _: Mapping[str, str]) -> dict[str, Any]:
        base = _packet_base(run_dir, "artwork-inspection", packet_id, source, completion_criterion="Inspect crop safety, small-size legibility, tone, and geometry for each bounded asset.", result_schema="artwork-inspection-result.schema.json")
        base.update({"images": list(items), "questions": ["Is the crop safe for the target role?", "Does the image remain legible at page scale?", "What tone and dominant geometry does it contribute?"]})
        return base
    counts = [len(group) for group in groups]
    pixels = [sum(int(item["width"]) * int(item["height"]) for item in group) for group in groups]
    return groups, _packet_with_items(run_dir, "artwork-inspection", "artwork-inspection", groups, make, image_counts=counts, pixel_counts=pixels, item_id_key="asset_id")


def _focused_packet(run_dir: Path, evidence_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from .evidence import evidence_catalog

    evidence = read_json(run_dir / "evidence.json")
    catalog = evidence_catalog(evidence, include_cards=True) if isinstance(evidence, Mapping) else {}
    if evidence_id not in catalog:
        raise ValueError(f"unknown evidence id: {evidence_id}")
    source = _source_fingerprints(run_dir, "focused-evidence")
    packet_id = "focused-evidence-01"
    payload = _packet_base(run_dir, "focused-evidence", packet_id, source, completion_criterion="Answer only the focused evidence lookup without expanding the source scope.", result_schema="focused-evidence-result.schema.json")
    payload.update({"evidence_id": evidence_id, "record": catalog[evidence_id]})
    path = packet_dir(run_dir) / f"{packet_id}.json"
    _validate_packet_payload("focused-evidence", payload)
    try:
        assert_packet_budget(payload, item_count=1)
    except BudgetViolation as exc:
        raise AgentPacketItemTooLarge(evidence_id, exc.actual, exc.limit, code=exc.code) from exc
    digest, metrics = _write_canonical(path, payload)
    return [payload], [{"packet_id": packet_id, "path": str(path.relative_to(run_dir)).replace("\\", "/"), "sha256": digest, "utf8_bytes": metrics["utf8_bytes"], "estimated_tokens": metrics["estimated_tokens"], "item_count": 1, "image_count": 0, "total_pixels": 0, "safe_to_dispatch": True}]


def _write_manifest(
    run_dir: Path,
    stage: str,
    rows: list[dict[str, Any]],
    details: Mapping[str, Any] | None = None,
) -> Path:
    manifest_path = agent_work_dir(run_dir) / "packet-sets" / f"{stage}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    source = _source_fingerprints(run_dir, stage)
    run_id = str(read_json(run_dir / "profile.json").get("run_id") or run_dir.name) if (run_dir / "profile.json").is_file() else run_dir.name
    safe_to_dispatch = all(row.get("safe_to_dispatch") is True for row in rows)
    if stage == "achievement-analysis" and len(rows) > MAX_ACHIEVEMENT_PACKETS:
        safe_to_dispatch = False
    payload = {
        "format": PACKET_SET_FORMAT,
        "run_id": run_id,
        "stage": stage,
        "source_fingerprints": source,
        "safe_to_dispatch": safe_to_dispatch,
        **dict(details or {}),
        "packets": rows,
    }
    from .planning import validate_schema_document

    validate_schema_document("agent packet set", "agent-packet-set.schema.json", payload)
    data = canonical_json_bytes(payload)
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, manifest_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return manifest_path


def build_packet_set(
    run_dir: str | Path,
    stage: str,
    *,
    evidence_id: str | None = None,
    select: Sequence[str] | None = None,
    cache: Any | None = None,
    cache_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(run_dir)
    if stage not in STAGES:
        raise ValueError(f"unsupported packet stage: {stage}")
    packet_dir(root).mkdir(parents=True, exist_ok=True)
    details: dict[str, Any] = {}
    if stage == "achievement-analysis":
        _, rows, details = _achievement_packets(root, cache=cache, cache_path=cache_path)
    elif stage == "editorial-curation":
        _, rows = _curation_packets(root)
    elif stage == "editorial-synthesis":
        _, rows = _synthesis_packets(root)
    elif stage == "focused-evidence":
        if not evidence_id:
            raise ValueError("focused-evidence requires --evidence-id")
        _, rows = _focused_packet(root, evidence_id)
    elif stage == "artwork-inspection":
        _, rows = _artwork_packets(root, select)
    else:
        raise ValueError(f"unsupported generic packet stage: {stage}")
    path = _write_manifest(root, stage, rows, details)
    manifest = read_json(path)
    return {"status": "ready", "stage": stage, "packet_set": str(path), "packet_count": len(rows), "safe_to_dispatch": bool(manifest.get("safe_to_dispatch"))}


def build_visual_brief(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    required = require_files(root, ["compiled-deck.json", "evidence.json", "visual-signals.json"])
    compiled = read_json(required["compiled-deck.json"])
    visual = read_json(required["visual-signals.json"])
    evidence = read_json(required["evidence.json"])
    asset_manifest = read_json(resolve_assets_dir(root) / "manifest.json")
    from .planning import validate_schema_document

    validate_schema_document("compiled-deck.json", "compiled-deck.schema.json", compiled)
    validate_schema_document("visual-signals.json", "visual-signals.schema.json", visual)
    expected_visual_fingerprint = compute_visual_fingerprint(visual)
    if visual.get("visual_fingerprint") != expected_visual_fingerprint:
        raise ValueError("visual signals are stale")
    if visual.get("evidence_fingerprint") != evidence.get("evidence_fingerprint"):
        raise ValueError("visual signals do not match evidence")
    asset_manifest_fingerprint = compute_asset_manifest_fingerprint(asset_manifest)
    candidates = _asset_images(root, None)
    accepted = []
    artwork_manifest_path = agent_work_dir(root) / "packet-sets" / "artwork-inspection.json"
    artwork_receipts_path = receipt_dir(root) / "artwork-inspection"
    if artwork_manifest_path.is_file() and artwork_receipts_path.is_dir():
        from .agent_results import _accepted_receipts

        artwork_manifest = read_json(artwork_manifest_path)
        if artwork_manifest.get("source_fingerprints") != _source_fingerprints(root, "artwork-inspection"):
            raise ValueError("artwork inspection packet set is stale")
        for _, document in _accepted_receipts(root, "artwork-inspection", artwork_manifest):
            accepted.extend(item for item in document.get("images", []) if isinstance(item, Mapping))
    payload = {
        "evidence_fingerprint": str(visual.get("evidence_fingerprint") or evidence.get("evidence_fingerprint") or ""),
        "visual_fingerprint": visual.get("visual_fingerprint"),
        "compiled_deck_fingerprint": str(compiled.get("compiled_deck_fingerprint") or ""),
        "asset_manifest_fingerprint": asset_manifest_fingerprint,
        "library_palette": visual.get("library_palette", {}),
        "comparison_palette": visual.get("breadth_palette", {}),
        "sampling": visual.get("sampling", {}),
        "confidence": visual.get("confidence", "low"),
        "failure_count": len(visual.get("failures", [])) if isinstance(visual.get("failures"), list) else 0,
        "candidate_assets": candidates,
        "accepted_inspections": accepted[:32],
        "deck_policy": {
            "max_pages_per_game": 2,
            "max_pages_per_asset": 1,
            "min_page_gap_for_repeated_game": 2,
        },
        "role_contracts": {
            "opening": {"encoding_kind": "editorial-opening", "content": "Orient the reader to the deck question and stakes."},
            "hero": {"encoding_kind": "single-subject-anchor", "content": "Give one worthwhile claim a clear visual anchor."},
            "archive-density": {"encoding_kind": "archive-density", "content": "Show scale, concentration, or breadth without a decorative gallery."},
            "evidence-ledger": {"encoding_kind": "evidence-ledger", "content": "Let evidence rows carry a claim about the selected set."},
            "quantitative-comparison": {"encoding_kind": "quantitative-comparison", "content": "Make a shared measure and its magnitude legible."},
            "qualitative-comparison": {"encoding_kind": "qualitative-comparison", "content": "Compare parallel item statements on a shared question."},
            "series-atlas": {"encoding_kind": "series-atlas", "content": "Reveal a meaningful sequence or recurrence using game:<appid>:portrait assets."},
            "pattern-atlas": {"encoding_kind": "pattern-atlas", "content": "Reveal a repeated pattern across subjects using game:<appid>:portrait assets."},
            "temporal-strata": {"encoding_kind": "temporal-strata", "content": "Show a meaningful change across time."},
            "achievement-anomaly": {"encoding_kind": "achievement-anomaly", "content": "Surface a consequential outlier or anomaly."},
            "abstract-portrait": {"encoding_kind": "abstract-portrait", "content": "Use abstraction only when it carries the page claim."},
            "closing": {"encoding_kind": "editorial-closing", "content": "Close with a new synthesis that answers the deck question."},
        },
    }
    payload["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(payload)
    # A visual brief is a merge artifact: it is Agent-readable but bounded by
    # the larger merge limit, not the per-assignment payload limit.
    from .context_budget import assert_merge_budget

    assert_merge_budget(payload)
    validate_packet_privacy(payload)
    validate_schema_document("visual-brief.json", "visual-brief.schema.json", payload)
    path = root / "visual-brief.json"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(canonical_json_bytes(payload))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"status": "ok", "artifact": str(root / "visual-brief.json"), "candidate_assets": len(candidates), "accepted_inspections": len(accepted)}


__all__ = [
    "OPAQUE_ARTIFACTS",
    "STAGES",
    "agent_work_dir",
    "build_packet_set",
    "build_visual_brief",
    "confined_packet_path",
    "confined_result_path",
    "packet_dir",
    "receipt_dir",
    "result_dir",
    "validate_packet_privacy",
]
