"""Deterministic validation for the current publish layout and render."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .context_budget import sha256_path_hex
from .fingerprint import (
    compute_asset_manifest_fingerprint,
    compute_layout_input_fingerprint,
    compute_visual_brief_fingerprint,
)
from .io_utils import write_json
from .locales import catalog_for, normalize_report_locale
from .planning import validate_schema_document


_INTERNAL_TEXT = re.compile(
    r"(?:sha256:[0-9a-f]{16,}|(?:^|[\s([])(?:game|achievement|claim|evidence|generated):[A-Za-z0-9:_-]+|"
    r"evidence\s+hash|page\s+role|quality\s+(?:gate|status)|\bdebug\b|"
    r"\b(?:role|template|presentation|machine\s+metadata)\s*[:=]\s*[A-Za-z0-9_-]+|"
    r"\b(?:game|achievement|claim|evidence|asset)\s*[_ -]?id\b)",
    re.IGNORECASE,
)
_ROLE_MARKERS = {
    "opening", "hero", "archive-density", "evidence-ledger",
    "quantitative-comparison", "qualitative-comparison", "series-atlas",
    "pattern-atlas", "temporal-strata", "achievement-anomaly",
    "abstract-portrait", "closing",
}


def _issue(code: str, message: str, *, page: int | None = None, field: str = "") -> dict[str, Any]:
    item = {"code": str(code), "message": str(message)}
    if page is not None:
        item["page"] = page
    if field:
        item["field"] = field
    return item


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _validate_page(
    page: Mapping[str, Any],
    expected: int,
    width: int,
    height: int,
    all_game_pages: dict[str, list[int]],
    all_asset_pages: dict[str, list[int]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    number = page.get("page")
    page_number = int(number) if isinstance(number, int) else expected
    if number != expected:
        errors.append(_issue("page_number_not_consecutive", "pages must be numbered consecutively", page=page_number, field="page"))
    if not str(page.get("reader_title") or "").strip():
        errors.append(_issue("reader_title_missing", "publish pages need a reader title", page=page_number, field="reader_title"))
    if not str(page.get("composition") or "").strip():
        errors.append(_issue("composition_missing", "publish pages need an adaptive composition", page=page_number, field="composition"))
    owner = page.get("visible_identity_owner")
    if owner not in {"headline", "renderer", "none"}:
        errors.append(_issue("identity_owner_invalid", "visible identity owner is invalid", page=page_number, field="visible_identity_owner"))
    metadata = page.get("machine_metadata")
    if not isinstance(metadata, Mapping):
        errors.append(_issue("machine_metadata_missing", "internal role and evidence metadata must stay machine-owned", page=page_number, field="machine_metadata"))
    else:
        if not str(metadata.get("role") or "").strip() or not str(metadata.get("claim_id") or "").strip() or not str(metadata.get("evidence_hash") or "").startswith("sha256:"):
            errors.append(_issue("machine_metadata_incomplete", "machine metadata must retain role, claim, and evidence hash", page=page_number, field="machine_metadata"))
    visible_games = [str(value) for value in page.get("visible_game_ids", []) if str(value)]
    visible_assets = [str(value) for value in page.get("asset_ids", []) if str(value)]
    for game_id in visible_games:
        all_game_pages.setdefault(game_id, []).append(page_number)
    for asset_id in visible_assets:
        all_asset_pages.setdefault(asset_id, []).append(page_number)

    elements = page.get("elements")
    if not isinstance(elements, list) or not elements:
        errors.append(_issue("elements_missing", "publish pages need visible elements", page=page_number, field="elements"))
        elements = []
    label_count = 0
    for index, element in enumerate(elements):
        field = f"elements[{index}]"
        if not isinstance(element, Mapping):
            errors.append(_issue("element_invalid", "each publish element must be an object", page=page_number, field=field))
            continue
        if element.get("type") not in {"text", "image", "mark"}:
            errors.append(_issue("element_type_invalid", "publish elements may only be text, image, or mark", page=page_number, field=field))
        for coordinate in ("x", "y", "w", "h", "width", "height"):
            value = element.get(coordinate)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(_issue("element_geometry_invalid", "publish element geometry must be numeric", page=page_number, field=f"{field}.{coordinate}"))
        if isinstance(element.get("x"), (int, float)) and isinstance(element.get("w"), (int, float)) and element["x"] + element["w"] > width + 1:
            errors.append(_issue("element_outside_canvas", "publish element exceeds the working canvas", page=page_number, field=field))
        if isinstance(element.get("y"), (int, float)) and isinstance(element.get("h"), (int, float)) and element["y"] + element["h"] > height + 1:
            errors.append(_issue("element_outside_canvas", "publish element exceeds the working canvas", page=page_number, field=field))
        if element.get("type") == "text":
            text = str(element.get("text") or "")
            if not text.strip():
                errors.append(_issue("visible_text_empty", "visible text elements cannot be empty", page=page_number, field=field))
            if _INTERNAL_TEXT.search(text) or text.casefold().strip() in _ROLE_MARKERS:
                errors.append(_issue("publish_surface_internal_text", "visible text contains internal role, ID, hash, or debug language", page=page_number, field=field))
            if element.get("truncated") is True:
                errors.append(_issue("text_truncated", "key visible text must fit without truncation", page=page_number, field=field))
        if element.get("semantic_role") == "game-label":
            label_count += 1
        if element.get("requires_evidence") is True and not element.get("evidence_ids") and element.get("decorative") is not True:
            errors.append(_issue("element_evidence_missing", "non-decorative visible content needs evidence IDs", page=page_number, field=field))
        if element.get("type") == "image" and isinstance(element.get("scale_factor"), (int, float)) and float(element["scale_factor"]) > 1.5:
            errors.append(_issue("low_resolution_upscale", "an image is enlarged beyond the 1.5x safety limit", page=page_number, field=field))

    if owner == "renderer" and label_count != len(visible_games):
        errors.append(_issue("identity_count_invalid", "renderer-owned identity must label each visible game exactly once", page=page_number, field="elements"))
    if owner == "headline" and label_count:
        errors.append(_issue("identity_duplicate", "headline-owned identity must not add a second renderer label", page=page_number, field="elements"))
    if owner == "none" and visible_games:
        errors.append(_issue("identity_missing", "a page with visible games needs a single identity owner", page=page_number, field="visible_identity_owner"))

    metrics = page.get("layout_metrics")
    if not isinstance(metrics, Mapping):
        errors.append(_issue("layout_metrics_missing", "adaptive layout metrics are required", page=page_number, field="layout_metrics"))
    else:
        cards = metrics.get("card_content", [])
        if isinstance(cards, list):
            for card in cards:
                if isinstance(card, Mapping) and float(card.get("occupancy_ratio", 0.0) or 0.0) < 0.55:
                    errors.append(_issue("card_density_too_low", "card content occupies too little of its allocated area", page=page_number, field="layout_metrics.card_content"))
        if not metrics.get("lower_anchor"):
            errors.append(_issue("composition_unanchored", "the page has no lower-half visual or textual anchor", page=page_number, field="layout_metrics.lower_anchor"))
    return errors, {"page": page_number, "errors": errors, "warnings": []}


def validate_deck(
    layout: Mapping[str, Any],
    output_dir: str | Path,
    *,
    check_output_manifest: bool = True,
    art_direction: Mapping[str, Any] | None = None,
    visual_brief: Mapping[str, Any] | None = None,
    assets_manifest: Mapping[str, Any] | None = None,
    compiled_deck: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a current publish-layout artifact and its rendered output."""

    if not isinstance(layout, Mapping):
        raise TypeError("layout must be an object produced by compose_publish_layout")
    if layout.get("format") != "steam-visualogue-publish-layout":
        raise TypeError("validate_deck accepts only a publish-layout artifact")
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    try:
        validate_schema_document("publish-layout.json", "publish-layout.schema.json", dict(layout))
    except ValueError as exc:
        errors.append(_issue("schema_invalid", str(exc), field="publish-layout"))
    pages = layout.get("pages") if isinstance(layout.get("pages"), list) else []
    if not 12 <= len(pages) <= 18:
        errors.append(_issue("page_count_outside_required_range", "publish deck must contain 12–18 pages", field="pages"))
    try:
        locale = normalize_report_locale(str(layout.get("locale") or ""))
    except ValueError:
        locale = "en-US"
        errors.append(_issue("locale_invalid", "publish layout locale is unsupported", field="locale"))
    working = layout.get("working_size")
    width, height = (int(working[0]), int(working[1])) if isinstance(working, list) and len(working) == 2 and all(isinstance(value, int) for value in working) else (0, 0)
    if not width or not height:
        errors.append(_issue("working_size_invalid", "publish layout working_size is invalid", field="working_size"))

    if isinstance(compiled_deck, Mapping):
        try:
            validate_schema_document("compiled-deck.json", "compiled-deck.schema.json", dict(compiled_deck))
        except ValueError as exc:
            errors.append(_issue("compiled_deck_invalid", str(exc), field="compiled-deck"))
        for key in ("locale", "catalog_version", "label_fingerprint", "deck_schema_fingerprint", "compiled_deck_fingerprint"):
            if layout.get(key) != compiled_deck.get(key):
                errors.append(_issue("layout_compiled_mismatch", f"publish layout does not match current compiled deck {key}", field=key))

    if isinstance(art_direction, Mapping) and isinstance(visual_brief, Mapping) and isinstance(assets_manifest, Mapping):
        expected_compiled_fingerprint = str((compiled_deck or {}).get("compiled_deck_fingerprint") or layout.get("compiled_deck_fingerprint") or "")
        try:
            validate_schema_document("visual-brief.json", "visual-brief.schema.json", dict(visual_brief))
        except ValueError as exc:
            errors.append(_issue("visual_brief_invalid", str(exc), field="visual-brief"))
        if visual_brief.get("compiled_deck_fingerprint") != expected_compiled_fingerprint:
            errors.append(_issue("visual_brief_compiled_stale", "visual brief does not match the publish layout's compiled deck", field="visual_brief.compiled_deck_fingerprint"))
        if visual_brief.get("asset_manifest_fingerprint") != compute_asset_manifest_fingerprint(assets_manifest):
            errors.append(_issue("visual_brief_assets_stale", "visual brief does not match the current asset manifest", field="visual_brief.asset_manifest_fingerprint"))
        if visual_brief.get("visual_brief_fingerprint") != compute_visual_brief_fingerprint(visual_brief):
            errors.append(_issue("visual_brief_fingerprint_mismatch", "visual brief fingerprint does not match its contents", field="visual_brief.visual_brief_fingerprint"))
        expected_layout_input = compute_layout_input_fingerprint(
            expected_compiled_fingerprint,
            art_direction,
            str(visual_brief.get("visual_brief_fingerprint") or ""),
            assets_manifest,
        )
        if layout.get("layout_input_fingerprint") != expected_layout_input:
            errors.append(_issue("layout_input_fingerprint_mismatch", "publish layout does not match the current four layout inputs", field="layout_input_fingerprint"))
    elif any(value is not None for value in (art_direction, visual_brief, assets_manifest)):
        errors.append(_issue("layout_inputs_incomplete", "all four current layout inputs are required for independent freshness validation"))

    game_pages: dict[str, list[int]] = {}
    asset_pages: dict[str, list[int]] = {}
    page_reports: list[dict[str, Any]] = []
    for index, page in enumerate(pages, 1):
        if not isinstance(page, Mapping):
            issue = _issue("page_invalid", "publish page must be an object", page=index)
            errors.append(issue)
            page_reports.append({"page": index, "errors": [issue], "warnings": []})
            continue
        page_errors, report = _validate_page(page, index, width, height, game_pages, asset_pages)
        errors.extend(page_errors)
        page_reports.append(report)

    for _, numbers in game_pages.items():
        ordered = sorted(numbers)
        if len(ordered) > 2:
            errors.append(_issue("game_exposure_exceeded", "a game appears on more than two pages", page=ordered[2], field="visible_game_ids"))
        if len(ordered) == 2 and ordered[1] - ordered[0] < 2:
            errors.append(_issue("repeated_game_too_close", "repeated game exposure needs a page gap of at least two", page=ordered[1], field="visible_game_ids"))
    if pages and isinstance(pages[0], Mapping) and isinstance(pages[-1], Mapping):
        first = set(str(value) for value in pages[0].get("visible_game_ids", []))
        last = set(str(value) for value in pages[-1].get("visible_game_ids", []))
        if first & last:
            errors.append(_issue("opening_closing_identity_reuse", "opening and closing pages cannot share a game", page=len(pages), field="visible_game_ids"))
    for _, numbers in asset_pages.items():
        if len(numbers) > 1:
            errors.append(_issue("asset_reused", "an asset appears on more than one page", page=sorted(numbers)[1], field="asset_ids"))

    output_root = Path(output_dir)
    render_manifest = _load_json(output_root / ".render-manifest.json")
    if not isinstance(render_manifest, Mapping):
        if check_output_manifest:
            errors.append(_issue("render_manifest_missing", "render manifest is required"))
    else:
        if render_manifest.get("format") != "steam-visualogue-render-manifest":
            errors.append(_issue("render_manifest_format_invalid", "render manifest has an unsupported format"))
        for key in ("catalog_version", "label_fingerprint", "deck_schema_fingerprint", "compiled_deck_fingerprint", "visual_brief_fingerprint", "layout_input_fingerprint"):
            if render_manifest.get(key) != layout.get(key):
                errors.append(_issue("render_fingerprint_mismatch", f"render manifest does not match {key}", field=key))
        if render_manifest.get("locale") != locale:
            errors.append(_issue("render_locale_mismatch", "render manifest locale does not match publish layout", field="locale"))
        rendered_pages = render_manifest.get("pages") if isinstance(render_manifest.get("pages"), list) else []
        if len(rendered_pages) != len(pages):
            errors.append(_issue("render_page_count_mismatch", "render manifest does not cover every publish page"))
        for index, rendered in enumerate(rendered_pages, 1):
            if not isinstance(rendered, Mapping):
                continue
            filename = str(rendered.get("file") or "")
            if not filename or Path(filename).name != filename or Path(filename).is_absolute():
                errors.append(_issue("render_path_invalid", "render manifest contains an unsafe page path"))
                continue
            if not (output_root / filename).is_file():
                errors.append(_issue("render_page_missing", "rendered page is missing", field=filename))
            if filename != f"{index:02d}.png":
                errors.append(_issue("render_page_inventory_invalid", "render manifest page files must match the current page order", field=filename))

    if check_output_manifest:
        output_manifest = _load_json(output_root / "manifest.json")
        if not isinstance(output_manifest, Mapping):
            errors.append(_issue("output_manifest_missing", "output manifest is required"))
        else:
            try:
                validate_schema_document("output manifest", "output-manifest.schema.json", output_manifest)
            except ValueError:
                errors.append(_issue("output_manifest_schema_invalid", "output manifest does not satisfy its current schema"))
            if output_manifest.get("format") != "steam-visualogue-output-manifest":
                errors.append(_issue("output_manifest_format_invalid", "output manifest has an unsupported format"))
            for key in ("locale", "catalog_version", "label_fingerprint", "deck_schema_fingerprint", "compiled_deck_fingerprint", "visual_brief_fingerprint", "layout_input_fingerprint"):
                if output_manifest.get(key) != layout.get(key):
                    errors.append(_issue("output_fingerprint_mismatch", f"output manifest does not match {key}", field=key))
            if output_manifest.get("validation_ok") is not True:
                errors.append(_issue("output_validation_not_ok", "output manifest is not marked validation-successful"))
            output_pages = output_manifest.get("pages")
            if not isinstance(output_pages, list) or len(output_pages) != len(pages) or output_manifest.get("page_count") != len(output_pages):
                errors.append(_issue("output_page_count_mismatch", "output manifest does not cover every current page"))
            else:
                for index, row in enumerate(output_pages, 1):
                    if not isinstance(row, Mapping):
                        errors.append(_issue("output_page_inventory_invalid", "output manifest page entry is invalid", page=index))
                        continue
                    filename = str(row.get("file") or "")
                    candidate = output_root / filename
                    if not filename or Path(filename).name != filename or Path(filename).is_absolute():
                        errors.append(_issue("output_path_invalid", "output manifest contains an unsafe page path", page=index, field="pages"))
                        continue
                    if not candidate.is_file():
                        errors.append(_issue("output_page_missing", "output manifest references a missing page", page=index, field=filename))
                    elif row.get("sha256") != sha256_path_hex(candidate):
                        errors.append(_issue("output_page_hash_mismatch", "output manifest page hash does not match the rendered file", page=index, field=filename))
                    if filename != f"{index:02d}.png":
                        errors.append(_issue("output_page_inventory_invalid", "output manifest page files must match the current page order", page=index, field=filename))
            contact = output_manifest.get("contact_sheet")
            if not isinstance(contact, Mapping):
                errors.append(_issue("output_contact_missing", "output manifest contact sheet is required"))
            else:
                contact_name = str(contact.get("file") or "")
                contact_path = output_root / contact_name
                if not contact_name or Path(contact_name).name != contact_name or Path(contact_name).is_absolute():
                    errors.append(_issue("output_contact_path_invalid", "output manifest contact sheet path is unsafe"))
                elif not contact_path.is_file():
                    errors.append(_issue("output_contact_missing", "output manifest contact sheet is missing", field=contact_name))
                elif contact.get("sha256") != sha256_path_hex(contact_path):
                    errors.append(_issue("output_contact_hash_mismatch", "output manifest contact sheet hash does not match the rendered file", field=contact_name))

    report = {
        "format": "steam-visualogue-validation",
        "ok": not errors,
        "valid": not errors,
        "locale": locale,
        "catalog_version": layout.get("catalog_version") or catalog_for(locale).catalog_version,
        "label_fingerprint": layout.get("label_fingerprint"),
        "deck_schema_fingerprint": layout.get("deck_schema_fingerprint"),
        "compiled_deck_fingerprint": layout.get("compiled_deck_fingerprint"),
        "visual_brief_fingerprint": layout.get("visual_brief_fingerprint"),
        "layout_input_fingerprint": layout.get("layout_input_fingerprint"),
        "summary": {"pages": len(pages), "errors": len(errors), "warnings": len(warnings)},
        "errors": errors,
        "warnings": warnings,
        "pages": page_reports,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(output_root / "validation-report.json", report)
    return report


__all__ = ["validate_deck"]
