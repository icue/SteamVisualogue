"""The three-gate quality workflow for the current report state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .context_budget import (
    BudgetViolation,
    MAX_FACTUAL_QUALITY_PAGES_PER_PACKET,
    MAX_FULL_RESOLUTION_PAGES_PER_PACKET,
    MAX_IMAGES_PER_PACKET,
    MAX_SOURCE_PIXELS_PER_PACKET,
    assert_merge_budget,
    assert_packet_budget,
    assert_reader_quality_packet_budget,
    metrics_for_path,
    sha256_path,
    sha256_path_hex,
)
from .io_utils import read_json, utc_now_iso, write_json
from .paths import skill_root
from .planning import validate_schema_document


GATES = ("reader", "visual", "factual")
_SEVERITIES = ("must-fix", "polish")
_STEAMID_PATTERN = re.compile(r"(?<!\d)\d{16,20}(?!\d)")
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PRIVATE_KEYS = {
    "api_key",
    "steamid",
    "steam_id",
    "identity",
    "identity_input",
    "profile_path",
    "cache_path",
    "source_path",
    "local_path",
    "temporary_source_path",
    "path",
}
_VERDICT_FIELDS = (
    "headline_gain",
    "support_gain",
    "identity_once",
    "reader_voice",
    "comparison_relation",
    "publish_surface",
)
_DECK_VERDICT_FIELDS = ("mode_fit", "progression", "claim_repetition", "opening_closing", "visual_rhythm")
_VAGUE_REASON_PATTERNS = (
    re.compile(r"^(?:reviewed(?: against the current packet)?|looks? good|all good|no issues?|pass(?:ed)?|ok|fine)[.! ]*$", re.IGNORECASE),
    re.compile(r"^(?:已检查|检查过了|没问题|没有问题|通过|符合要求|看起来不错)[。.!！ ]*$"),
    re.compile(r"placeholder|replace this|待填写|待补充", re.IGNORECASE),
)
_MUST_FIX_CATEGORIES = {
    "headline_gain",
    "headline_no_information_gain",
    "headline_repeats_visible_comparison",
    "caption_no_information_gain",
    "comparison_relation",
    "claim_repetition",
    "backstage-prose",
    "publish_surface",
    "identity_once",
    "unsupported-claim",
    "wrong-subject",
    "wrong-measure",
    "wrong-unit",
    "unreadable",
    "privacy",
    "privacy-violation",
    "low-resolution-upscale",
    "unbalanced-comparison",
    "card-density",
    "card-density-too-low",
    "composition-unanchored",
    "text-truncated",
    "comparison-no-relation",
    "opening-closing-repetition",
    "identity-duplicate",
    "scope-overclaim",
    "language-technical",
    "fact-mismatch",
    "factual-error",
    "evidence-mismatch",
    "subject-mismatch",
    "measure-mismatch",
    "unit-mismatch",
}


def _category_key(value: Any) -> str:
    """Normalize reviewer category spelling without changing its stored label."""

    return re.sub(r"[\s_]+", "-", str(value or "").casefold()).strip("-")


_MUST_FIX_CATEGORY_KEYS = {_category_key(category) for category in _MUST_FIX_CATEGORIES}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


class QualityGateError(ValueError):
    """A safe protocol or current-state error."""

    def __init__(self, code: str, message: str, *, gate: str | None = None, attempt_id: str | None = None, packet_id: str | None = None) -> None:
        self.code = str(code)
        self.message = str(message)
        self.gate = gate
        self.attempt_id = attempt_id
        self.packet_id = packet_id
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"status": "error", "code": self.code, "message": self.message, "gate": self.gate, "attempt_id": self.attempt_id, "packet_id": self.packet_id}


def quality_root(run_dir: str | Path) -> Path:
    return Path(run_dir) / ".agent-work" / "quality"


def _registry_path(root: Path) -> Path:
    return root / "registry.json"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _validate_quality_privacy(value: Any, key: str = "") -> None:
    normalized = "".join(character if character.isalnum() else "_" for character in key.casefold()).strip("_")
    if normalized in _PRIVATE_KEYS or any(token in normalized for token in ("api_key", "steamid", "identity_input")):
        raise ValueError("quality result contains a prohibited private field")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _validate_quality_privacy(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            _validate_quality_privacy(child, key)
    elif isinstance(value, str):
        if _SHA256_PATTERN.fullmatch(value):
            return
        if _STEAMID_PATTERN.search(value):
            raise ValueError("quality result contains a prohibited private identifier")
        if ("path" in normalized or "file" in normalized) and Path(value).is_absolute():
            raise ValueError("quality result contains an absolute local path")


def _gate(value: str) -> str:
    gate = str(value or "").strip().casefold()
    if gate not in GATES:
        raise QualityGateError("gate_invalid", "gate must be reader, visual, or factual", gate=gate or None)
    return gate


def _read_registry(root: Path) -> dict[str, Any]:
    path = _registry_path(root)
    if not path.is_file():
        return {"format": "steam-visualogue-quality-registry", "attempts": [], "current": {gate: None for gate in GATES}}
    try:
        document = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise QualityGateError("registry_invalid", f"quality registry is unreadable: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != "steam-visualogue-quality-registry":
        raise QualityGateError("registry_invalid", "quality registry has an unsupported format")
    document.setdefault("attempts", [])
    document.setdefault("current", {gate: None for gate in GATES})
    return document


def _write_registry(root: Path, registry: Mapping[str, Any]) -> None:
    write_json(_registry_path(root), dict(registry))


def _load_compiled(root: Path) -> dict[str, Any]:
    path = root / "compiled-deck.json"
    if not path.is_file():
        raise QualityGateError("compiled_deck_missing", "compiled-deck.json is required before quality starts")
    document = read_json(path)
    try:
        validate_schema_document("compiled-deck.json", "compiled-deck.schema.json", document)
    except ValueError as exc:
        raise QualityGateError("compiled_deck_invalid", str(exc)) from exc
    return document


def _load_layout(root: Path) -> dict[str, Any]:
    path = root / "publish-layout.json"
    if not path.is_file():
        raise QualityGateError("publish_layout_missing", "publish-layout.json is required for visual and factual gates")
    document = read_json(path)
    try:
        validate_schema_document("publish-layout.json", "publish-layout.schema.json", document)
    except ValueError as exc:
        raise QualityGateError("publish_layout_invalid", str(exc)) from exc
    return document


def _load_current_render(root: Path, compiled: Mapping[str, Any], layout: Mapping[str, Any]) -> tuple[dict[str, Any], Path]:
    """Load the render inventory used by the visual packet and fingerprint."""

    render_path = root / "output" / ".render-manifest.json"
    contact_path = root / ".agent-work" / "quality" / "contact-sheet.png"
    if not render_path.is_file():
        raise QualityGateError("render_missing", "a current render is required for the visual gate", gate="visual")
    if not contact_path.is_file():
        raise QualityGateError("quality_contact_sheet_missing", "the visual quality contact sheet is missing", gate="visual")
    try:
        render_document = read_json(render_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise QualityGateError("render_invalid", f"the current render manifest is unreadable: {exc}", gate="visual") from exc
    if not isinstance(render_document, Mapping) or render_document.get("format") != "steam-visualogue-render-manifest":
        raise QualityGateError("render_invalid", "the current render manifest has an unsupported format", gate="visual")
    for field, expected in (
        ("locale", layout.get("locale")),
        ("catalog_version", compiled.get("catalog_version")),
        ("label_fingerprint", compiled.get("label_fingerprint")),
        ("deck_schema_fingerprint", compiled.get("deck_schema_fingerprint")),
        ("compiled_deck_fingerprint", compiled.get("compiled_deck_fingerprint")),
        ("visual_brief_fingerprint", layout.get("visual_brief_fingerprint")),
        ("layout_input_fingerprint", layout.get("layout_input_fingerprint")),
    ):
        if render_document.get(field) != expected:
            raise QualityGateError("render_stale", f"the current render manifest does not match {field}", gate="visual")
    rendered_pages = render_document.get("pages")
    if not isinstance(rendered_pages, list) or len(rendered_pages) != len(_list(compiled.get("pages"))):
        raise QualityGateError("render_incomplete", "the current render does not cover every compiled page", gate="visual")
    for index, row in enumerate(rendered_pages, 1):
        filename = str(row.get("file") or "") if isinstance(row, Mapping) else ""
        candidate = Path(filename)
        if (
            not isinstance(row, Mapping)
            or not filename
            or candidate.name != filename
            or candidate.is_absolute()
            or filename != f"{index:02d}.png"
            or not (root / "output" / candidate).is_file()
        ):
            raise QualityGateError("render_incomplete", "the current render contains a missing or unsafe page file", gate="visual")
    return dict(render_document), contact_path


def _input_fingerprint(root: Path, gate: str) -> str:
    compiled = _load_compiled(root)
    if gate == "reader":
        payload = {
            "locale": compiled.get("locale"),
            "mode": compiled.get("mode"),
            "title": compiled.get("title"),
            "pages": [
                {
                    "page": page.get("page"),
                    "narrative_move": page.get("narrative_move"),
                    "reader_question": page.get("reader_question"),
                    "claim": page.get("claim"),
                    "reader_copy": page.get("reader_copy"),
                    "presentation": page.get("presentation"),
                    "evidence_ids": page.get("evidence_ids"),
                    "visible_identity_owner": page.get("visible_identity_owner"),
                    "encoded_claims": page.get("encoded_claims"),
                    "developed_claim_ids": page.get("developed_claim_ids"),
                }
                for page in compiled.get("pages", [])
                if isinstance(page, Mapping)
            ],
            "schema": compiled.get("deck_schema_fingerprint"),
        }
    elif gate == "factual":
        evidence_path = root / "evidence.json"
        if not evidence_path.is_file():
            raise QualityGateError("evidence_missing", "evidence.json is required for the factual gate", gate=gate)
        evidence = read_json(evidence_path)
        try:
            validate_schema_document("evidence.json", "evidence.schema.json", evidence)
        except ValueError as exc:
            raise QualityGateError("evidence_invalid", str(exc), gate=gate) from exc
        payload = {
            "compiled": compiled,
            "evidence": sha256_path(evidence_path),
            "schema": compiled.get("deck_schema_fingerprint"),
        }
    else:
        layout = _load_layout(root)
        render_manifest = root / "output" / ".render-manifest.json"
        contact = root / ".agent-work" / "quality" / "contact-sheet.png"
        if not render_manifest.is_file():
            raise QualityGateError("render_missing", "a current render is required for the visual gate", gate=gate)
        if not contact.is_file():
            raise QualityGateError("quality_contact_sheet_missing", "the visual quality contact sheet is missing", gate=gate)
        try:
            render_document = read_json(render_manifest)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise QualityGateError("render_invalid", f"the current render manifest is unreadable: {exc}", gate=gate) from exc
        if not isinstance(render_document, Mapping) or render_document.get("format") != "steam-visualogue-render-manifest":
            raise QualityGateError("render_invalid", "the current render manifest has an unsupported format", gate=gate)
        for field in ("locale", "catalog_version", "label_fingerprint", "deck_schema_fingerprint", "compiled_deck_fingerprint", "visual_brief_fingerprint", "layout_input_fingerprint"):
            expected = layout.get(field) if field in {"locale", "visual_brief_fingerprint", "layout_input_fingerprint"} else compiled.get(field)
            if render_document.get(field) != expected:
                raise QualityGateError("render_stale", f"the current render manifest does not match {field}", gate=gate)
        rendered_pages = render_document.get("pages")
        if not isinstance(rendered_pages, list) or len(rendered_pages) != len(compiled.get("pages", [])):
            raise QualityGateError("render_incomplete", "the current render does not cover every compiled page", gate=gate)
        rendered_hashes: list[dict[str, str]] = []
        for row in rendered_pages:
            filename = str(row.get("file") or "") if isinstance(row, Mapping) else ""
            candidate = Path(filename)
            if not isinstance(row, Mapping) or not filename or candidate.name != filename or candidate.is_absolute() or not (root / "output" / candidate).is_file():
                raise QualityGateError("render_incomplete", "the current render is missing a page file", gate=gate)
            rendered_hashes.append({"file": filename, "sha256": sha256_path(root / "output" / candidate)})
        payload = {
            "layout": layout,
            "render": sha256_path(render_manifest),
            "contact": sha256_path(contact),
            "pages": rendered_hashes,
        }
    return _digest(payload)


def _page_rows(compiled: Mapping[str, Any], gate: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in _list(compiled.get("pages")):
        if not isinstance(page, Mapping):
            continue
        row = {
            "page": page.get("page"),
            "narrative_move": page.get("narrative_move"),
            "reader_question": page.get("reader_question"),
            "claim": page.get("claim", {}),
            "reader_copy": page.get("reader_copy", {}),
            "visible_identity_owner": page.get("visible_identity_owner"),
            "visible_game_ids": page.get("visible_game_ids", []),
            "presentation_kind": page.get("presentation", {}).get("kind") if isinstance(page.get("presentation"), Mapping) else None,
            "evidence_ids": page.get("evidence_ids", []),
        }
        if gate == "factual":
            row["presentation_content"] = page.get("presentation", {}).get("content", {}) if isinstance(page.get("presentation"), Mapping) else {}
        if gate in {"visual", "factual"}:
            row.update({"asset_ids": page.get("asset_ids", []), "measure_bindings": page.get("measure_bindings", []), "item_bindings": page.get("item_bindings", [])})
        rows.append(row)
    return rows


def _page_number(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _collect_evidence_ids(value: Any, output: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "evidence_id" and isinstance(child, str) and child:
                output.add(child)
            elif key == "evidence_ids" and isinstance(child, list):
                output.update(str(item) for item in child if isinstance(item, str) and item)
            else:
                _collect_evidence_ids(child, output)
    elif isinstance(value, list):
        for child in value:
            _collect_evidence_ids(child, output)


def _evidence_records(evidence: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for collection in ("metrics", "games", "achievements", "patterns", "cards"):
        for item in _list(evidence.get(collection)):
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                records[str(item["id"])] = dict(item)
    return records


def _page_evidence_ids(page: Mapping[str, Any]) -> list[str]:
    identifiers: set[str] = set()
    _collect_evidence_ids(page.get("claim", {}), identifiers)
    identifiers.update(str(value) for value in _list(page.get("evidence_ids")) if isinstance(value, str) and value)
    _collect_evidence_ids(page.get("measure_bindings", []), identifiers)
    _collect_evidence_ids(page.get("item_bindings", []), identifiers)
    _collect_evidence_ids(page.get("presentation_content", {}), identifiers)
    return sorted(identifiers)


def _factual_label_bindings(page_rows: Iterable[Mapping[str, Any]], evidence: Mapping[str, Any], allowed_ids: Iterable[str]) -> dict[str, str]:
    records = _evidence_records(evidence)
    allowed = {str(value) for value in allowed_ids}
    labels: dict[str, str] = {}
    for identifier in sorted(allowed):
        record = records.get(identifier, {})
        for fact in _list(record.get("facts")):
            if not isinstance(fact, Mapping) or str(fact.get("name") or "") not in {"name", "display_name", "title"}:
                continue
            value = fact.get("value")
            if isinstance(value, str) and value.strip():
                labels[identifier] = value.strip()
                break
    content_labels: dict[str, str] = {}
    for page in page_rows:
        content = page.get("presentation_content", {})
        _collect_quality_labels(content, content_labels)
    for identifier, label in content_labels.items():
        if identifier in allowed:
            labels.setdefault(identifier, label)
    return dict(sorted(labels.items()))


def _collect_quality_labels(value: Any, labels: dict[str, str]) -> None:
    if isinstance(value, Mapping):
        identifier = value.get("game_id") or value.get("achievement_id") or value.get("evidence_id")
        label = value.get("display_name") or value.get("name") or value.get("title")
        if isinstance(identifier, str) and isinstance(label, str) and label.strip():
            labels.setdefault(identifier, label.strip())
        for child in value.values():
            _collect_quality_labels(child, labels)
    elif isinstance(value, list):
        for child in value:
            _collect_quality_labels(child, labels)


def _image_descriptor(root: Path, path: Path, *, kind: str, page: int | None = None) -> dict[str, Any]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except (OSError, ValueError) as exc:
        raise QualityGateError("quality_image_invalid", "a quality image is unreadable", gate="visual") from exc
    descriptor: dict[str, Any] = {
        "kind": kind,
        "file": path.resolve().relative_to(root.resolve()).as_posix(),
        "sha256": sha256_path(path),
        "width": int(width),
        "height": int(height),
    }
    if page is not None:
        descriptor["page"] = page
    return descriptor


def _visual_page_rows(root: Path, rows: list[dict[str, Any]], layout: Mapping[str, Any], render: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    layout_by_page = {
        _page_number(item.get("page")): item
        for item in _list(layout.get("pages"))
        if isinstance(item, Mapping) and _page_number(item.get("page")) is not None
    }
    rendered_by_page = {
        _page_number(item.get("page")): item
        for item in _list(render.get("pages"))
        if isinstance(item, Mapping) and _page_number(item.get("page")) is not None
    }
    visual_rows: list[dict[str, Any]] = []
    image_descriptors: list[dict[str, Any]] = []
    total_pixels = 0
    for row in rows:
        page_number = _page_number(row.get("page"))
        layout_row = layout_by_page.get(page_number)
        render_row = rendered_by_page.get(page_number)
        if page_number is None or not isinstance(layout_row, Mapping) or not isinstance(render_row, Mapping):
            raise QualityGateError("quality_visual_binding_missing", "visual quality data does not cover every page", gate="visual")
        filename = str(render_row.get("file") or "")
        rendered_path = root / "output" / filename
        descriptor = _image_descriptor(root, rendered_path, kind="rendered-page", page=page_number)
        total_pixels += int(descriptor["width"]) * int(descriptor["height"])
        image_descriptors.append(descriptor)
        element_bindings = [
            {
                "element_id": item.get("id"),
                "asset_id": item.get("asset_id"),
                "type": item.get("type"),
            }
            for item in _list(layout_row.get("elements"))
            if isinstance(item, Mapping) and item.get("type") == "image" and item.get("asset_id")
        ]
        visual_rows.append({
            **row,
            "layout_row": dict(layout_row),
            "rendered_page": descriptor,
            "asset_bindings": element_bindings,
            "composition_metadata": {
                "composition": layout_row.get("composition"),
                "layout_metrics": layout_row.get("layout_metrics", {}),
                "machine_metadata": layout_row.get("machine_metadata", {}),
            },
        })
    return visual_rows, image_descriptors, total_pixels


def _quality_page_groups(rows: list[dict[str, Any]], gate: str, *, root: Path, layout: Mapping[str, Any] | None = None, render: Mapping[str, Any] | None = None) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
    """Build deterministic quality packet groups and their deck-level context."""

    deck_context: dict[str, Any] = {}
    if gate == "reader":
        deck_context.update({"locale": None, "mode": None, "title": None})
        try:
            compiled = _load_compiled(root)
            deck_context.update({"locale": compiled.get("locale"), "mode": compiled.get("mode"), "title": compiled.get("title")})
        except QualityGateError:
            pass
        assert_reader_quality_packet_budget({"pages": rows, **deck_context})
        return [rows], deck_context
    if gate == "visual":
        if layout is None or render is None:
            raise QualityGateError("quality_visual_context_missing", "visual quality context is missing", gate=gate)
        visual_rows, image_descriptors, _ = _visual_page_rows(root, rows, layout, render)
        contact_path = root / ".agent-work" / "quality" / "contact-sheet.png"
        contact_descriptor = _image_descriptor(root, contact_path, kind="contact-sheet")
        deck_context.update({
            "contact_sheet": contact_descriptor,
            "deck_images": image_descriptors,
            "layout_metadata": layout.get("machine_metadata", {}),
        })
        max_pages = MAX_FULL_RESOLUTION_PAGES_PER_PACKET
        groups: list[list[dict[str, Any]]] = []
        for row in visual_rows:
            candidate = (groups[-1] if groups else []) + [row]
            candidate_pixels = sum(
                int(item["rendered_page"]["width"]) * int(item["rendered_page"]["height"])
                for item in candidate
            )
            probe_exceeds_budget = False
            if len(candidate) > max_pages or 1 + len(candidate) > MAX_IMAGES_PER_PACKET or candidate_pixels > MAX_SOURCE_PIXELS_PER_PACKET:
                probe_exceeds_budget = True
            else:
                try:
                    _quality_packet("visual", "visual-01", "probe", "sha256:" + "0" * 64, candidate, deck_context)
                except (BudgetViolation, QualityGateError):
                    probe_exceeds_budget = True
            if groups and probe_exceeds_budget:
                groups.append([row])
            elif not groups and probe_exceeds_budget:
                raise QualityGateError("quality_visual_packet_too_large", "one rendered page exceeds the visual packet budget", gate=gate)
            else:
                if not groups:
                    groups.append([])
                groups[-1].append(row)
        for group in groups:
            _quality_packet("visual", "visual-01", "probe", "sha256:" + "0" * 64, group, deck_context)
        return groups, deck_context
    evidence_path = root / "evidence.json"
    evidence = read_json(evidence_path)
    all_ids: set[str] = set()
    for row in rows:
        row["evidence_ids"] = _page_evidence_ids(row)
        all_ids.update(row["evidence_ids"])
    records = _evidence_records(evidence)
    if set(records) & all_ids != all_ids:
        raise QualityGateError("evidence_closure_incomplete", "factual quality evidence is not a closed set", gate=gate)
    closure = {identifier: records[identifier] for identifier in sorted(all_ids)}
    deck_context.update({
        "locale": _load_compiled(root).get("locale"),
        "mode": _load_compiled(root).get("mode"),
        "title": _load_compiled(root).get("title"),
        "evidence_closure": closure,
        "allowed_evidence_ids": sorted(all_ids),
        "labels": _factual_label_bindings(rows, evidence, all_ids),
    })
    groups = [rows[index:index + MAX_FACTUAL_QUALITY_PAGES_PER_PACKET] for index in range(0, len(rows), MAX_FACTUAL_QUALITY_PAGES_PER_PACKET)]
    for group in groups:
        assert_packet_budget({"pages": group, **_factual_context_for_rows(deck_context, group)}, item_count=len(group))
    return groups, deck_context


def _factual_context_for_rows(deck_context: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    allowed = sorted({identifier for row in rows for identifier in _list(row.get("evidence_ids")) if isinstance(identifier, str)})
    closure = deck_context.get("evidence_closure", {})
    labels = deck_context.get("labels", {})
    return {
        "locale": deck_context.get("locale"),
        "mode": deck_context.get("mode"),
        "title": deck_context.get("title"),
        "evidence_closure": {identifier: closure[identifier] for identifier in allowed if isinstance(closure, Mapping) and identifier in closure},
        "allowed_evidence_ids": allowed,
        "labels": {identifier: labels[identifier] for identifier in allowed if isinstance(labels, Mapping) and identifier in labels},
    }


def _quality_packet(
    gate: str,
    attempt_id: str,
    packet_id: str,
    fingerprint: str,
    rows: list[dict[str, Any]],
    deck_context: Mapping[str, Any],
) -> dict[str, Any]:
    common_page_fields = (
        "page",
        "narrative_move",
        "reader_question",
        "claim",
        "reader_copy",
        "visible_identity_owner",
        "visible_game_ids",
        "presentation_kind",
        "evidence_ids",
        "asset_ids",
        "measure_bindings",
        "item_bindings",
        "presentation_content",
        "layout_row",
        "rendered_page",
        "asset_bindings",
        "composition_metadata",
    )
    pages = [
        {key: row.get(key) for key in common_page_fields if key in row}
        for row in rows
    ]
    packet: dict[str, Any] = {
        "format": "steam-visualogue-quality-packet",
        "gate": gate,
        "attempt_id": attempt_id,
        "packet_id": packet_id,
        "input_fingerprint": fingerprint,
        "pages": pages,
        "required_page_ids": [int(row["page"]) for row in rows],
        "rubric": _rubric(gate),
    }
    if gate == "reader":
        packet.update({
            "locale": deck_context.get("locale"),
            "mode": deck_context.get("mode"),
            "title": deck_context.get("title"),
            "deck_progression": [
                {
                    "page": row.get("page"),
                    "narrative_move": row.get("narrative_move"),
                    "reader_question": row.get("reader_question"),
                    "claim_id": row.get("claim", {}).get("claim_id") if isinstance(row.get("claim"), Mapping) else None,
                    "develops": row.get("claim", {}).get("develops", []) if isinstance(row.get("claim"), Mapping) else [],
                }
                for row in rows
            ],
        })
    elif gate == "visual":
        rendered_pages = [dict(row["rendered_page"]) for row in rows]
        packet.update({
            "contact_sheet": deck_context.get("contact_sheet"),
            "images": [deck_context.get("contact_sheet"), *rendered_pages],
            "rendered_pages": rendered_pages,
            "layout_rows": [dict(row["layout_row"]) for row in rows],
            "asset_bindings": [
                {"page": row.get("page"), **binding}
                for row in rows
                for binding in _list(row.get("asset_bindings"))
                if isinstance(binding, Mapping)
            ],
            "composition_metadata": [
                {"page": row.get("page"), **dict(row.get("composition_metadata", {}))}
                for row in rows
            ],
        })
    else:
        factual_context = _factual_context_for_rows(deck_context, rows)
        packet.update({
            "locale": factual_context.get("locale"),
            "mode": factual_context.get("mode"),
            "title": factual_context.get("title"),
            "labels": dict(factual_context.get("labels", {})),
            "evidence_closure": dict(factual_context.get("evidence_closure", {})),
            "allowed_evidence_ids": list(factual_context.get("allowed_evidence_ids", [])),
        })
    template = _result_template(gate, attempt_id, packet_id, fingerprint, rows)
    packet["result_template"] = template
    try:
        _validate_quality_privacy(packet)
    except ValueError as exc:
        raise QualityGateError("quality_packet_privacy_invalid", "quality packet contains prohibited private data", gate=gate, attempt_id=attempt_id, packet_id=packet_id) from exc
    if gate == "reader":
        assert_reader_quality_packet_budget(packet)
    else:
        image_count = 1 + len(rows) if gate == "visual" else 0
        total_pixels = (
            sum(int(row["rendered_page"]["width"]) * int(row["rendered_page"]["height"]) for row in rows)
            if gate == "visual"
            else 0
        )
        assert_packet_budget(packet, item_count=len(rows), image_count=image_count, total_pixels=total_pixels)
    try:
        validate_schema_document("quality packet", "quality-packet.schema.json", packet)
        validate_schema_document("quality result", "quality-result.schema.json", template)
    except ValueError as exc:
        raise QualityGateError(
            "quality_contract_invalid",
            str(exc),
            gate=gate,
            attempt_id=attempt_id,
            packet_id=packet_id,
        ) from exc
    return packet


def _rubric(gate: str) -> dict[str, Any]:
    rubric = {
        "gate": gate,
        "finding_severities": list(_SEVERITIES),
        "page_verdicts": list(_VERDICT_FIELDS),
        "deck_verdicts": list(_DECK_VERDICT_FIELDS),
        "must_fix_categories": sorted(_MUST_FIX_CATEGORIES),
        "instruction": "Return a concrete pass, fail, or not-applicable status and a specific reason for every required verdict.",
    }
    if gate == "reader":
        rubric["caption_policy"] = (
            "Treat caption as not-applicable when omitted. If present, require an explicit "
            "non-obvious-visual reason and confirm that it adds interpretation beyond the "
            "headline, support, claim, and visible encoding; generic or redundant captions "
            "are must-fix."
        )
    return rubric


def _empty_verdict(reason: str = "Reviewed against the current packet.", status: str = "pass") -> dict[str, str]:
    return {"status": status, "reason": reason}


def _reason_is_vague(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or any(pattern.search(text) for pattern in _VAGUE_REASON_PATTERNS)


def _result_template(gate: str, attempt_id: str, packet_id: str, fingerprint: str, pages: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    page_results: list[dict[str, Any]] = []
    for page in pages:
        verdicts = {field: _empty_verdict() for field in _VERDICT_FIELDS}
        if page.get("presentation_kind") not in {"quantitative-comparison", "qualitative-comparison"}:
            verdicts["comparison_relation"] = _empty_verdict(
                "Not applicable because this page is not a comparison.",
                "not-applicable",
            )
        page_results.append({"page": int(page["page"]), **verdicts})
    return {
        "format": "steam-visualogue-quality-result",
        "gate": gate,
        "attempt_id": attempt_id,
        "packet_id": packet_id,
        "input_fingerprint": fingerprint,
        "pages": page_results,
        "deck_verdict": {field: _empty_verdict() for field in _DECK_VERDICT_FIELDS},
        "findings": [],
        "recommended_changes": [],
    }


def _attempt_directory(root: Path, attempt_id: str) -> Path:
    return root / "attempts" / attempt_id


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root.parent.parent).as_posix()


def _attempt_entry(registry: Mapping[str, Any], attempt_id: str) -> dict[str, Any] | None:
    for item in _list(registry.get("attempts")):
        if isinstance(item, Mapping) and item.get("attempt_id") == attempt_id:
            return dict(item)
    return None


def _current_attempt(registry: Mapping[str, Any], gate: str) -> dict[str, Any] | None:
    current = registry.get("current")
    if isinstance(current, Mapping) and current.get(gate):
        return _attempt_entry(registry, str(current[gate]))
    return None


def _new_attempt_id(gate: str, registry: Mapping[str, Any]) -> str:
    used = {str(item.get("attempt_id")) for item in registry.get("attempts", []) if isinstance(item, Mapping)}
    for cycle in (1, 2):
        candidate = f"{gate}-0{cycle}"
        if candidate not in used:
            return candidate
    raise QualityGateError("quality_budget_exhausted", f"{gate} already used both quality attempts", gate=gate)


def start_quality_gate(run_dir: str | Path, gate: str) -> dict[str, Any]:
    root = quality_root(run_dir)
    normalized = _gate(gate)
    root.mkdir(parents=True, exist_ok=True)
    run_root = Path(run_dir)
    compiled = _load_compiled(run_root)
    fingerprint = _input_fingerprint(run_root, normalized)
    registry = _read_registry(root)
    current = _current_attempt(registry, normalized)
    if current:
        if current.get("status") == "stopped":
            raise QualityGateError("quality_budget_exhausted", "the second substantive attempt stopped this gate", gate=normalized, attempt_id=str(current.get("attempt_id")))
        if current.get("input_fingerprint") == fingerprint:
            if current.get("status") in {"active", "passed"}:
                return _summary(root, current)
            if current.get("status") == "revision-required":
                raise QualityGateError("input_unchanged", "the quality input fingerprint must change before a new cycle", gate=normalized, attempt_id=str(current.get("attempt_id")))
    attempt_id = _new_attempt_id(normalized, registry)
    attempt_root = _attempt_directory(root, attempt_id)
    packet_root = attempt_root / "packets"
    result_root = attempt_root / "results"
    receipt_root = attempt_root / "receipts"
    for directory in (packet_root, result_root, receipt_root):
        directory.mkdir(parents=True, exist_ok=True)
    rows = _page_rows(compiled, normalized)
    layout = _load_layout(run_root) if normalized in {"visual", "factual"} else None
    render = None
    if normalized == "visual":
        render, _ = _load_current_render(run_root, compiled, layout or {})
    try:
        groups, deck_context = _quality_page_groups(rows, normalized, root=run_root, layout=layout, render=render)
    except BudgetViolation as exc:
        raise QualityGateError(
            "quality_packet_budget_exceeded",
            "quality packet exceeds a fixed packet budget",
            gate=normalized,
            attempt_id=attempt_id,
        ) from exc
    packet_rows: list[dict[str, Any]] = []
    for index, group in enumerate(groups, 1):
        packet_id = f"{normalized}-deck" if normalized == "reader" else f"{normalized}-{index:02d}"
        try:
            packet = _quality_packet(normalized, attempt_id, packet_id, fingerprint, group, deck_context)
        except BudgetViolation as exc:
            raise QualityGateError(
                "quality_packet_budget_exceeded",
                "quality packet exceeds a fixed packet budget",
                gate=normalized,
                attempt_id=attempt_id,
                packet_id=packet_id,
            ) from exc
        packet_path = packet_root / f"{packet_id}.json"
        result_path = result_root / f"{packet_id}.json"
        write_json(packet_path, packet, compact=True, sort_keys=True)
        write_json(result_path, packet["result_template"], compact=True, sort_keys=True)
        if normalized == "reader":
            packet_metrics = assert_reader_quality_packet_budget(packet)
        else:
            packet_metrics = assert_packet_budget(
                packet,
                item_count=len(group),
                image_count=1 + len(group) if normalized == "visual" else 0,
                total_pixels=(
                    sum(int(row["rendered_page"]["width"]) * int(row["rendered_page"]["height"]) for row in group)
                    if normalized == "visual"
                    else 0
                ),
            )
        packet_rows.append({
            "packet_id": packet_id,
            "packet_path": _relative(root, packet_path),
            "result_path": _relative(root, result_path),
            "receipt_path": _relative(root, receipt_root / f"{packet_id}.json"),
            "required_page_ids": [int(row["page"]) for row in group],
            "packet_utf8_bytes": packet_metrics.utf8_bytes,
        })
    page_ids = [int(row["page"]) for row in rows if isinstance(row.get("page"), int)]
    manifest = {
        "format": "steam-visualogue-quality-attempt",
        "gate": normalized,
        "attempt_id": attempt_id,
        "cycle": int(attempt_id[-2:]),
        "input_fingerprint": fingerprint,
        "status": "active",
        "started_at": utc_now_iso(),
        "required_page_ids": page_ids,
        "packets": packet_rows,
    }
    write_json(attempt_root / "manifest.json", manifest)
    entry = {"attempt_id": attempt_id, "gate": normalized, "cycle": manifest["cycle"], "input_fingerprint": fingerprint, "status": "active", "started_at": manifest["started_at"], "manifest_path": _relative(root, attempt_root / "manifest.json"), "merged_path": None}
    attempts = [dict(item) for item in registry.get("attempts", []) if isinstance(item, Mapping)]
    attempts.append(entry)
    current_map = dict(registry.get("current", {}))
    current_map[normalized] = attempt_id
    registry = {**registry, "attempts": attempts, "current": current_map}
    _write_registry(root, registry)
    return _summary(root, entry)


def _summary(root: Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    manifest_path = root.parent.parent / str(entry.get("manifest_path") or "")
    manifest = read_json(manifest_path) if manifest_path.is_file() else {}
    assignments = []
    for packet in _list(manifest.get("packets")):
        if not isinstance(packet, Mapping):
            continue
        assignments.append({
            "gate": entry.get("gate"),
            "attempt_id": entry.get("attempt_id"),
            "packet_id": packet.get("packet_id"),
            "packet_path": packet.get("packet_path"),
            "result_path": packet.get("result_path"),
            "submit_command": f'python -B "{(skill_root() / "scripts" / "run.py").as_posix()}" quality-submit --run-dir <run> --attempt {entry.get("attempt_id")} --packet-id {packet.get("packet_id")}',
        })
    return {"status": entry.get("status"), "gate": entry.get("gate"), "attempt_id": entry.get("attempt_id"), "cycle": entry.get("cycle"), "input_fingerprint": entry.get("input_fingerprint"), "assignments": assignments, "merged_path": entry.get("merged_path")}


def _manifest_for_attempt(root: Path, attempt_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    registry = _read_registry(root)
    entry = _attempt_entry(registry, attempt_id)
    if not entry:
        raise QualityGateError("attempt_not_found", f"unknown quality attempt '{attempt_id}'", attempt_id=attempt_id)
    path = (root.parent.parent / str(entry.get("manifest_path") or "")).resolve()
    attempt_root = (root / "attempts" / attempt_id).resolve()
    if not path.is_relative_to(attempt_root):
        raise QualityGateError("attempt_manifest_invalid", "quality attempt manifest escapes its attempt directory", attempt_id=attempt_id)
    if not path.is_file():
        raise QualityGateError("attempt_manifest_missing", "quality attempt manifest is missing", attempt_id=attempt_id)
    manifest = read_json(path)
    if not isinstance(manifest, dict):
        raise QualityGateError("attempt_manifest_invalid", "quality attempt manifest is invalid", attempt_id=attempt_id)
    return registry, manifest


def _result_path(root: Path, manifest: Mapping[str, Any], packet_id: str) -> Path:
    for packet in _list(manifest.get("packets")):
        if isinstance(packet, Mapping) and packet.get("packet_id") == packet_id:
            value = root.parent.parent / str(packet.get("result_path") or "")
            attempt_root = (root / "attempts" / str(manifest.get("attempt_id"))).resolve()
            if value.resolve().is_relative_to(attempt_root) and value.name.endswith(".json"):
                return value
    raise QualityGateError("packet_not_found", f"packet '{packet_id}' is not assigned to this attempt", gate=str(manifest.get("gate")), attempt_id=str(manifest.get("attempt_id")), packet_id=packet_id)


def _packet_path(root: Path, manifest: Mapping[str, Any], packet_id: str) -> Path:
    for packet in _list(manifest.get("packets")):
        if isinstance(packet, Mapping) and packet.get("packet_id") == packet_id:
            value = root.parent.parent / str(packet.get("packet_path") or "")
            attempt_root = (root / "attempts" / str(manifest.get("attempt_id"))).resolve()
            if value.resolve().is_relative_to(attempt_root) and value.name.endswith(".json"):
                return value
    raise QualityGateError("packet_not_found", f"packet '{packet_id}' is not assigned to this attempt", gate=str(manifest.get("gate")), attempt_id=str(manifest.get("attempt_id")), packet_id=packet_id)


def _protocol_issues(root: Path, manifest: Mapping[str, Any], packet_id: str, result: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    gate = str(manifest.get("gate"))
    attempt_id = str(manifest.get("attempt_id"))
    if not isinstance(result, Mapping):
        return [{"code": "result_not_object", "message": "result must be a JSON object"}]
    try:
        _validate_quality_privacy(result)
    except ValueError as exc:
        return [{"code": "privacy_violation", "message": str(exc)}]
    try:
        validate_schema_document("quality result", "quality-result.schema.json", result)
    except ValueError as exc:
        issues.append({"code": "schema_invalid", "message": str(exc)})
        return issues
    for field, expected in (("gate", gate), ("attempt_id", attempt_id), ("input_fingerprint", manifest.get("input_fingerprint"))):
        if result.get(field) != expected:
            issues.append({"code": "binding_mismatch", "message": f"result {field} does not match the assigned packet"})
    if result.get("packet_id") != packet_id:
        issues.append({"code": "binding_mismatch", "message": "result packet_id does not match the assigned packet"})
    packet_pages: set[int] = set()
    try:
        packet_path = _packet_path(root, manifest, packet_id)
    except QualityGateError:
        issues.append({"code": "packet_missing", "message": "the assigned quality packet is missing"})
        return issues
    if not packet_path.is_file():
        issues.append({"code": "packet_missing", "message": "the assigned quality packet is missing"})
        return issues
    try:
        packet = read_json(packet_path)
        validate_schema_document("quality packet", "quality-packet.schema.json", packet)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        issues.append({"code": "packet_invalid", "message": str(exc)})
        return issues
    if (
        not isinstance(packet, Mapping)
        or packet.get("gate") != gate
        or packet.get("attempt_id") != attempt_id
        or packet.get("packet_id") != packet_id
        or packet.get("input_fingerprint") != manifest.get("input_fingerprint")
    ):
        issues.append({"code": "packet_binding_mismatch", "message": "quality packet does not match the assigned attempt"})
    packet_pages = {int(value) for value in packet.get("required_page_ids", []) if isinstance(value, int)}
    packet_page_kinds = {
        int(row.get("page")): str(row.get("presentation_kind") or "")
        for row in packet.get("pages", [])
        if isinstance(row, Mapping) and isinstance(row.get("page"), int)
    }
    result_pages = result.get("pages") if isinstance(result.get("pages"), list) else []
    result_ids = [int(page.get("page")) for page in result_pages if isinstance(page, Mapping) and isinstance(page.get("page"), int)]
    if set(result_ids) != packet_pages or len(result_ids) != len(set(result_ids)):
        issues.append({"code": "page_coverage_incomplete", "message": "result must cover each assigned page exactly once"})
    for page in result_pages:
        if not isinstance(page, Mapping):
            continue
        number = page.get("page")
        for field in _VERDICT_FIELDS:
            verdict = page.get(field)
            if not isinstance(verdict, Mapping) or verdict.get("status") not in {"pass", "fail", "not-applicable"} or _reason_is_vague(verdict.get("reason")):
                issues.append({"code": "verdict_invalid", "message": f"page {number} requires a status and concrete reason for {field}"})
        comparison = page.get("comparison_relation")
        is_comparison = packet_page_kinds.get(number) in {"quantitative-comparison", "qualitative-comparison"}
        if isinstance(comparison, Mapping):
            expected_status = {"pass", "fail"} if is_comparison else {"not-applicable"}
            if comparison.get("status") not in expected_status:
                issues.append({"code": "comparison_verdict_invalid", "message": f"page {number} comparison_relation must be {'pass or fail' if is_comparison else 'not-applicable'}"})
    deck_verdict = result.get("deck_verdict")
    for field in _DECK_VERDICT_FIELDS:
        verdict = deck_verdict.get(field) if isinstance(deck_verdict, Mapping) else None
        if not isinstance(verdict, Mapping) or verdict.get("status") not in {"pass", "fail", "not-applicable"} or _reason_is_vague(verdict.get("reason")):
            issues.append({"code": "verdict_invalid", "message": f"deck verdict requires a status and concrete reason for {field}"})
    pages_by_id = packet_pages
    for finding in _list(result.get("findings")):
        if not isinstance(finding, Mapping):
            issues.append({"code": "finding_invalid", "message": "each finding must be an object"})
            continue
        category = str(finding.get("category") or "")
        severity = str(finding.get("severity") or "")
        if severity not in _SEVERITIES:
            issues.append({"code": "finding_severity_invalid", "message": "finding severity must be must-fix or polish"})
        if _category_key(category) in _MUST_FIX_CATEGORY_KEYS and severity != "must-fix":
            issues.append({"code": "finding_floor", "message": f"{category} is fixed at must-fix and cannot be downgraded"})
        locations = finding.get("locations")
        if not isinstance(locations, list) or not locations:
            issues.append({"code": "finding_location_missing", "message": "each finding needs at least one location"})
        else:
            for location in locations:
                if not isinstance(location, Mapping) or location.get("page") not in pages_by_id or not str(location.get("field") or "").strip():
                    issues.append({"code": "finding_location_invalid", "message": "finding locations must refer to an assigned page and field"})
        if not str(finding.get("explanation") or "").strip() or not str(finding.get("recommendation") or "").strip():
            issues.append({"code": "finding_reason_missing", "message": "findings need an explanation and recommendation"})
    failed_pages = {
        int(page.get("page"))
        for page in result_pages
        if isinstance(page, Mapping)
        and isinstance(page.get("page"), int)
        and any(isinstance(page.get(field), Mapping) and page[field].get("status") == "fail" for field in _VERDICT_FIELDS)
    }
    failed_deck = bool(
        isinstance(deck_verdict, Mapping)
        and any(isinstance(value, Mapping) and value.get("status") == "fail" for value in deck_verdict.values())
    )
    concrete_findings = [finding for finding in _list(result.get("findings")) if isinstance(finding, Mapping)]
    for page in result_pages:
        if not isinstance(page, Mapping) or not isinstance(page.get("page"), int):
            continue
        number = int(page["page"])
        for field in _VERDICT_FIELDS:
            verdict = page.get(field)
            if not isinstance(verdict, Mapping) or verdict.get("status") != "fail":
                continue
            if not any(
                finding.get("severity") == "must-fix"
                and any(isinstance(location, Mapping) and location.get("page") == number for location in _list(finding.get("locations")))
                for finding in concrete_findings
            ):
                issues.append({"code": "verdict_finding_mismatch", "message": f"page {number} has a failed {field} verdict without a locating must-fix finding"})
    for finding in concrete_findings:
        if finding.get("severity") != "must-fix":
            continue
        locations = [location for location in _list(finding.get("locations")) if isinstance(location, Mapping)]
        if not locations:
            continue
        if not any(int(location["page"]) in failed_pages for location in locations if isinstance(location.get("page"), int)) and not failed_deck:
            issues.append({"code": "verdict_finding_mismatch", "message": "a must-fix finding must be backed by a failed page or deck verdict"})
    return issues


def submit_quality_result(run_dir: str | Path, attempt_id: str, packet_id: str) -> dict[str, Any]:
    root = quality_root(run_dir)
    _, manifest = _manifest_for_attempt(root, attempt_id)
    if manifest.get("status") != "active":
        raise QualityGateError("attempt_not_active", "only an active quality attempt accepts results", gate=str(manifest.get("gate")), attempt_id=attempt_id)
    gate = _gate(str(manifest.get("gate") or ""))
    if _input_fingerprint(root.parent.parent, gate) != manifest.get("input_fingerprint"):
        raise QualityGateError("attempt_stale", "quality attempt input fingerprint is no longer current", gate=gate, attempt_id=attempt_id, packet_id=packet_id)
    result_path = _result_path(root, manifest, packet_id)
    if not result_path.is_file():
        raise QualityGateError("result_missing", "assigned result file is missing", gate=str(manifest.get("gate")), attempt_id=attempt_id, packet_id=packet_id)
    try:
        result_metrics = metrics_for_path(result_path, result=True)
    except UnicodeDecodeError:
        return {"status": "rejected", "gate": manifest.get("gate"), "attempt_id": attempt_id, "packet_id": packet_id, "validation_report": {"ok": False, "issues": [{"code": "result_invalid_utf8", "message": "result must be valid UTF-8"}], "result_path": _relative(root, result_path)}}
    if not result_metrics.safe_to_dispatch:
        return {"status": "rejected", "gate": manifest.get("gate"), "attempt_id": attempt_id, "packet_id": packet_id, "validation_report": {"ok": False, "issues": [{"code": "result_budget_exceeded", "message": "result exceeds the fixed UTF-8 result budget"}], "result_path": _relative(root, result_path), "budget": result_metrics.as_dict()}}
    try:
        result = read_json(result_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "rejected", "gate": manifest.get("gate"), "attempt_id": attempt_id, "packet_id": packet_id, "validation_report": {"ok": False, "issues": [{"code": "result_invalid_json", "message": "result is not valid JSON"}], "result_path": _relative(root, result_path)}}
    issues = _protocol_issues(root, manifest, packet_id, result)
    if issues:
        return {"status": "rejected", "gate": manifest.get("gate"), "attempt_id": attempt_id, "packet_id": packet_id, "validation_report": {"ok": False, "issues": issues, "result_path": _relative(root, result_path)}}
    packet_path = _packet_path(root, manifest, packet_id)
    receipt = {
        "format": "steam-visualogue-quality-receipt",
        "gate": manifest.get("gate"),
        "attempt_id": attempt_id,
        "packet_id": packet_id,
        "input_fingerprint": manifest.get("input_fingerprint"),
        "packet_sha256": sha256_path(packet_path),
        "result_schema": "quality-result.schema.json",
        "result_utf8_bytes": result_metrics.utf8_bytes,
        "result_sha256": sha256_path(result_path),
        "accepted": True,
        "submitted_at": utc_now_iso(),
    }
    receipt_path = root / "attempts" / attempt_id / "receipts" / f"{packet_id}.json"
    write_json(receipt_path, receipt)
    return {"status": "accepted", "gate": manifest.get("gate"), "attempt_id": attempt_id, "packet_id": packet_id, "receipt_path": _relative(root, receipt_path)}


def _all_receipts(root: Path, manifest: Mapping[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    receipts: list[dict[str, Any]] = []
    attempt_root = (root / "attempts" / str(manifest.get("attempt_id"))).resolve()
    for packet in _list(manifest.get("packets")):
        if not isinstance(packet, Mapping):
            continue
        path = (root.parent.parent / str(packet.get("receipt_path") or "")).resolve()
        result_path = (root.parent.parent / str(packet.get("result_path") or "")).resolve()
        packet_path = (root.parent.parent / str(packet.get("packet_path") or "")).resolve()
        if not path.is_relative_to(attempt_root) or not result_path.is_relative_to(attempt_root) or not packet_path.is_relative_to(attempt_root):
            return False, receipts
        if not path.is_file() or not packet_path.is_file():
            return False, receipts
        try:
            receipt = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return False, receipts
        if not isinstance(receipt, Mapping) or receipt.get("accepted") is not True:
            return False, receipts
        packet_id = str(packet.get("packet_id") or "")
        try:
            result_metrics = metrics_for_path(result_path, result=True) if result_path.is_file() else None
        except UnicodeDecodeError:
            return False, receipts
        result_utf8_bytes = result_metrics.utf8_bytes if result_metrics is not None else None
        if (
            receipt.get("format") != "steam-visualogue-quality-receipt"
            or receipt.get("gate") != manifest.get("gate")
            or receipt.get("attempt_id") != manifest.get("attempt_id")
            or receipt.get("packet_id") != packet_id
            or receipt.get("input_fingerprint") != manifest.get("input_fingerprint")
            or receipt.get("packet_sha256") != sha256_path(packet_path)
            or receipt.get("result_schema") != "quality-result.schema.json"
            or not result_path.is_file()
            or result_metrics is None
            or not result_metrics.safe_to_dispatch
            or receipt.get("result_utf8_bytes") != result_utf8_bytes
            or receipt.get("result_sha256") != sha256_path(result_path)
        ):
            return False, receipts
        receipts.append(dict(receipt))
    return bool(receipts) and len(receipts) == len(manifest.get("packets", [])), receipts


def _coverage_is_exact(root: Path, manifest: Mapping[str, Any], results: Iterable[Mapping[str, Any]]) -> bool:
    expected = {
        int(page.get("page"))
        for page in _list(_load_compiled(root.parent.parent).get("pages"))
        if isinstance(page, Mapping) and isinstance(page.get("page"), int)
    }
    assigned: list[int] = []
    result_pages: list[int] = []
    result_by_packet = {
        str(result.get("packet_id")): result
        for result in results
        if isinstance(result, Mapping)
    }
    packets = [packet for packet in _list(manifest.get("packets")) if isinstance(packet, Mapping)]
    for packet in packets:
        packet_ids = [value for value in _list(packet.get("required_page_ids")) if isinstance(value, int)]
        result = result_by_packet.get(str(packet.get("packet_id") or ""))
        if len(packet_ids) != len(set(packet_ids)) or any(value not in expected for value in packet_ids):
            return False
        assigned.extend(packet_ids)
        if not isinstance(result, Mapping):
            return False
        ids = [
            int(page.get("page"))
            for page in _list(result.get("pages"))
            if isinstance(page, Mapping) and isinstance(page.get("page"), int)
        ]
        if ids != packet_ids or len(ids) != len(set(ids)):
            return False
        result_pages.extend(ids)
    return (
        assigned == sorted(expected)
        and result_pages == sorted(expected)
        and len(assigned) == len(expected)
        and len(packets) == len(set(str(packet.get("packet_id")) for packet in packets))
    )


def _finding_key(finding: Mapping[str, Any]) -> tuple[Any, ...]:
    locations = tuple(sorted((int(item.get("page")), str(item.get("field"))) for item in finding.get("locations", []) if isinstance(item, Mapping) and isinstance(item.get("page"), int)))
    return str(finding.get("category")), locations, str(finding.get("explanation")), str(finding.get("recommendation"))


def _merge_findings(results: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for result in results:
        for raw in _list(result.get("findings")):
            if isinstance(raw, Mapping):
                finding = dict(raw)
                if _category_key(finding.get("category")) in _MUST_FIX_CATEGORY_KEYS:
                    finding["severity"] = "must-fix"
                unique[_finding_key(finding)] = finding
    findings = list(unique.values())
    by_category: dict[str, set[int]] = {}
    for finding in findings:
        by_category.setdefault(_category_key(finding.get("category")), set()).update(int(item.get("page")) for item in finding.get("locations", []) if isinstance(item, Mapping) and isinstance(item.get("page"), int))
    for category, pages in by_category.items():
        if len(pages) >= 3:
            for finding in findings:
                if _category_key(finding.get("category")) == category:
                    finding["severity"] = "must-fix"
    polish = [finding for finding in findings if finding.get("severity") == "polish"]
    polish_pages = [int(location.get("page")) for finding in polish for location in finding.get("locations", []) if isinstance(location, Mapping) and isinstance(location.get("page"), int)]
    polish_categories = [_category_key(finding.get("category")) for finding in polish]
    if len(polish) > 2 or len(set(polish_pages)) < len(polish_pages) or len(set(polish_categories)) < len(polish_categories):
        for finding in findings:
            if finding.get("severity") == "polish":
                finding["severity"] = "must-fix"
    return sorted(findings, key=lambda item: (str(item.get("severity")), str(item.get("category")), _finding_key(item)[1]))


def finish_quality_gate(run_dir: str | Path, attempt_id: str) -> dict[str, Any]:
    root = quality_root(run_dir)
    registry, manifest = _manifest_for_attempt(root, attempt_id)
    if manifest.get("status") != "active":
        raise QualityGateError("attempt_not_active", "only an active quality attempt can be finished", gate=str(manifest.get("gate")), attempt_id=attempt_id)
    gate = _gate(str(manifest.get("gate") or ""))
    if _input_fingerprint(root.parent.parent, gate) != manifest.get("input_fingerprint"):
        raise QualityGateError("attempt_stale", "quality attempt input fingerprint is no longer current", gate=gate, attempt_id=attempt_id)
    complete, _ = _all_receipts(root, manifest)
    if not complete:
        raise QualityGateError("coverage_incomplete", "every assigned quality result must be accepted before finishing", gate=str(manifest.get("gate")), attempt_id=attempt_id)
    results: list[dict[str, Any]] = []
    for packet in _list(manifest.get("packets")):
        if isinstance(packet, Mapping):
            result_path = root.parent.parent / str(packet.get("result_path") or "")
            result = read_json(result_path)
            if isinstance(result, Mapping):
                results.append(dict(result))
    if not _coverage_is_exact(root, manifest, results):
        raise QualityGateError("coverage_incomplete", "quality packets and results must cover every page exactly once", gate=str(manifest.get("gate")), attempt_id=attempt_id)
    findings = _merge_findings(results)
    must_fix = sum(1 for finding in findings if finding.get("severity") == "must-fix")
    polish = sum(1 for finding in findings if finding.get("severity") == "polish")
    failed_verdict = False
    for result in results:
        deck_verdict = result.get("deck_verdict")
        if isinstance(deck_verdict, Mapping) and any(isinstance(value, Mapping) and value.get("status") == "fail" for value in deck_verdict.values()):
            failed_verdict = True
        for page in _list(result.get("pages")):
            if isinstance(page, Mapping) and any(isinstance(page.get(field), Mapping) and page[field].get("status") == "fail" for field in _VERDICT_FIELDS):
                failed_verdict = True
    if failed_verdict and not must_fix:
        findings.append({"category": "verdict-failed", "severity": "must-fix", "locations": [{"page": 1, "field": "verdicts"}], "explanation": "A required reviewer verdict failed without a locating finding.", "recommendation": "Add the concrete page finding and repair the failed condition."})
        must_fix = 1
    cycle = int(manifest.get("cycle", 1))
    if must_fix:
        status = "revision-required" if cycle == 1 else "stopped"
    else:
        status = "passed" if polish <= 2 else ("revision-required" if cycle == 1 else "stopped")
    merged = {
        "format": "steam-visualogue-quality-merge",
        "gate": manifest.get("gate"),
        "attempt_id": attempt_id,
        "cycle": cycle,
        "input_fingerprint": manifest.get("input_fingerprint"),
        "status": status,
        "findings": sorted(findings, key=lambda item: (str(item.get("severity")), str(item.get("category")))),
        "severity": {"must-fix": must_fix, "polish": polish},
        "coverage": sorted({int(page.get("page")) for result in results for page in result.get("pages", []) if isinstance(page, Mapping) and isinstance(page.get("page"), int)}),
        "deck_verdict": results[0].get("deck_verdict", {}) if results else {},
        "recommended_changes": [str(change) for result in results for change in result.get("recommended_changes", []) if isinstance(change, str)],
        "finished_at": utc_now_iso(),
    }
    try:
        _validate_quality_privacy(merged)
        assert_merge_budget(merged)
    except BudgetViolation as exc:
        raise QualityGateError(
            "quality_merge_budget_exceeded",
            "merged quality findings exceed the fixed merge budget",
            gate=gate,
            attempt_id=attempt_id,
        ) from exc
    except ValueError as exc:
        raise QualityGateError(
            "quality_merge_privacy_invalid",
            "merged quality artifact contains prohibited private data",
            gate=gate,
            attempt_id=attempt_id,
        ) from exc
    merged_path = root / "attempts" / attempt_id / "merged.json"
    write_json(merged_path, merged)
    manifest = {**manifest, "status": status, "merged_path": _relative(root, merged_path), "finished_at": merged["finished_at"]}
    write_json(root / "attempts" / attempt_id / "manifest.json", manifest)
    attempts = [dict(item) for item in registry.get("attempts", []) if isinstance(item, Mapping)]
    for item in attempts:
        if item.get("attempt_id") == attempt_id:
            item.update({"status": status, "merged_path": _relative(root, merged_path), "finished_at": merged["finished_at"]})
    current_map = dict(registry.get("current", {}))
    current_map[str(manifest.get("gate"))] = attempt_id
    _write_registry(root, {**registry, "attempts": attempts, "current": current_map})
    return {"status": status, "gate": manifest.get("gate"), "attempt_id": attempt_id, "cycle": cycle, "merged_path": _relative(root, merged_path), "severity": merged["severity"], "coverage": merged["coverage"]}


def _status_entry(root: Path, entry: Mapping[str, Any] | None) -> dict[str, Any]:
    if not entry:
        return {"status": "not-started", "current": False, "attempt_id": None, "input_fingerprint": None}
    gate = str(entry.get("gate"))
    current = False
    try:
        current = str(entry.get("input_fingerprint")) == _input_fingerprint(root.parent.parent, gate)
    except (OSError, ValueError, KeyError, QualityGateError):
        current = False
    status = str(entry.get("status") or "active") if current else "stale"
    result = dict(entry)
    result.update({"status": status, "current": current})
    merged_path = root.parent.parent / str(entry.get("merged_path") or "")
    if merged_path.is_file():
        merged = read_json(merged_path)
        if isinstance(merged, Mapping):
            result["severity"] = dict(merged.get("severity", {}))
            result["coverage"] = merged.get("coverage", [])
    return result


def get_quality_status(run_dir: str | Path) -> dict[str, Any]:
    root = quality_root(run_dir)
    registry = _read_registry(root)
    stages = {gate: _status_entry(root, _current_attempt(registry, gate)) for gate in GATES}
    return {"format": "steam-visualogue-quality-status", "gates": stages, "attempts": registry.get("attempts", [])}


def quality_state_is_current(run_dir: str | Path) -> bool:
    path = Path(run_dir) / "quality-state.json"
    if not path.is_file():
        return False
    try:
        state = read_json(path)
        validate_schema_document("quality-state.json", "quality-state.schema.json", state)
        if state.get("state") != "passed":
            return False
        quality_root_path = quality_root(run_dir)
        registry = _read_registry(quality_root_path)
        status = {
            gate: _status_entry(quality_root_path, _current_attempt(registry, gate))
            for gate in GATES
        }
        return all(
            status[gate].get("status") == "passed"
            and status[gate].get("current") is True
            and state.get("fingerprints", {}).get(gate) == status[gate].get("input_fingerprint")
            for gate in GATES
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False


def finalize_quality(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    registry = _read_registry(quality_root(root))
    status = {gate: _status_entry(quality_root(root), _current_attempt(registry, gate)) for gate in GATES}
    if any(item.get("status") != "passed" or item.get("current") is not True for item in status.values()):
        raise QualityGateError("gates_not_passed", "reader, visual, and factual gates must all be current and passed")
    validation_path = root / "validation.json"
    evidence_path = root / "evidence.json"
    if not validation_path.is_file():
        raise QualityGateError("validation_missing", "current deterministic validation is required before finalization")
    if not evidence_path.is_file():
        raise QualityGateError("evidence_missing", "current evidence is required before finalization")
    validation = read_json(validation_path)
    evidence = read_json(evidence_path)
    try:
        validate_schema_document("evidence.json", "evidence.schema.json", evidence)
    except ValueError as exc:
        raise QualityGateError("evidence_invalid", str(exc)) from exc
    if not isinstance(validation, Mapping) or validation.get("format") != "steam-visualogue-validation" or validation.get("ok") is not True:
        raise QualityGateError("validation_failed", "deterministic validation must pass before finalization")
    layout_path = root / "publish-layout.json"
    compiled_path = root / "compiled-deck.json"
    render_manifest_path = root / "output" / ".render-manifest.json"
    output_manifest_path = root / "output" / "manifest.json"
    if not compiled_path.is_file() or not layout_path.is_file() or not render_manifest_path.is_file() or not output_manifest_path.is_file():
        raise QualityGateError("render_current_missing", "compiled deck, publish layout, output manifest, and current render are required")
    layout = read_json(layout_path)
    compiled = read_json(compiled_path)
    render_manifest = read_json(render_manifest_path)
    output_manifest = read_json(output_manifest_path)
    if not isinstance(layout, Mapping) or layout.get("format") != "steam-visualogue-publish-layout":
        raise QualityGateError("publish_layout_invalid", "current publish layout is invalid")
    try:
        validate_schema_document("compiled-deck.json", "compiled-deck.schema.json", compiled)
        validate_schema_document("publish-layout.json", "publish-layout.schema.json", layout)
        validate_schema_document("output manifest", "output-manifest.schema.json", output_manifest)
    except ValueError as exc:
        raise QualityGateError("render_current_invalid", str(exc)) from exc
    if any(layout.get(field) != compiled.get(field) for field in ("locale", "catalog_version", "label_fingerprint", "deck_schema_fingerprint", "compiled_deck_fingerprint")):
        raise QualityGateError("render_current_stale", "publish layout does not match the current compiled deck")
    for document, label in ((render_manifest, "render manifest"), (output_manifest, "output manifest")):
        if not isinstance(document, Mapping):
            raise QualityGateError("render_current_invalid", f"{label} is invalid")
        expected_format = "steam-visualogue-render-manifest" if label == "render manifest" else "steam-visualogue-output-manifest"
        if document.get("format") != expected_format:
            raise QualityGateError("render_current_invalid", f"{label} has an unsupported format")
        for field in ("locale", "catalog_version", "label_fingerprint", "deck_schema_fingerprint", "compiled_deck_fingerprint", "visual_brief_fingerprint", "layout_input_fingerprint"):
            if document.get(field) != layout.get(field):
                raise QualityGateError("render_current_stale", f"{label} does not match current {field}")
    rendered_pages = render_manifest.get("pages") if isinstance(render_manifest, Mapping) else None
    if not isinstance(rendered_pages, list) or len(rendered_pages) != len(layout.get("pages", [])):
        raise QualityGateError("render_current_invalid", "render manifest does not cover every current page")
    for index, rendered in enumerate(rendered_pages, 1):
        filename = str(rendered.get("file") or "") if isinstance(rendered, Mapping) else ""
        candidate = Path(filename)
        if not filename or candidate.name != filename or candidate.is_absolute() or not (root / "output" / candidate).is_file():
            raise QualityGateError("render_current_invalid", "render manifest contains a missing or unsafe page file")
        if filename != f"{index:02d}.png":
            raise QualityGateError("render_current_invalid", "render manifest page files must match the current page order")
    if output_manifest.get("validation_ok") is not True:
        raise QualityGateError("render_current_invalid", "output manifest is not marked validation-successful")
    output_pages = output_manifest.get("pages")
    if not isinstance(output_pages, list) or len(output_pages) != len(layout.get("pages", [])) or output_manifest.get("page_count") != len(output_pages):
        raise QualityGateError("render_current_invalid", "output manifest does not cover every current page")
    for index, row in enumerate(output_pages, 1):
        filename = str(row.get("file") or "") if isinstance(row, Mapping) else ""
        candidate = Path(filename)
        if not isinstance(row, Mapping) or not filename or candidate.name != filename or candidate.is_absolute() or not (root / "output" / candidate).is_file():
            raise QualityGateError("render_current_invalid", "output manifest contains a missing or unsafe page file")
        if filename != f"{index:02d}.png" or row.get("sha256") != sha256_path_hex(root / "output" / candidate):
            raise QualityGateError("render_current_invalid", "output manifest page inventory or hash is stale")
    contact = output_manifest.get("contact_sheet")
    contact_name = str(contact.get("file") or "") if isinstance(contact, Mapping) else ""
    contact_path = root / "output" / contact_name
    if not isinstance(contact, Mapping) or not contact_name or Path(contact_name).name != contact_name or Path(contact_name).is_absolute() or not contact_path.is_file() or contact.get("sha256") != sha256_path_hex(contact_path):
        raise QualityGateError("render_current_invalid", "output manifest contact sheet is missing or stale")
    for field in ("catalog_version", "label_fingerprint", "deck_schema_fingerprint", "compiled_deck_fingerprint", "visual_brief_fingerprint", "layout_input_fingerprint"):
        if validation.get(field) != layout.get(field):
            raise QualityGateError("validation_stale", "deterministic validation is not for the current layout")
    fingerprints = {gate: str(status[gate].get("input_fingerprint")) for gate in GATES}
    state = {
        "format": "steam-visualogue-quality-state",
        "state": "passed",
        "fingerprints": fingerprints,
        "gates": {
            gate: {
                "status": "passed",
                "attempt_id": status[gate].get("attempt_id"),
                "input_fingerprint": status[gate].get("input_fingerprint"),
                "must_fix": int((status[gate].get("severity") or {}).get("must-fix", 0)),
                "polish": int((status[gate].get("severity") or {}).get("polish", 0)),
            }
            for gate in GATES
        },
        "finalized_at": utc_now_iso(),
    }
    validate_schema_document("quality-state.json", "quality-state.schema.json", state)
    destination = write_json(root / "quality-state.json", state)
    return {"status": "passed", "artifact": str(destination), "gates": state["gates"], "fingerprints": fingerprints}


__all__ = [
    "GATES",
    "QualityGateError",
    "finish_quality_gate",
    "finalize_quality",
    "get_quality_status",
    "quality_root",
    "quality_state_is_current",
    "start_quality_gate",
    "submit_quality_result",
]
