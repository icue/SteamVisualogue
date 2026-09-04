"""Accept, bind, and merge bounded Agent result files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .agent_packets import (
    STAGES,
    _cached_game_is_valid,
    _semantic_cache_context,
    _source_fingerprints,
    agent_work_dir,
    confined_packet_path,
    confined_result_path,
    receipt_dir,
    validate_packet_privacy,
)
from .context_budget import (
    MAX_FINAL_SEMANTIC_FINDINGS,
    RESULT_MAX_UTF8_BYTES,
    assert_merge_budget,
    metrics_for_path,
    sha256_path,
    sha256_bytes,
    canonical_json_bytes,
)
from .io_utils import read_json, write_json
from .planning import validate_schema_document
from .semantic_candidates import (
    achievement_game_input_fingerprint,
    candidate_artifact_is_current,
)


RESULT_FORMAT = "steam-visualogue-agent-result"
RECEIPT_FORMAT = "steam-visualogue-agent-result-receipt"


RESULT_SCHEMA = {
    "achievement-analysis": "achievement-analysis-result.schema.json",
    "editorial-curation": "editorial-curation-result.schema.json",
    "editorial-synthesis": "editorial-synthesis-result.schema.json",
    "focused-evidence": "focused-evidence-result.schema.json",
    "artwork-inspection": "artwork-inspection-result.schema.json",
}


def _manifest_path(run_dir: Path, value: str | Path) -> Path:
    run_root = run_dir.resolve()
    root = (agent_work_dir(run_root) / "packet-sets").resolve()
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("packet-set manifest cannot use parent traversal")
    if not path.is_absolute():
        # CLI output commonly passes ``run/.agent-work/...`` while API users
        # pass ``.agent-work/...``.  Resolve both forms without ever treating
        # an arbitrary path outside this run as a valid manifest.
        if path.exists() and path.resolve().is_relative_to(run_root):
            path = path.resolve()
        else:
            path = run_root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("packet-set manifest must be inside .agent-work/packet-sets")
    if not resolved.is_file():
        raise FileNotFoundError("packet-set manifest is missing")
    return resolved


def _packet_reference_sets(packet: Mapping[str, Any]) -> dict[str, set[str]]:
    references = {"evidence": set(), "game": set(), "achievement": set(), "asset": set(), "page": set()}

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if not isinstance(value, (str, int)):
            return
        text = str(value)
        if key in {"evidence_id", "evidence_ids", "allowed_evidence_ids"} or text.startswith(("metric:", "pattern:", "achievement:", "game:")):
            references["evidence"].add(text)
        if key in {"game_id", "game_ids"} or text.startswith("game:"):
            references["game"].add(text)
        if key in {"achievement_id", "achievement_ids"} or text.startswith("achievement:"):
            references["achievement"].add(text)
        if key in {"asset_id", "asset_ids"} or text.startswith("game:") or text.startswith("generated:"):
            references["asset"].add(text)
        if key in {"page", "page_id", "page_ids", "reviewed_pages"} and text.isdigit():
            references["page"].add(text)

    visit(packet)
    return references


def _result_references(result: Mapping[str, Any]) -> dict[str, set[str]]:
    return _packet_reference_sets(result)


def _assert_result_closure(packet: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    allowed = _packet_reference_sets(packet)
    actual = _result_references(result)
    errors: list[str] = []
    for kind in allowed:
        # packet id, stage and free-text claim strings are intentionally not
        # treated as references; only structured ID-shaped fields reach here.
        unsupported = actual[kind] - allowed[kind]
        if unsupported:
            errors.append(f"{kind}: {', '.join(sorted(unsupported))}")
    if errors:
        raise ValueError("result references outside packet closure: " + "; ".join(errors))


def _packet_for(manifest: Mapping[str, Any], packet_id: str) -> Mapping[str, Any]:
    for packet in manifest.get("packets", []) if isinstance(manifest.get("packets"), list) else []:
        if isinstance(packet, Mapping) and packet.get("packet_id") == packet_id:
            return packet
    raise ValueError(f"unknown packet id: {packet_id}")


def _current_manifest(run_dir: Path, value: str | Path, stage: str) -> tuple[Path, dict[str, Any]]:
    path = _manifest_path(run_dir, value)
    manifest = read_json(path)
    validate_schema_document("agent packet set", "agent-packet-set.schema.json", manifest)
    if manifest.get("stage") != stage or manifest.get("safe_to_dispatch") is not True:
        raise ValueError("packet-set manifest is not safe for this stage")
    return path, manifest


def _achievement_result_games(
    packet: Mapping[str, Any],
    result: Mapping[str, Any],
    packet_id: str,
) -> tuple[dict[str, Mapping[str, Any]], list[Mapping[str, Any]]]:
    packet_games = {
        str(game.get("game_id")): game
        for game in packet.get("games", [])
        if isinstance(game, Mapping) and game.get("game_id")
    }
    result_rows = [
        game
        for game in result.get("games", [])
        if isinstance(game, Mapping)
    ] if isinstance(result.get("games"), list) else []
    result_ids = [str(game.get("game_id")) for game in result_rows]
    if (
        len(result_ids) != len(set(result_ids))
        or set(result_ids) != set(packet_games)
        or not result_ids
    ):
        raise ValueError(f"achievement result coverage does not match packet {packet_id}")
    return packet_games, result_rows


def _cache_accepted_achievement_games(
    root: Path,
    manifest: Mapping[str, Any],
    packet: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    cache: Any | None,
    cache_path: str | Path | None,
) -> None:
    """Persist per-game rows only after the whole result has passed validation."""

    packet_games, result_rows = _achievement_result_games(
        packet,
        result,
        str(result.get("packet_id") or "packet"),
    )
    from .locales import load_run_config

    locale = load_run_config(root)["report_locale"]
    source = manifest.get("source_fingerprints", {}) if isinstance(manifest.get("source_fingerprints"), Mapping) else {}
    contract_fingerprint = str(source.get("achievement_contract") or "")
    if not str(source.get("evidence") or "") or not contract_fingerprint:
        raise ValueError("achievement packet is missing cache contract fingerprints")
    selected_contract = str(manifest.get("analysis_contract_fingerprint") or contract_fingerprint)
    if selected_contract != contract_fingerprint:
        raise ValueError("achievement packet contract fingerprint is inconsistent")
    candidate_artifact = _candidate_artifact_for_merge(root)
    candidate_by_game = {
        str(row.get("game_id")): row
        for row in candidate_artifact.get("selected", [])
        if isinstance(row, Mapping) and row.get("game_id")
    }
    cache, owns_cache, identity_scope = _semantic_cache_context(root, cache, cache_path)
    try:
        rows_to_store: list[tuple[str, Mapping[str, Any]]] = []
        for row in result_rows:
            candidate = packet_games[str(row.get("game_id"))]
            candidate_ledger = candidate_by_game.get(str(row.get("game_id")))
            if candidate_ledger is None:
                raise ValueError(f"achievement candidate ledger is missing {row.get('game_id')}")
            if not _cached_game_is_valid(candidate, row):
                raise ValueError(f"achievement result for {row.get('game_id')} cannot be cached")
            referenced_evidence_fingerprint = str(candidate_ledger.get("referenced_evidence_fingerprint") or "")
            expected_game_input = str(candidate_ledger.get("game_input_fingerprint") or "")
            if not referenced_evidence_fingerprint or not expected_game_input:
                raise ValueError(f"achievement candidate cache identity is incomplete for {row.get('game_id')}")
            game_input = achievement_game_input_fingerprint(
                candidate,
                evidence_fingerprint=referenced_evidence_fingerprint,
            )
            if game_input != expected_game_input:
                raise ValueError(f"achievement candidate cache identity is stale for {row.get('game_id')}")
            rows_to_store.append((game_input, row))
        for game_input, row in rows_to_store:
            cache.upsert_achievement_semantic_cache(
                identity_scope,
                game_input,
                contract_fingerprint,
                locale,
                row,
            )
    finally:
        if owns_cache:
            cache.close()


def accept_agent_result(
    run_dir: str | Path,
    packet_set: str | Path,
    packet_id: str,
    result: str | Path,
    *,
    cache: Any | None = None,
    cache_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(run_dir)
    manifest_path = _manifest_path(root, packet_set)
    manifest_document = read_json(manifest_path)
    manifest_path, manifest = _current_manifest(root, manifest_path, str(manifest_document.get("stage")))
    expected_sources = _source_fingerprints(root, str(manifest.get("stage")))
    if manifest.get("source_fingerprints") != expected_sources:
        raise ValueError("packet-set source fingerprints are stale")
    packet_row = _packet_for(manifest, packet_id)
    packet_path = confined_packet_path(root, str(packet_row.get("path")))
    packet_metrics = metrics_for_path(
        packet_path,
        item_count=int(packet_row.get("item_count", 0)),
        image_count=int(packet_row.get("image_count", 0)),
        total_pixels=int(packet_row.get("total_pixels", 0)),
    )
    if sha256_path(packet_path) != packet_row.get("sha256") or packet_metrics.utf8_bytes != int(packet_row.get("utf8_bytes", -1)) or packet_metrics.estimated_tokens != int(packet_row.get("estimated_tokens", -1)):
        raise ValueError("packet hash does not match packet-set manifest")
    packet = read_json(packet_path)
    stage = str(manifest.get("stage"))
    result_path = confined_result_path(root, result)
    if not result_path.is_file():
        raise FileNotFoundError("agent result is missing")
    metrics = metrics_for_path(result_path, result=True)
    if not metrics.safe_to_dispatch:
        raise ValueError(
            f"agent result exceeds the {RESULT_MAX_UTF8_BYTES // 1024} KiB result budget"
        )
    document = read_json(result_path)
    validate_packet_privacy(document)
    expected_schema = RESULT_SCHEMA[stage]
    validate_schema_document("agent result", expected_schema, document)
    if document.get("packet_id") != packet_id or document.get("stage") != stage:
        raise ValueError("agent result packet binding does not match the packet-set")
    _assert_result_closure(packet, document)
    if document.get("source_fingerprints") and document.get("source_fingerprints") != manifest.get("source_fingerprints"):
        raise ValueError("agent result source fingerprints do not match packet set")
    if stage == "achievement-analysis":
        _cache_accepted_achievement_games(
            root,
            manifest,
            packet,
            document,
            cache=cache,
            cache_path=cache_path,
        )
    receipt = {
        "format": RECEIPT_FORMAT,
        "stage": stage,
        "packet_id": packet_id,
        "packet_sha256": packet_row.get("sha256"),
        "result_path": str(result_path.resolve()),
        "result_sha256": sha256_path(result_path),
        "result_schema": expected_schema,
        "result_utf8_bytes": metrics.utf8_bytes,
        "references_valid": True,
        "source_fingerprints": dict(manifest.get("source_fingerprints", {})),
        "accepted": True,
        "failure_code": None,
    }
    destination = receipt_dir(root) / stage / f"{packet_id}.json"
    validate_schema_document("agent result receipt", "agent-result-receipt.schema.json", receipt)
    write_json(destination, receipt)
    return {"status": "accepted", "receipt": str(destination)}


def _accepted_receipts(run_dir: Path, stage: str, manifest: Mapping[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    directory = receipt_dir(run_dir) / stage
    expected = {str(row.get("packet_id")): row for row in manifest.get("packets", []) if isinstance(row, Mapping)}
    if not expected:
        return []
    if not directory.is_dir():
        raise ValueError(f"no accepted results for {stage}")
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for packet_id in sorted(expected):
        path = directory / f"{packet_id}.json"
        if not path.is_file():
            raise ValueError(f"missing accepted result for packet {packet_id}")
        receipt = read_json(path)
        if (
            receipt.get("accepted") is not True
            or receipt.get("stage") != stage
            or receipt.get("packet_id") != packet_id
            or receipt.get("source_fingerprints") != manifest.get("source_fingerprints")
            or receipt.get("result_schema") != RESULT_SCHEMA[stage]
        ):
            raise ValueError(f"invalid receipt for packet {packet_id}")
        validate_schema_document("agent result receipt", "agent-result-receipt.schema.json", receipt)
        packet_path = confined_packet_path(run_dir, str(expected[packet_id].get("path")))
        if sha256_path(packet_path) != expected[packet_id].get("sha256") or sha256_path(packet_path) != receipt.get("packet_sha256"):
            raise ValueError(f"packet for accepted result {packet_id} was changed")
        result_path = confined_result_path(run_dir, str(receipt.get("result_path", "")))
        metrics = metrics_for_path(result_path, result=True)
        if not result_path.is_file() or not metrics.safe_to_dispatch or sha256_path(result_path) != receipt.get("result_sha256") or metrics.utf8_bytes != receipt.get("result_utf8_bytes"):
            raise ValueError(f"accepted result for packet {packet_id} was changed")
        if sha256_path(packet_path) != receipt.get("packet_sha256"):
            raise ValueError(f"packet for accepted result {packet_id} was changed")
        packet = read_json(packet_path)
        current = dict(read_json(result_path))
        validate_schema_document("agent result", RESULT_SCHEMA[stage], current)
        if current.get("stage") != stage or current.get("packet_id") != packet_id:
            raise ValueError(f"accepted result binding for packet {packet_id} was changed")
        _assert_result_closure(packet, current)
        if stage == "achievement-analysis":
            _achievement_result_games(packet, current, packet_id)
        rows.append((receipt, current))
    if len(rows) != len(expected):
        raise ValueError("accepted result coverage is incomplete")
    return rows


def _write_merge(root: Path, name: str, document: dict[str, Any]) -> dict[str, Any]:
    if name in {"semantic-findings.json", "achievement-findings.json", "editorial-curation-findings.json"}:
        validate_schema_document("merge artifact", "semantic-findings.schema.json", document)
    assert_merge_budget(document)
    destination = root / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(canonical_json_bytes(document))
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    metrics = metrics_for_path(destination, merge=True)
    if not metrics.safe_to_dispatch:
        raise ValueError("merge artifact exceeds the 24 KiB limit")
    return {"status": "merged", "artifact": str(destination), "bytes": metrics.utf8_bytes}


def _invalidate_semantic_downstream(root: Path) -> None:
    """Drop internal downstream handoff state after a new achievement merge."""

    base = agent_work_dir(root)
    for stage in ("editorial-curation", "editorial-synthesis"):
        (base / "packet-sets" / f"{stage}.json").unlink(missing_ok=True)
        packet_directory = base / "packets"
        if packet_directory.is_dir():
            for path in packet_directory.glob(f"{stage}-*.json"):
                path.unlink(missing_ok=True)
        receipt_directory = base / "receipts" / stage
        if receipt_directory.is_dir():
            for path in receipt_directory.glob("*.json"):
                try:
                    receipt = read_json(path)
                    result_path = confined_result_path(root, str(receipt.get("result_path", "")))
                    result_path.unlink(missing_ok=True)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
                path.unlink(missing_ok=True)
        result_directory = base / "results"
        if result_directory.is_dir():
            for path in result_directory.glob(f"{stage}-*.json"):
                path.unlink(missing_ok=True)
    (base / "merged" / "editorial-curation-findings.json").unlink(missing_ok=True)
    (root / "semantic-findings.json").unlink(missing_ok=True)


def _finding_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    confidence = {"high": 3, "medium": 2, "low": 1}.get(str(item.get("confidence")), 0)
    evidence_count = len({str(value) for value in item.get("evidence_ids", []) if value}) if isinstance(item.get("evidence_ids"), list) else 0
    return (-confidence, -evidence_count, str(item.get("type", "")), str(item.get("id", "")))


def _candidate_artifact_for_merge(root: Path) -> dict[str, Any]:
    path = agent_work_dir(root) / "candidates" / "achievement-analysis.json"
    if not path.is_file():
        raise ValueError("achievement candidate artifact is missing")
    artifact = read_json(path)
    from .locales import load_run_config

    if not candidate_artifact_is_current(root, artifact, report_locale=load_run_config(root)["report_locale"]):
        raise ValueError("achievement candidate artifact is stale")
    return artifact


def _achievement_merge_document(
    root: Path,
    manifest: Mapping[str, Any],
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    cache: Any | None = None,
    cache_path: str | Path | None = None,
) -> dict[str, Any]:
    artifact = _candidate_artifact_for_merge(root)
    selected = {
        str(row.get("game_id")): row
        for row in artifact.get("selected", [])
        if isinstance(row, Mapping) and row.get("game_id")
    }
    expected_selected_fingerprint = str(artifact.get("selected_set_fingerprint"))
    expected_contract_fingerprint = str(artifact.get("analysis_contract_fingerprint"))
    expected_candidate_fingerprint = str(artifact.get("candidate_fingerprint"))
    if (
        str(manifest.get("selected_set_fingerprint")) != expected_selected_fingerprint
        or str(manifest.get("analysis_contract_fingerprint")) != expected_contract_fingerprint
        or str(manifest.get("candidate_fingerprint")) != expected_candidate_fingerprint
        or int(manifest.get("selected_achievement_count", -1))
        != sum(len(row.get("achievements", [])) for row in selected.values())
    ):
        raise ValueError("achievement packet set fingerprints or counts are stale")
    selected_ids = set(str(value) for value in manifest.get("selected_game_ids", []))
    if selected_ids != set(selected):
        raise ValueError("achievement packet set selected-game coverage is stale")
    cache_hit_ids = set(str(value) for value in manifest.get("cache_hit_game_ids", []))
    dispatched_ids = set(str(value) for value in manifest.get("dispatched_game_ids", []))
    if cache_hit_ids & dispatched_ids or cache_hit_ids | dispatched_ids != selected_ids:
        raise ValueError("achievement packet set cache and dispatch coverage is incomplete")

    result_by_game: dict[str, Mapping[str, Any]] = {}
    for receipt, result in rows:
        packet = read_json(confined_packet_path(root, str(_packet_for(manifest, str(receipt.get("packet_id"))).get("path"))))
        _, result_rows = _achievement_result_games(packet, result, str(receipt.get("packet_id")))
        for game in result_rows:
            game_id = str(game.get("game_id"))
            if game_id in result_by_game:
                raise ValueError("achievement results duplicate game coverage")
            result_by_game[game_id] = game
    if set(result_by_game) != dispatched_ids:
        raise ValueError("accepted achievement results do not cover dispatched games")

    from .locales import load_run_config

    locale = load_run_config(root)["report_locale"]
    cache, owns_cache, identity_scope = _semantic_cache_context(root, cache, cache_path)
    try:
        for game_id in sorted(cache_hit_ids, key=lambda value: int(value.removeprefix("game:"))):
            candidate = selected.get(game_id)
            if candidate is None:
                raise ValueError(f"achievement cache game is not selected: {game_id}")
            cached = cache.get_achievement_semantic_cache(
                identity_scope,
                str(candidate.get("game_input_fingerprint")),
                str(artifact.get("analysis_contract_fingerprint")),
                locale,
            )
            if cached is None or not _cached_game_is_valid(candidate, cached):
                raise ValueError(f"achievement cache coverage is stale for {game_id}")
            result_by_game[game_id] = cached
    finally:
        if owns_cache:
            cache.close()

    if set(result_by_game) != selected_ids:
        raise ValueError("achievement merge coverage is incomplete")

    findings: list[dict[str, Any]] = []
    for game_id in sorted(result_by_game, key=lambda value: int(value.removeprefix("game:"))):
        game = result_by_game[game_id]
        safe_claims = game.get("safe_claims", []) if isinstance(game.get("safe_claims"), list) else []
        evidence_ids = sorted({
            str(item)
            for item in game.get("evidence_ids", [])
            if str(item).startswith(("achievement:", "game:"))
        }) if isinstance(game.get("evidence_ids"), list) else []
        if not evidence_ids:
            continue
        for index, claim in enumerate(safe_claims):
            if not isinstance(claim, str) or not claim.strip():
                continue
            findings.append(
                {
                    "id": f"achievement-analysis:{game_id}:{index + 1}",
                    "type": "achievement-semantic",
                    "claim": claim.strip(),
                    "evidence_ids": evidence_ids,
                    "confidence": "medium",
                }
            )
    deduped = {str(item["id"]): item for item in findings}
    bounded = sorted(deduped.values(), key=_finding_sort_key)[:MAX_FINAL_SEMANTIC_FINDINGS]
    source = dict(manifest.get("source_fingerprints", {}))
    coverage = {
        "complete": True,
        "selected_game_count": len(selected_ids),
        "covered_game_count": len(result_by_game),
        "cache_hit_game_count": len(cache_hit_ids),
        "dispatched_game_count": len(dispatched_ids),
        "selected_achievement_count": int(manifest.get("selected_achievement_count", 0) or 0),
    }
    document = {
        "stage": "achievement-analysis",
        "selected_set_fingerprint": str(manifest.get("selected_set_fingerprint") or artifact.get("selected_set_fingerprint")),
        "analysis_contract_fingerprint": str(manifest.get("analysis_contract_fingerprint") or artifact.get("analysis_contract_fingerprint")),
        "candidate_fingerprint": str(manifest.get("candidate_fingerprint") or artifact.get("candidate_fingerprint")),
        "source_fingerprints": source,
        "selected_game_ids": sorted(selected_ids, key=lambda value: int(value.removeprefix("game:"))),
        "coverage": coverage,
        "findings": bounded,
    }
    fingerprint_payload = dict(document)
    fingerprint_payload.pop("achievement_merge_fingerprint", None)
    from .context_budget import sha256_bytes

    document["achievement_merge_fingerprint"] = sha256_bytes(canonical_json_bytes(fingerprint_payload))
    return document


def _merge_achievement(
    root: Path,
    manifest: Mapping[str, Any],
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    cache: Any | None = None,
    cache_path: str | Path | None = None,
) -> dict[str, Any]:
    document = _achievement_merge_document(
        root,
        manifest,
        rows,
        cache=cache,
        cache_path=cache_path,
    )
    result = _write_merge(root / ".agent-work" / "merged", "achievement-findings.json", document)
    _invalidate_semantic_downstream(root)
    return result


def ensure_empty_achievement_merge(root: Path, artifact: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Create the explicit current empty dependency state for a no-candidate run."""

    if artifact is None:
        artifact = _candidate_artifact_for_merge(root)
    selected_ids = [str(row.get("game_id")) for row in artifact.get("selected", []) if isinstance(row, Mapping) and row.get("game_id")]
    if selected_ids:
        raise ValueError("cannot create an empty achievement merge for selected candidates")
    source = _source_fingerprints(root, "achievement-analysis")
    base = {
        "stage": "achievement-analysis",
        "selected_set_fingerprint": str(artifact.get("selected_set_fingerprint")),
        "analysis_contract_fingerprint": str(artifact.get("analysis_contract_fingerprint")),
        "candidate_fingerprint": str(artifact.get("candidate_fingerprint")),
        "source_fingerprints": source,
        "selected_game_ids": [],
        "coverage": {
            "complete": True,
            "selected_game_count": 0,
            "covered_game_count": 0,
            "cache_hit_game_count": 0,
            "dispatched_game_count": 0,
            "selected_achievement_count": 0,
        },
        "findings": [],
    }
    document = dict(base)
    from .context_budget import sha256_bytes

    document["achievement_merge_fingerprint"] = sha256_bytes(canonical_json_bytes(base))
    result = _write_merge(root / ".agent-work" / "merged", "achievement-findings.json", document)
    _invalidate_semantic_downstream(root)
    return result


def _assert_artwork_coverage(root: Path, manifest: Mapping[str, Any], rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
    seen: set[str] = set()
    for receipt, result in rows:
        packet = read_json(confined_packet_path(root, str(_packet_for(manifest, str(receipt.get("packet_id"))).get("path"))))
        expected = {
            str(image.get("asset_id"))
            for image in packet.get("images", [])
            if isinstance(image, Mapping) and image.get("asset_id")
        }
        actual = [
            str(image.get("asset_id"))
            for image in result.get("images", [])
            if isinstance(image, Mapping) and image.get("asset_id")
        ]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError(f"artwork result coverage does not match packet {receipt.get('packet_id')}")
        if seen.intersection(actual):
            raise ValueError("artwork results duplicate asset coverage")
        seen.update(actual)


def _invalidate_synthesis_downstream(root: Path) -> None:
    base = agent_work_dir(root)
    (base / "packet-sets" / "editorial-synthesis.json").unlink(missing_ok=True)
    packet_directory = base / "packets"
    if packet_directory.is_dir():
        for path in packet_directory.glob("editorial-synthesis-*.json"):
            path.unlink(missing_ok=True)
    receipt_directory = base / "receipts" / "editorial-synthesis"
    if receipt_directory.is_dir():
        for path in receipt_directory.glob("*.json"):
            try:
                receipt = read_json(path)
                result_path = confined_result_path(root, str(receipt.get("result_path", "")))
                result_path.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            path.unlink(missing_ok=True)
    result_directory = base / "results"
    if result_directory.is_dir():
        for path in result_directory.glob("editorial-synthesis-*.json"):
            path.unlink(missing_ok=True)
    (root / "semantic-findings.json").unlink(missing_ok=True)


def _merge_semantic(
    root: Path,
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    manifest: Mapping[str, Any],
    synthesis: bool = False,
) -> dict[str, Any]:
    findings: dict[str, dict[str, Any]] = {}
    for _, result in rows:
        for item in result.get("findings", []) if isinstance(result.get("findings"), list) else []:
            if isinstance(item, Mapping) and item.get("id"):
                findings[str(item["id"])] = dict(item)
    bounded = sorted(findings.values(), key=_finding_sort_key)[:MAX_FINAL_SEMANTIC_FINDINGS]
    source = dict(manifest.get("source_fingerprints", {}))
    document: dict[str, Any] = {
        "format": "steam-visualogue-semantic-findings",
        "stage": "editorial-synthesis" if synthesis else "editorial-curation",
        "source_fingerprints": source,
        "achievement_merge_fingerprint": source.get("achievement_merge"),
        "findings": bounded,
    }
    if synthesis:
        document["curation_merge_fingerprint"] = source.get("curation_merge")
    else:
        _invalidate_synthesis_downstream(root)
    fingerprint_payload = dict(document)
    fingerprint_payload.pop("semantic_merge_fingerprint", None)
    from .context_budget import sha256_bytes

    if synthesis:
        document["semantic_merge_fingerprint"] = sha256_bytes(canonical_json_bytes(fingerprint_payload))
        return _write_merge(root, "semantic-findings.json", document)
    document["curation_merge_fingerprint"] = sha256_bytes(canonical_json_bytes(fingerprint_payload))
    _write_merge(root / ".agent-work" / "merged", "editorial-curation-findings.json", document)
    return _write_merge(root, "semantic-findings.json", document)


def merge_agent_results(
    run_dir: str | Path,
    stage: str,
    *,
    packet_set: str | Path | None = None,
    cache: Any | None = None,
    cache_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(run_dir)
    if stage not in STAGES:
        raise ValueError(f"unsupported result stage: {stage}")
    if packet_set is None:
        packet_set = agent_work_dir(root) / "packet-sets" / f"{stage}.json"
    _, manifest = _current_manifest(root, packet_set, stage)
    if manifest.get("source_fingerprints") != _source_fingerprints(root, stage):
        raise ValueError("packet-set source fingerprints are stale")
    rows = _accepted_receipts(root, stage, manifest)
    if stage == "achievement-analysis":
        return _merge_achievement(root, manifest, rows, cache=cache, cache_path=cache_path)
    if stage == "editorial-curation":
        from .agent_packets import _load_current_candidate_artifact, _require_current_achievement_merge

        _require_current_achievement_merge(root, _load_current_candidate_artifact(root))
        return _merge_semantic(root, rows, manifest=manifest)
    if stage == "editorial-synthesis":
        from .agent_packets import _require_current_curation_merge

        _require_current_curation_merge(root)
        return _merge_semantic(root, rows, manifest=manifest, synthesis=True)
    if stage == "artwork-inspection":
        _assert_artwork_coverage(root, manifest, rows)
        return {**build_visual_brief_result(root), "status": "merged"}
    # focused evidence is intentionally a one-packet lookup; its accepted
    # result is already the compact artifact the story author consumes.
    return {"status": "merged", "artifact": str(receipt_dir(root) / stage / f"{rows[0][0]['packet_id']}.json")}


def build_visual_brief_result(root: Path) -> dict[str, Any]:
    from .agent_packets import build_visual_brief

    return build_visual_brief(root)


__all__ = ["accept_agent_result", "merge_agent_results"]
