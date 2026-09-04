"""Exact editorial reuse for current deck artifacts only."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from .assets import (
    _load_asset_records,
    _manifest_candidate,
    _selected_asset_ids,
    _validated_generated_record,
    register_generated_asset_bytes,
)
from .cache_db import CacheDB
from .asset_paths import remember_assets_dir, resolve_assets_dir
from .context_budget import sha256_path_hex
from .editorial_deck import deck_schema_fingerprint
from .fingerprint import compute_evidence_fingerprint, compute_visual_fingerprint
from .io_utils import read_json, require_files, utc_now_iso, write_json
from .locales import load_run_config
from .planning import validate_schema_document
from .quality_gate import quality_state_is_current


PLANNING_FILES = ("semantic-findings.json", "deck-plan.json", "art-direction.json")
ProgressCallback = Callable[[str, int | None, int | None], None]


def _progress(callback: ProgressCallback | None, message: str, current: int, total: int) -> None:
    if callback is not None:
        callback(message, current, total)


def _current_inputs(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str, str]:
    required = require_files(root, ["profile.json", "evidence.json", "visual-signals.json", "run-config.json"])
    profile, evidence, visual = (read_json(required[name]) for name in ("profile.json", "evidence.json", "visual-signals.json"))
    evidence_fp = compute_evidence_fingerprint(profile)
    visual_fp = compute_visual_fingerprint(visual)
    if evidence.get("evidence_fingerprint") != evidence_fp:
        raise ValueError("evidence.json does not match profile.json")
    if visual.get("evidence_fingerprint") != evidence_fp or visual.get("visual_fingerprint") != visual_fp:
        raise ValueError("visual-signals.json does not match current inputs")
    return profile, evidence, visual, evidence_fp, visual_fp, load_run_config(root)["report_locale"]


def _documents(root: Path) -> dict[str, dict[str, Any]]:
    paths = require_files(root, list(PLANNING_FILES))
    return {name: read_json(paths[name]) for name in PLANNING_FILES}


def _generated_ids(deck_plan: Mapping[str, Any]) -> set[str]:
    return {asset_id for asset_id in _selected_asset_ids(dict(deck_plan)) if asset_id.startswith("generated:")}


def _review_document(record: Mapping[str, Any]) -> dict[str, Any]:
    review = record.get("review")
    if not isinstance(review, Mapping):
        raise ValueError("generated reuse asset has no review")
    document = {"kind": record.get("kind"), **dict(review)}
    validate_schema_document("generated asset review", "generated-asset-review.schema.json", document)
    return document


def _verify_output(root: Path, layout: Mapping[str, Any], validation: Mapping[str, Any]) -> None:
    if validation.get("ok") is not True:
        raise ValueError("deterministic validation has not passed")
    output = root / "output"
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("output manifest is missing")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, Mapping) or manifest.get("format") != "steam-visualogue-output-manifest":
        raise ValueError("output manifest has an unsupported format")
    for field in ("locale", "deck_schema_fingerprint", "compiled_deck_fingerprint"):
        if manifest.get(field) != layout.get(field):
            raise ValueError("output manifest is stale")
    pages = manifest.get("pages")
    if not isinstance(pages, list) or manifest.get("page_count") != len(pages):
        raise ValueError("output manifest has an invalid page inventory")
    for row in pages:
        if not isinstance(row, Mapping):
            raise ValueError("output manifest page entry is invalid")
        filename = str(row.get("file") or "")
        if not filename or Path(filename).name != filename:
            raise ValueError("output manifest contains an unsafe page path")
        path = output / filename
        if not path.is_file() or sha256_path_hex(path) != row.get("sha256"):
            raise ValueError("a rendered page no longer matches the output manifest")
    contact = manifest.get("contact_sheet")
    if not isinstance(contact, Mapping):
        raise ValueError("output manifest contact sheet is missing")
    contact_name = str(contact.get("file") or "")
    contact_path = output / contact_name
    if not contact_name or Path(contact_name).name != contact_name or not contact_path.is_file() or sha256_path_hex(contact_path) != contact.get("sha256"):
        raise ValueError("the contact sheet no longer matches the output manifest")


def commit_editorial_reuse(
    run_dir: str | Path,
    cache: CacheDB,
    *,
    assets_dir: str | Path | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    root = Path(run_dir)
    _progress(progress, "validating current evidence inputs", 0, 5)
    _, evidence, _, evidence_fp, visual_fp, locale = _current_inputs(root)
    schema_fp = deck_schema_fingerprint()
    _progress(progress, "loading the current deck bundle", 1, 5)
    documents = _documents(root)
    compiled = read_json(require_files(root, ["compiled-deck.json"])["compiled-deck.json"])
    layout = read_json(require_files(root, ["publish-layout.json"])["publish-layout.json"])
    validation = read_json(require_files(root, ["validation.json"])["validation.json"])
    quality = root / "quality-state.json"
    if not quality_state_is_current(root) or not quality.is_file():
        raise ValueError("current reader, visual, and factual quality state is required")
    if compiled.get("deck_schema_fingerprint") != schema_fp or layout.get("deck_schema_fingerprint") != schema_fp:
        raise ValueError("current deck schema fingerprint is missing or stale")
    _verify_output(root, layout, validation)
    _progress(progress, "verifying generated assets", 2, 5)
    assets_root = resolve_assets_dir(root, assets_dir)
    records = _load_asset_records(assets_root)
    requested = _generated_ids(documents["deck-plan.json"])
    render_manifest = read_json(root / "output" / ".render-manifest.json")
    rendered = {
        str(asset.get("asset_id"))
        for page in render_manifest.get("pages", [])
        if isinstance(page, Mapping)
        for asset in page.get("assets", [])
        if isinstance(asset, Mapping) and asset.get("status") == "rendered"
    }
    if requested - rendered:
        raise ValueError("a selected generated asset was not rendered")
    cached_assets: list[dict[str, Any]] = []
    for asset_id in sorted(requested):
        record = _validated_generated_record(asset_id, records.get(asset_id), assets_root)
        if record is None:
            raise ValueError("a selected generated asset failed integrity validation")
        _review_document(record)
        source = _manifest_candidate(assets_root, record.get("path"))
        if source is None:
            raise ValueError("a generated asset path is unsafe")
        cached_assets.append({"asset_id": asset_id, "payload": source.read_bytes(), "record": {key: value for key, value in record.items() if key != "path"}})
    bundle = {
        "format": "steam-visualogue-editorial-reuse",
        "evidence_fingerprint": evidence_fp,
        "visual_fingerprint": visual_fp,
        "deck_schema_fingerprint": schema_fp,
        "compiled_deck_fingerprint": compiled.get("compiled_deck_fingerprint"),
        "report_locale": locale,
        "documents": documents,
        "generated_asset_ids": sorted(requested),
    }
    _progress(progress, "writing the reusable current bundle", 3, 5)
    stored = cache.put_editorial_bundle_for_run(
        str(evidence.get("run_id") or ""),
        evidence_fp,
        visual_fp,
        locale,
        bundle,
        cached_assets,
        deck_schema_fingerprint=schema_fp,
    )
    receipt = {
        "format": "steam-visualogue-editorial-reuse-receipt",
        "status": "committed",
        "evidence_fingerprint": evidence_fp,
        "visual_fingerprint": visual_fp,
        "deck_schema_fingerprint": schema_fp,
        "report_locale": locale,
        "generated_assets": stored.get("generated_assets", []),
        "committed_at": utc_now_iso(),
    }
    write_json(root / "editorial-reuse.json", receipt)
    _progress(progress, "reusable bundle committed", 5, 5)
    return receipt


def reuse_editorial(
    run_dir: str | Path,
    cache: CacheDB,
    *,
    assets_dir: str | Path | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    root = Path(run_dir)
    if any((root / name).exists() for name in PLANNING_FILES):
        return {"status": "conflict", "reason": "current-deck-artifacts-exist"}
    _progress(progress, "validating current evidence inputs", 0, 4)
    _, evidence, _, evidence_fp, visual_fp, locale = _current_inputs(root)
    schema_fp = deck_schema_fingerprint()
    _progress(progress, "looking up an exact current bundle", 1, 4)
    try:
        cached = cache.get_editorial_bundle_for_run(
            str(evidence.get("run_id") or ""),
            evidence_fp,
            visual_fp,
            locale,
            deck_schema_fingerprint=schema_fp,
        )
    except (TypeError, ValueError):
        return {"status": "miss", "reason": "fingerprint-not-found"}
    if not isinstance(cached, Mapping):
        return {"status": "miss", "reason": "fingerprint-not-found"}
    bundle = cached.get("bundle")
    if not isinstance(bundle, Mapping) or bundle.get("format") != "steam-visualogue-editorial-reuse" or bundle.get("deck_schema_fingerprint") != schema_fp or bundle.get("evidence_fingerprint") != evidence_fp or bundle.get("visual_fingerprint") != visual_fp or bundle.get("report_locale") != locale:
        return {"status": "miss", "reason": "bundle-is-not-current"}
    documents = bundle.get("documents")
    if not isinstance(documents, Mapping) or set(documents) != set(PLANNING_FILES):
        return {"status": "miss", "reason": "bundle-documents-invalid"}
    try:
        _progress(progress, "validating reusable deck documents", 2, 4)
        for name in PLANNING_FILES:
            if not isinstance(documents.get(name), Mapping):
                raise ValueError(f"{name} is invalid")
        requested = _generated_ids(documents["deck-plan.json"])
        assets = {str(item.get("asset_id")): item for item in cached.get("generated_assets", []) if isinstance(item, Mapping)}
        if requested != set(assets):
            raise ValueError("cached generated assets do not match the current deck")
        with tempfile.TemporaryDirectory(prefix="steam-visualogue-reuse-") as temporary:
            stage = Path(temporary) / "assets"
            for asset_id in sorted(requested):
                item = assets[asset_id]
                record = item.get("record")
                if not isinstance(record, Mapping):
                    raise ValueError("cached generated asset record is invalid")
                registered = register_generated_asset_bytes(item.get("payload") or b"", stage, _review_document(record))
                if registered.get("asset_id") != asset_id:
                    raise ValueError("cached generated pixels do not match asset ID")
            target = resolve_assets_dir(root, assets_dir)
            target.mkdir(parents=True, exist_ok=True)
            target_records = _load_asset_records(target)
            staged_records = _load_asset_records(stage)
            for asset_id, record in staged_records.items():
                source = _manifest_candidate(stage, record.get("path"))
                destination = _manifest_candidate(target, record.get("path"))
                if source is None or destination is None:
                    raise ValueError("staged asset path is unsafe")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                target_records[asset_id] = record
            write_json(target / "manifest.json", {"assets": target_records})
            remember_assets_dir(root, target)
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"status": "miss", "reason": "bundle-preflight-failed"}
    _progress(progress, "restoring current deck documents", 3, 4)
    for name in PLANNING_FILES:
        write_json(root / name, documents[name])
    for name in ("compiled-deck.json", "publish-layout.json", "validation.json", "quality-state.json", "editorial-reuse.json"):
        path = root / name
        if path.is_file():
            path.unlink()
    output_dir = root / "output"
    if output_dir.is_dir():
        for child in output_dir.iterdir():
            if child.is_file():
                child.unlink()
            elif child.is_dir() and child.name == "thumbnails":
                shutil.rmtree(child)
    receipt = {
        "format": "steam-visualogue-editorial-reuse-receipt",
        "status": "reused",
        "source_run_id": cached.get("source_run_id"),
        "evidence_fingerprint": evidence_fp,
        "visual_fingerprint": visual_fp,
        "deck_schema_fingerprint": schema_fp,
        "report_locale": locale,
        "generated_asset_ids": sorted(_generated_ids(documents["deck-plan.json"])),
        "reused_at": utc_now_iso(),
    }
    write_json(root / "editorial-reuse.json", receipt)
    _progress(progress, "current deck bundle restored", 4, 4)
    return receipt


__all__ = ["PLANNING_FILES", "commit_editorial_reuse", "reuse_editorial"]
