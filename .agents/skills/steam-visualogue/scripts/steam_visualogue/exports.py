"""Reader export and machine-readable output inventory for current artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from .golden import deck_pixel_sha256
from .context_budget import sha256_path_hex
from .io_utils import utc_now_iso, write_json, write_text
from .locales import catalog_for, normalize_report_locale
from .planning import validate_schema_document


def export_story_markdown(
    compiled_deck: Mapping[str, Any],
    destination: str | Path,
    *,
    report_locale: str | None = None,
) -> Path:
    """Export only reader copy from the final compiled deck, in page order."""

    if not isinstance(compiled_deck, Mapping) or compiled_deck.get("format") != "steam-visualogue-compiled-deck":
        raise ValueError("story export accepts only compiled-deck")
    locale = normalize_report_locale(str(report_locale or compiled_deck.get("locale") or "en-US"))
    if compiled_deck.get("locale") != locale:
        raise ValueError("compiled deck locale does not match story export locale")
    lines = [f"# {str(compiled_deck.get('title') or catalog_for(locale).text('product_name', 'Steam Visualogue'))}", ""]
    pages = compiled_deck.get("pages", [])
    if not isinstance(pages, list):
        raise ValueError("compiled deck pages must be an array")
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        number = int(page.get("page", 0))
        copy = page.get("reader_copy", {})
        if not isinstance(copy, Mapping):
            raise ValueError(f"page {number} has no reader copy")
        headline = str(copy.get("headline") or "").strip()
        if not headline:
            raise ValueError(f"page {number} has no reader headline")
        lines.extend([f"## {number:02d}", "", headline])
        for field in ("support", "caption"):
            value = str(copy.get(field) or "").strip()
            if value:
                lines.extend(["", value])
        lines.append("")
    return write_text(destination, "\n".join(lines).rstrip())


def _safe_file(path: Path) -> dict[str, Any]:
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": sha256_path_hex(path)}


def build_output_manifest(
    pages: Iterable[str | Path],
    *,
    contact_sheet: str | Path,
    layout: Mapping[str, Any],
    validation: Mapping[str, Any],
    destination: str | Path,
    input_assets: Iterable[Mapping[str, Any]] = (),
    report_locale: str | None = None,
    label_fingerprint: str | None = None,
) -> Path:
    """Write the public output inventory plus machine-only page semantics."""

    if not isinstance(layout, Mapping) or layout.get("format") != "steam-visualogue-publish-layout":
        raise ValueError("output manifest accepts only publish-layout")
    locale = normalize_report_locale(str(report_locale or layout.get("locale") or "en-US"))
    if layout.get("locale") != locale:
        raise ValueError("publish layout locale does not match output manifest locale")
    catalog_version = str(layout.get("catalog_version") or "")
    expected_label_fingerprint = str(layout.get("label_fingerprint") or "")
    visual_brief_fingerprint = str(layout.get("visual_brief_fingerprint") or "")
    layout_input_fingerprint = str(layout.get("layout_input_fingerprint") or "")
    if not expected_label_fingerprint or not visual_brief_fingerprint or not layout_input_fingerprint:
        raise ValueError("publish layout is missing current input fingerprints")
    if label_fingerprint is not None and str(label_fingerprint) != expected_label_fingerprint:
        raise ValueError("output manifest label fingerprint does not match publish layout")
    page_paths = [Path(path) for path in pages]
    contact_path = Path(contact_sheet)
    if not contact_path.is_file() or any(not path.is_file() for path in page_paths):
        raise FileNotFoundError("all rendered pages and the contact sheet must exist")
    assets_by_id: dict[str, dict[str, Any]] = {}
    for item in input_assets:
        if not isinstance(item, Mapping) or not item.get("asset_id"):
            continue
        assets_by_id[str(item["asset_id"])] = {
            str(key): item[key]
            for key in ("asset_id", "status", "source", "kind", "sha256", "pixel_sha256")
            if item.get(key) is not None
        }
    page_semantics: list[dict[str, Any]] = []
    for page in layout.get("pages", []):
        if not isinstance(page, Mapping):
            continue
        metadata = page.get("machine_metadata") if isinstance(page.get("machine_metadata"), Mapping) else {}
        page_semantics.append({
            "page": page.get("page"),
            "role": metadata.get("role"),
            "claim_id": metadata.get("claim_id"),
            "evidence_hash": metadata.get("evidence_hash"),
            "narrative_move": metadata.get("narrative_move"),
            "visible_game_ids": list(page.get("visible_game_ids", [])),
            "asset_ids": list(page.get("asset_ids", [])),
        })
    payload = {
        "format": "steam-visualogue-output-manifest",
        "locale": locale,
        "catalog_version": catalog_version,
        "label_fingerprint": expected_label_fingerprint,
        "deck_schema_fingerprint": layout.get("deck_schema_fingerprint"),
        "compiled_deck_fingerprint": layout.get("compiled_deck_fingerprint"),
        "visual_brief_fingerprint": visual_brief_fingerprint,
        "layout_input_fingerprint": layout_input_fingerprint,
        "generated_at": utc_now_iso(),
        "final_size": layout.get("final_size", [1080, 1440]),
        "page_count": len(page_paths),
        "validation_ok": validation.get("ok") is True,
        "pages": [_safe_file(path) for path in page_paths],
        "pixel_regression": {
            "algorithm": "rgb-pixels-sha256",
            "deck_sha256": deck_pixel_sha256(page_paths),
        },
        "contact_sheet": _safe_file(contact_path),
        "input_assets": [assets_by_id[key] for key in sorted(assets_by_id)],
        "page_semantics": page_semantics,
    }
    validate_schema_document("output manifest", "output-manifest.schema.json", payload)
    return write_json(destination, payload)


__all__ = ["build_output_manifest", "export_story_markdown"]
