"""Command-line orchestration for the current Steam Visualogue workflow."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .io_utils import read_json, require_files, write_json
from .asset_paths import remember_assets_dir, resolve_assets_dir
from .paths import workspace_root


def _default_cache_path() -> Path:
    return workspace_root() / ".steam-visualogue-cache.sqlite"


def _resolve_collect_locale(args: argparse.Namespace) -> str:
    from .locales import load_default_report_locale, normalize_report_locale
    if args.report_locale is not None:
        return normalize_report_locale(args.report_locale)
    return load_default_report_locale(workspace_root()) or "en-US"


def _run_locale(run_dir: str | Path) -> str:
    from .locales import load_run_config
    return load_run_config(Path(run_dir))["report_locale"]


def _print_result(payload: Mapping[str, Any] | dict[str, Any]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False), flush=True)


def _structured_cli_error(error: Exception, command: str | None = None) -> dict[str, Any]:
    from .editorial_deck import EditorialDeckError
    from .publish_layout import PublishLayoutError
    from .quality_gate import QualityGateError
    for error_type in (EditorialDeckError, PublishLayoutError, QualityGateError):
        if isinstance(error, error_type):
            return error.as_dict()
    message = _redact_sensitive_error(str(error))
    code_by_type = {FileNotFoundError: "required_artifact_missing", json.JSONDecodeError: "json_invalid", ValueError: "validation_error"}
    code = next((value for error_type, value in code_by_type.items() if isinstance(error, error_type)), "command_failed")
    next_steps = {
        "compile-deck": "repair the current deck inputs and rerun compile-deck",
        "quality-start": "repair or create the current compiled-deck.json, then rerun quality-start",
        "quality-submit": "repair the assigned current result and rerun quality-submit",
        "quality-finish": "submit every assigned result and rerun quality-finish",
        "quality-status": "inspect current quality fingerprints and decisions",
        "finalize-quality": "complete all current quality gates and rerun finalize-quality",
        "render": "compile the deck and complete the current reader gate before rendering",
        "assets": "compile the deck and select assets before materializing them",
        "validate": "repair the current publish layout or rendered output and rerun validate",
    }
    return {
        "status": "error",
        "code": code,
        "message": message,
        "command": command,
        "attempt_id": getattr(error, "attempt_id", None),
        "gate": getattr(error, "gate", None),
        "packet_id": getattr(error, "packet_id", None),
        "next_step": next_steps.get(command, "inspect the reported artifact and rerun the command after repairing it"),
    }


def _require_current_quality_gate(run_dir: str | Path, gate: str) -> dict[str, Any]:
    from .quality_gate import get_quality_status
    status = get_quality_status(run_dir)
    gates = status.get("gates")
    entry = gates.get(gate) if isinstance(gates, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("status") != "passed" or entry.get("current") is not True:
        raise ValueError(f"{gate} quality gate must be passed and current before this stage")
    return dict(entry)


def _print_progress(stage: str, message: str, current: int | None = None, total: int | None = None) -> None:
    payload: dict[str, Any] = {"event": "progress", "stage": str(stage), "message": str(message)}
    if current is not None:
        payload["current"] = max(0, int(current))
    if total is not None:
        payload["total"] = max(0, int(total))
    if current is not None and total is not None and int(total) > 0:
        bounded = min(max(0, int(current)), int(total))
        payload["percent"] = round((bounded / int(total)) * 100)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _stage_progress(stage: str) -> Callable[[str, int | None, int | None], None]:
    last_key: tuple[str, int | None, int | None] | None = None
    def emit(message: str, current: int | None = None, total: int | None = None) -> None:
        nonlocal last_key
        position = int(current) if current is not None else None
        if current is not None and total is not None and total > 20:
            position = min(20, max(0, int(current)) * 20 // int(total))
        key = (str(message), int(total) if total is not None else None, position)
        if key == last_key:
            return
        last_key = key
        _print_progress(stage, message, current, total)
    return emit


def _redact_sensitive_error(message: str, api_key: str = "") -> str:
    if api_key:
        message = message.replace(api_key, "<redacted>")
        message = message.replace(urllib.parse.quote(api_key, safe=""), "<redacted>")
    message = re.sub(r"(?i)\b[0-9a-f]{32}\b", "<redacted>", message)
    return re.sub(r"\b\d{16,20}\b", "<steamid>", message)


def _tracked_steam_api_factory(apis: list[Any]) -> Callable[[], Any]:
    from .credentials import load_steam_api_key
    from .steam_api import SteamAPI

    def create() -> SteamAPI:
        api = SteamAPI(load_steam_api_key())
        apis.append(api)
        return api

    return create


def _collect(args: argparse.Namespace) -> int:
    from .cache_db import CacheDB
    from .locales import ensure_run_config
    from .steam_api import SteamDataCollector
    apis: list[Any] = []
    create_steam_api = _tracked_steam_api_factory(apis)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    ensure_run_config(run_dir, _resolve_collect_locale(args))
    cache = CacheDB(Path(args.cache))
    try:
        profile = SteamDataCollector(cache=cache, api_factory=create_steam_api).collect(args.identity, force=args.force, progress=_stage_progress("collect"))
    finally:
        for api in apis:
            api.close()
        cache.close()
    destination = write_json(run_dir / "profile.json", profile)
    snapshot = profile.get("data_snapshot") or {}
    _print_result({"status": "ok", "artifact": str(destination), "source": snapshot.get("source", "network"), "snapshot_at": snapshot.get("collected_at"), "games": len(profile.get("games", []))})
    return 0


def _enrich(args: argparse.Namespace) -> int:
    from .cache_db import CacheDB
    from .steam_api import SteamDataCollector
    apis: list[Any] = []
    create_steam_api = _tracked_steam_api_factory(apis)
    _run_locale(args.run_dir)
    paths = require_files(args.run_dir, ["profile.json"])
    cache = CacheDB(Path(args.cache))
    try:
        profile = SteamDataCollector(cache=cache, api_factory=create_steam_api).enrich_played_profile(read_json(paths["profile.json"]), force=args.force, progress=_stage_progress("enrich"))
    finally:
        for api in apis:
            api.close()
        cache.close()
    destination = write_json(Path(args.run_dir) / "profile.json", profile)
    state = profile.get("data_status", {}).get("enrichment", {})
    snapshot = profile.get("data_snapshot") or {}
    _print_result({"status": "ok", "artifact": str(destination), "source": state.get("source", snapshot.get("source", "network")), "processed": state.get("requested", 0), "reused": state.get("reused", 0), "enriched": state.get("requested", 0), "enriched_at": profile.get("enriched_at", snapshot.get("enriched_at"))})
    return 0


def _derive(args: argparse.Namespace) -> int:
    from .analytics import derive_signals
    from .evidence import build_evidence
    from .fingerprint import compute_evidence_fingerprint
    progress = _stage_progress("derive")
    _run_locale(args.run_dir)
    paths = require_files(args.run_dir, ["profile.json"])
    progress("Loading profile", 0, 3)
    profile = read_json(paths["profile.json"])
    fingerprint = compute_evidence_fingerprint(profile)
    if profile.get("evidence_fingerprint") != fingerprint:
        profile["evidence_fingerprint"] = fingerprint
        write_json(paths["profile.json"], profile)
    progress("Deriving signals", 1, 3)
    signals = derive_signals(profile)
    signals["evidence_fingerprint"] = fingerprint
    progress("Building evidence", 2, 3)
    evidence = build_evidence(profile, signals)
    progress("Writing derived artifacts", 3, 3)
    signals_path = write_json(Path(args.run_dir) / "signals.json", signals)
    evidence_path = write_json(Path(args.run_dir) / "evidence.json", evidence)
    _print_result({"status": "ok", "artifacts": [str(signals_path), str(evidence_path)], "evidence_cards": len(evidence.get("cards", evidence.get("evidence", [])))})
    return 0


def _palette_scan(args: argparse.Namespace) -> int:
    from .cache_db import CacheDB
    from .fingerprint import compute_visual_fingerprint
    from .visual_signals import build_library_palette_signals
    _run_locale(args.run_dir)
    profile = read_json(require_files(args.run_dir, ["profile.json"])["profile.json"])
    summary: dict[str, int] = {}
    cache = CacheDB(Path(args.cache))
    try:
        visual = build_library_palette_signals(profile, cache, force_artwork=args.force_artwork, force_palette=args.force_palette, progress=_stage_progress("palette"), work_summary=summary)
    finally:
        cache.close()
    visual["evidence_fingerprint"] = profile.get("evidence_fingerprint") or visual.get("evidence_fingerprint")
    visual["visual_fingerprint"] = compute_visual_fingerprint(visual)
    destination = write_json(Path(args.run_dir) / "visual-signals.json", visual)
    signals_path = Path(args.run_dir) / "signals.json"
    if signals_path.is_file():
        signals = read_json(signals_path)
        signals["visual"] = visual
        write_json(signals_path, signals)
    _print_result({"status": "ok", "artifact": str(destination), "eligible_games": visual["sampling"]["eligible_games"], "selected_games": visual["sampling"]["selected_games"], "successful_games": visual["sampling"]["successful_games"], "confidence": visual["confidence"], "cache_hits": summary.get("artwork_cache_hits", 0) + summary.get("palette_cache_hits", 0), "downloads": summary.get("downloads", 0), "stale_fallbacks": summary.get("stale_fallbacks", 0), "extraction_failures": summary.get("extraction_failures", 0)})
    return 0


def _validate_schema_document(document_name: str, schema_name: str, document: Any) -> None:
    from .planning import validate_schema_document
    validate_schema_document(document_name, schema_name, document)


def _ensure_localized_labels(args: argparse.Namespace, run_dir: Path, deck_plan: Mapping[str, Any]) -> dict[str, Any]:
    from .label_localization import localized_labels_current, materialize_localized_labels, scan_label_references
    labels_path = run_dir / "localized-labels.json"
    if labels_path.is_file() and not getattr(args, "force_labels", False):
        labels = read_json(labels_path)
        if localized_labels_current(labels, deck_plan, _run_locale(run_dir)):
            return labels
    if not (run_dir / "profile.json").is_file():
        if labels_path.is_file():
            raise ValueError("profile.json is required to rematerialize stale localized labels")
        return {}
    from .cache_db import CacheDB
    from .credentials import load_steam_api_key
    from .steam_api import SteamAPI
    references = scan_label_references(deck_plan)
    api = None
    if _run_locale(run_dir) == "zh-CN" and (references["games"] or references["achievements"]):
        api = SteamAPI(load_steam_api_key())
    cache = CacheDB(Path(getattr(args, "cache", _default_cache_path())))
    try:
        return materialize_localized_labels(run_dir, cache, api=api, force=getattr(args, "force_labels", False), progress=_stage_progress("compile-deck"))
    finally:
        if api is not None:
            api.close()
        cache.close()


def _compile_deck(args: argparse.Namespace) -> int:
    from .editorial_deck import compile_editorial_deck, deck_schema_fingerprint
    run_dir = Path(args.run_dir)
    locale = _run_locale(run_dir)
    paths = require_files(run_dir, ["evidence.json", "semantic-findings.json", "deck-plan.json"])
    evidence = read_json(paths["evidence.json"])
    findings = read_json(paths["semantic-findings.json"])
    plan = read_json(paths["deck-plan.json"])
    _validate_schema_document("evidence.json", "evidence.schema.json", evidence)
    _validate_schema_document("semantic-findings.json", "semantic-findings.schema.json", findings)
    _validate_schema_document("deck-plan.json", "deck-plan.schema.json", plan)
    if plan.get("locale") != locale:
        raise ValueError("deck-plan.json locale does not match run-config.json")
    labels = _ensure_localized_labels(args, run_dir, plan)
    compiled = compile_editorial_deck(plan, findings, evidence, labels)
    _validate_schema_document("compiled-deck.json", "compiled-deck.schema.json", compiled)
    artifact = write_json(run_dir / "compiled-deck.json", compiled)
    manifest = write_json(run_dir / "compile-manifest.json", {"format": "steam-visualogue-compile-manifest", "locale": locale, "deck_schema_fingerprint": deck_schema_fingerprint(), "compiled_deck_fingerprint": compiled.get("compiled_deck_fingerprint"), "reader_audit": compiled.get("reader_audit", {}), "method_note": "Deterministic reader contract and evidence-closure compilation."})
    _print_result({"status": "ok", "artifact": str(artifact), "manifest": str(manifest), "pages": len(compiled.get("pages", [])), "reader_audit": compiled.get("reader_audit", {})})
    return 0


def _render(args: argparse.Namespace) -> int:
    from .contact_sheet import make_contact_sheet
    from .exports import build_output_manifest, export_story_markdown
    from .publish_layout import compose_publish_layout
    from .render import render_deck
    from .validate import validate_deck
    run_dir = Path(args.run_dir)
    locale = _run_locale(run_dir)
    _require_current_quality_gate(run_dir, "reader")
    paths = require_files(run_dir, ["compiled-deck.json", "art-direction.json", "visual-signals.json", "visual-brief.json"])
    compiled = read_json(paths["compiled-deck.json"])
    direction = read_json(paths["art-direction.json"])
    _validate_schema_document("compiled-deck.json", "compiled-deck.schema.json", compiled)
    _validate_schema_document("art-direction.json", "art-direction.schema.json", direction)
    assets_dir = resolve_assets_dir(run_dir, args.assets_dir)
    assets_path = assets_dir / "manifest.json"
    if not assets_path.is_file():
        raise FileNotFoundError(f"Missing required artifact: {assets_path}")
    assets = read_json(assets_path)
    visual = read_json(paths["visual-signals.json"])
    visual_brief = read_json(paths["visual-brief.json"])
    _validate_schema_document("visual-signals.json", "visual-signals.schema.json", visual)
    _validate_schema_document("visual-brief.json", "visual-brief.schema.json", visual_brief)
    from .fingerprint import compute_asset_manifest_fingerprint, compute_visual_brief_fingerprint, compute_visual_fingerprint
    if visual.get("visual_fingerprint") != compute_visual_fingerprint(visual):
        raise ValueError("visual-signals.json is stale")
    if visual_brief.get("visual_fingerprint") != visual.get("visual_fingerprint"):
        raise ValueError("visual-brief.json does not match visual-signals.json")
    if visual_brief.get("evidence_fingerprint") != visual.get("evidence_fingerprint"):
        raise ValueError("visual-brief.json does not match visual evidence")
    if visual_brief.get("compiled_deck_fingerprint") != compiled.get("compiled_deck_fingerprint"):
        raise ValueError("visual-brief.json does not match compiled-deck.json")
    if visual_brief.get("asset_manifest_fingerprint") != compute_asset_manifest_fingerprint(assets):
        raise ValueError("visual-brief.json does not match the asset manifest")
    if visual_brief.get("visual_brief_fingerprint") != compute_visual_brief_fingerprint(visual_brief):
        raise ValueError("visual-brief.json fingerprint does not match its contents")
    layout = compose_publish_layout(compiled, direction, assets, visual_brief)
    layout_path = write_json(run_dir / "publish-layout.json", layout)
    output_dir = run_dir / "output"
    pages = render_deck(layout, assets_dir, output_dir, progress=_stage_progress("render"))
    report = validate_deck(layout, output_dir, check_output_manifest=False, art_direction=direction, visual_brief=visual_brief, assets_manifest=assets, compiled_deck=compiled)
    report_path = run_dir / "validation.json"
    if not report.get("ok"):
        write_json(report_path, report)
        _print_result({"status": "validation-failed", "layout": str(layout_path), "validation": str(report_path)})
        return 2
    contact = make_contact_sheet(pages, output_dir / "contact-sheet.png", report_locale=locale, layout=layout)
    quality_contact = run_dir / ".agent-work" / "quality" / "contact-sheet.png"
    quality_contact.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(contact, quality_contact)
    reader_export = export_story_markdown(compiled, output_dir / "story.md", report_locale=locale)
    render_manifest = read_json(output_dir / ".render-manifest.json") if (output_dir / ".render-manifest.json").is_file() else {}
    input_assets = [asset for page in render_manifest.get("pages", []) if isinstance(page, Mapping) for asset in page.get("assets", []) if isinstance(asset, Mapping)]
    manifest_path = build_output_manifest(pages, contact_sheet=contact, layout=layout, validation=report, destination=output_dir / "manifest.json", input_assets=input_assets, report_locale=locale, label_fingerprint=layout.get("label_fingerprint"))
    final_report = validate_deck(layout, output_dir, art_direction=direction, visual_brief=visual_brief, assets_manifest=assets, compiled_deck=compiled)
    final_report_path = write_json(report_path, final_report)
    if final_report.get("ok"):
        remember_assets_dir(run_dir, assets_dir)
    _print_result({"status": "ok" if final_report.get("ok") else "validation-failed", "pages": [str(path) for path in pages], "layout": str(layout_path), "contact_sheet": str(contact), "reader_export": str(reader_export), "validation": str(final_report_path), "manifest": str(manifest_path)})
    return 0 if final_report.get("ok") else 2


def _assets(args: argparse.Namespace) -> int:
    from .assets import _selected_asset_ids, materialize_selected_assets
    from .cache_db import CacheDB
    run_dir = Path(args.run_dir)
    _run_locale(run_dir)
    _require_current_quality_gate(run_dir, "reader")
    profile = read_json(require_files(run_dir, ["profile.json"])["profile.json"])
    plan: dict[str, Any] = {"shortlist": list(args.select)} if args.select else read_json(require_files(run_dir, ["deck-plan.json"])["deck-plan.json"])
    assets_dir = resolve_assets_dir(run_dir, args.assets_dir)
    cache = CacheDB(Path(args.cache)) if args.cache else None
    try:
        manifest = materialize_selected_assets(profile, plan, assets_dir, prune_generated=args.prune_generated, cache=cache, force_artwork=args.force_artwork, force_palette=args.force_palette, progress=_stage_progress("assets"))
    finally:
        if cache is not None:
            cache.close()
    selected = _selected_asset_ids(plan)
    remember_assets_dir(run_dir, assets_dir)
    records = manifest.get("assets", {})
    ready = sum(1 for asset_id in selected if isinstance(records.get(asset_id), Mapping) and records[asset_id].get("status") == "ready")
    _print_result({"status": "ok", "artifact": str(assets_dir / "manifest.json"), "ready": ready, "requested": len(selected)})
    return 0


def _register_generated(args: argparse.Namespace) -> int:
    from .assets import register_generated_asset
    _run_locale(args.run_dir)
    review = read_json(args.review)
    _validate_schema_document("generated asset review", "generated-asset-review.schema.json", review)
    assets_dir = resolve_assets_dir(args.run_dir, args.assets_dir)
    registered = register_generated_asset(args.source, assets_dir, review)
    remember_assets_dir(args.run_dir, assets_dir)
    _print_result({"status": "ok", "asset_id": registered["asset_id"], "artifact": str(assets_dir / "manifest.json"), "sha256": registered["sha256"]})
    return 0


def _reuse_editorial(args: argparse.Namespace) -> int:
    from .cache_db import CacheDB
    from .editorial_reuse import reuse_editorial
    _run_locale(args.run_dir)
    cache = CacheDB(Path(args.cache))
    try:
        result = reuse_editorial(args.run_dir, cache, assets_dir=args.assets_dir, progress=_stage_progress("reuse-editorial"))
    finally:
        cache.close()
    _print_result(result)
    return 0


def _commit_reuse(args: argparse.Namespace) -> int:
    from .cache_db import CacheDB
    from .editorial_reuse import commit_editorial_reuse
    _run_locale(args.run_dir)
    cache = CacheDB(Path(args.cache))
    try:
        result = commit_editorial_reuse(args.run_dir, cache, assets_dir=args.assets_dir, progress=_stage_progress("commit-reuse"))
    finally:
        cache.close()
    _print_result(result)
    return 0


def _quality_start(args: argparse.Namespace) -> int:
    from .quality_gate import start_quality_gate
    _print_result(start_quality_gate(args.run_dir, args.gate))
    return 0


def _quality_submit(args: argparse.Namespace) -> int:
    from .quality_gate import submit_quality_result
    result = submit_quality_result(args.run_dir, args.attempt, args.packet_id)
    _print_result(result)
    return 0 if result.get("status") == "accepted" else 2


def _quality_finish(args: argparse.Namespace) -> int:
    from .quality_gate import finish_quality_gate
    result = finish_quality_gate(args.run_dir, args.attempt)
    _print_result(result)
    return 0 if result.get("status") == "passed" else 2


def _quality_status(args: argparse.Namespace) -> int:
    from .quality_gate import get_quality_status
    _print_result(get_quality_status(args.run_dir))
    return 0


def _finalize_quality(args: argparse.Namespace) -> int:
    from .quality_gate import finalize_quality
    _print_result(finalize_quality(args.run_dir))
    return 0


def _packetize(args: argparse.Namespace) -> int:
    from .agent_packets import build_packet_set
    _print_result(build_packet_set(args.run_dir, args.stage, evidence_id=args.evidence_id, select=args.select, cache_path=getattr(args, "cache", None)))
    return 0


def _accept_agent_result(args: argparse.Namespace) -> int:
    from .agent_results import accept_agent_result
    _print_result(accept_agent_result(args.run_dir, args.packet_set, args.packet_id, args.result, cache_path=getattr(args, "cache", None)))
    return 0


def _merge_agent_results(args: argparse.Namespace) -> int:
    from .agent_results import merge_agent_results
    _print_result(merge_agent_results(args.run_dir, args.stage, packet_set=args.packet_set, cache_path=getattr(args, "cache", None)))
    return 0


def _build_visual_brief(args: argparse.Namespace) -> int:
    from .agent_packets import build_visual_brief
    _require_current_quality_gate(args.run_dir, "reader")
    _print_result(build_visual_brief(args.run_dir))
    return 0


def _validate(args: argparse.Namespace) -> int:
    from .validate import validate_deck
    run_dir = Path(args.run_dir)
    _run_locale(run_dir)
    paths = require_files(run_dir, ["compiled-deck.json", "publish-layout.json", "output/manifest.json", "art-direction.json", "visual-signals.json", "visual-brief.json"])
    compiled = read_json(paths["compiled-deck.json"])
    layout = read_json(paths["publish-layout.json"])
    direction = read_json(paths["art-direction.json"])
    visual = read_json(paths["visual-signals.json"])
    visual_brief = read_json(paths["visual-brief.json"])
    assets = read_json(resolve_assets_dir(run_dir) / "manifest.json")
    _validate_schema_document("compiled-deck.json", "compiled-deck.schema.json", compiled)
    _validate_schema_document("publish-layout.json", "publish-layout.schema.json", layout)
    _validate_schema_document("art-direction.json", "art-direction.schema.json", direction)
    _validate_schema_document("visual-signals.json", "visual-signals.schema.json", visual)
    _validate_schema_document("visual-brief.json", "visual-brief.schema.json", visual_brief)
    from .fingerprint import compute_asset_manifest_fingerprint, compute_visual_brief_fingerprint, compute_visual_fingerprint
    if visual.get("visual_fingerprint") != compute_visual_fingerprint(visual) or visual_brief.get("visual_brief_fingerprint") != compute_visual_brief_fingerprint(visual_brief):
        raise ValueError("current visual inputs are stale")
    if visual_brief.get("evidence_fingerprint") != visual.get("evidence_fingerprint"):
        raise ValueError("visual brief evidence is stale")
    if visual_brief.get("compiled_deck_fingerprint") != layout.get("compiled_deck_fingerprint") or visual_brief.get("asset_manifest_fingerprint") != compute_asset_manifest_fingerprint(assets):
        raise ValueError("current visual inputs do not match the publish layout")
    report = validate_deck(layout, run_dir / "output", art_direction=direction, visual_brief=visual_brief, assets_manifest=assets, compiled_deck=compiled)
    destination = write_json(run_dir / "validation.json", report)
    _print_result({"status": "ok" if report.get("ok") else "validation-failed", "artifact": str(destination), "pages": report.get("pages")})
    return 0 if report.get("ok") else 2


def _purge_user_cache(args: argparse.Namespace) -> int:
    from .cache_db import CacheDB
    if not args.confirm:
        raise ValueError("Refusing to purge user cache without --confirm")
    if not re.fullmatch(r"\d{16,20}", args.steamid):
        raise ValueError("--steamid must be a numeric SteamID64")
    cache = CacheDB(Path(args.cache))
    try:
        deleted = cache.purge_user(args.steamid)
    finally:
        cache.close()
    _print_result({"status": "ok", "deleted_rows": deleted})
    return 0


def _purge_global_cache(args: argparse.Namespace) -> int:
    from .cache_db import CacheDB
    if not args.confirm:
        raise ValueError("Refusing to purge global cache without --confirm")
    cache = CacheDB(Path(args.cache))
    try:
        deleted = cache.purge_global()
    finally:
        cache.close()
    _print_result({"status": "ok", "deleted_rows": deleted})
    return 0


def normalize_run_dir(value: str | Path) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    parts = path.parts
    if not parts:
        return str(path)
    if parts[0] == "output":
        return path.as_posix()
    return (Path("output") / path).as_posix()


def _common_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", type=normalize_run_dir, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="steam-visualogue", description="Deterministic data and rendering pipeline for a personal Steam visual essay.")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Fetch and normalize the library.")
    collect.add_argument("--identity", required=True)
    _common_run(collect)
    collect.add_argument("--cache", default=str(_default_cache_path()))
    collect.add_argument("--report-locale")
    collect.add_argument("--force", action="store_true")
    collect.set_defaults(handler=_collect)

    enrich = sub.add_parser("enrich", help="Fetch Store and achievement data.")
    _common_run(enrich)
    enrich.add_argument("--cache", default=str(_default_cache_path()))
    enrich.add_argument("--force", action="store_true")
    enrich.set_defaults(handler=_enrich)

    derive = sub.add_parser("derive", help="Create signals.json and evidence.json.")
    _common_run(derive)
    derive.set_defaults(handler=_derive)

    palette = sub.add_parser("palette", help="Estimate sampled library artwork signals.")
    _common_run(palette)
    palette.add_argument("--cache", default=str(_default_cache_path()))
    palette.add_argument("--force-artwork", action="store_true")
    palette.add_argument("--force-palette", action="store_true")
    palette.set_defaults(handler=_palette_scan)

    assets = sub.add_parser("assets", help="Materialize artwork selected by the current deck plan.")
    _common_run(assets)
    assets.add_argument("--assets-dir")
    assets.add_argument("--cache", default=str(_default_cache_path()))
    assets.add_argument("--force-artwork", action="store_true")
    assets.add_argument("--force-palette", action="store_true")
    assets.add_argument("--select", action="append")
    assets.add_argument("--prune-generated", action="store_true")
    assets.set_defaults(handler=_assets)

    generated = sub.add_parser("register-generated", help="Register one reviewed generated visual asset.")
    _common_run(generated)
    generated.add_argument("--source", required=True)
    generated.add_argument("--review", required=True)
    generated.add_argument("--assets-dir")
    generated.set_defaults(handler=_register_generated)

    reuse = sub.add_parser("reuse-editorial", help="Restore an exact current editorial bundle.")
    _common_run(reuse)
    reuse.add_argument("--cache", default=str(_default_cache_path()))
    reuse.add_argument("--assets-dir")
    reuse.set_defaults(handler=_reuse_editorial)

    commit = sub.add_parser("commit-reuse", help="Cache the current finalized editorial bundle.")
    _common_run(commit)
    commit.add_argument("--cache", default=str(_default_cache_path()))
    commit.add_argument("--assets-dir")
    commit.set_defaults(handler=_commit_reuse)

    compile_deck = sub.add_parser("compile-deck", help="Compile deck-plan.json into compiled-deck.json.")
    _common_run(compile_deck)
    compile_deck.add_argument("--cache", default=str(_default_cache_path()))
    compile_deck.add_argument("--force-labels", action="store_true")
    compile_deck.set_defaults(handler=_compile_deck)

    quality_start = sub.add_parser("quality-start", help="Start a reader, visual, or factual quality gate.")
    _common_run(quality_start)
    quality_start.add_argument("--gate", required=True, choices=("reader", "visual", "factual"))
    quality_start.set_defaults(handler=_quality_start)

    quality_submit = sub.add_parser("quality-submit", help="Submit one assigned quality result.")
    _common_run(quality_submit)
    quality_submit.add_argument("--attempt", required=True)
    quality_submit.add_argument("--packet-id", required=True)
    quality_submit.set_defaults(handler=_quality_submit)

    quality_finish = sub.add_parser("quality-finish", help="Finish one bounded quality attempt.")
    _common_run(quality_finish)
    quality_finish.add_argument("--attempt", required=True)
    quality_finish.set_defaults(handler=_quality_finish)

    quality_status = sub.add_parser("quality-status", help="Show current quality gate status.")
    _common_run(quality_status)
    quality_status.set_defaults(handler=_quality_status)

    finalize = sub.add_parser("finalize-quality", help="Finalize the current render and quality gates.")
    _common_run(finalize)
    finalize.set_defaults(handler=_finalize_quality)

    packetize = sub.add_parser("packetize", help="Build one bounded Agent packet set.")
    _common_run(packetize)
    packetize.add_argument("--stage", required=True, choices=("achievement-analysis", "editorial-curation", "editorial-synthesis", "focused-evidence", "artwork-inspection"))
    packetize.add_argument("--evidence-id")
    packetize.add_argument("--select", action="append")
    packetize.add_argument("--cache", default=str(_default_cache_path()))
    packetize.set_defaults(handler=_packetize)

    accept = sub.add_parser("accept-agent-result", help="Validate and receipt one bounded Agent result.")
    _common_run(accept)
    accept.add_argument("--packet-set", required=True)
    accept.add_argument("--packet-id", required=True)
    accept.add_argument("--result", required=True)
    accept.add_argument("--cache", default=str(_default_cache_path()))
    accept.set_defaults(handler=_accept_agent_result)

    merge = sub.add_parser("merge-agent-results", help="Merge only accepted current Agent results.")
    _common_run(merge)
    merge.add_argument("--stage", required=True, choices=("achievement-analysis", "editorial-curation", "editorial-synthesis", "focused-evidence", "artwork-inspection"))
    merge.add_argument("--packet-set")
    merge.add_argument("--cache", default=str(_default_cache_path()))
    merge.set_defaults(handler=_merge_agent_results)

    visual_brief = sub.add_parser("build-visual-brief", help="Build the bounded Agent-readable visual brief.")
    _common_run(visual_brief)
    visual_brief.set_defaults(handler=_build_visual_brief)

    render = sub.add_parser("render", help="Build, render, and validate the current publish layout.")
    _common_run(render)
    render.add_argument("--assets-dir")
    render.set_defaults(handler=_render)

    validate = sub.add_parser("validate", help="Re-run deterministic validation on rendered output.")
    _common_run(validate)
    validate.set_defaults(handler=_validate)

    purge = sub.add_parser("purge-user-cache", help="Delete one identity's cached rows.")
    purge.add_argument("--steamid", required=True)
    purge.add_argument("--cache", default=str(_default_cache_path()))
    purge.add_argument("--confirm", action="store_true")
    purge.set_defaults(handler=_purge_user_cache)

    purge_global = sub.add_parser("purge-global", help="Delete shared public cache rows.")
    purge_global.add_argument("--cache", default=str(_default_cache_path()))
    purge_global.add_argument("--confirm", action="store_true")
    purge_global.set_defaults(handler=_purge_global_cache)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr, flush=True)
        return 130
    except Exception as error:
        print(json.dumps(_structured_cli_error(error, getattr(args, "command", None)), ensure_ascii=False), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
