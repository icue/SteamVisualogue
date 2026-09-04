"""Adaptive, privacy-safe layout for a compiled editorial deck."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .fingerprint import (
    compute_asset_manifest_fingerprint,
    compute_layout_input_fingerprint,
    compute_visual_brief_fingerprint,
)
from .locales import format_visible_measure, normalize_report_locale


PUBLISH_LAYOUT_FORMAT = "steam-visualogue-publish-layout"
DEFAULT_WORKING_SIZE = (1080, 1440)
DEFAULT_FINAL_SIZE = (1080, 1440)
ATLAS_IMAGE_SIZE = (112, 168)
ATLAS_CARD_HEIGHT = 250
ATLAS_ROW_STEP = 270


class PublishLayoutError(ValueError):
    """A locatable layout contract error."""

    def __init__(self, code: str, message: str, *, page: int | None = None, field: str = "") -> None:
        self.code = str(code)
        self.page = page
        self.field = field
        self.message = str(message)
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "page": self.page, "field": self.field, "message": self.message}


def _font_candidates(locale: str, bold: bool = False) -> list[Path]:
    names = (
        ("msyhbd.ttc", "Microsoft YaHei UI Bold", "NotoSansCJK-Bold.ttc")
        if bold and locale == "zh-CN"
        else ("msyh.ttc", "Microsoft YaHei UI", "NotoSansCJK-Regular.ttc")
        if locale == "zh-CN"
        else ("arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf")
        if bold
        else ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf")
    )
    roots = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/opentype/noto")]
    return list(dict.fromkeys(root / name for root in roots for name in names))


def _font_family(path: Path | None, bold: bool) -> str:
    if path is None:
        return "Pillow default"
    name = path.name.casefold()
    if "msyh" in name:
        return "Microsoft YaHei UI Bold" if bold else "Microsoft YaHei UI"
    if "segoe" in name:
        return "Segoe UI Semibold" if bold else "Segoe UI"
    if "arial" in name:
        return "Arial Bold" if bold else "Arial"
    if "dejavu" in name:
        return "DejaVu Sans Bold" if bold else "DejaVu Sans"
    if "noto" in name:
        return "Noto Sans CJK Bold" if bold else "Noto Sans CJK"
    return path.stem


def _select_font(size: int, report_locale: str, bold: bool = False) -> tuple[Any, str]:
    from PIL import ImageFont

    for candidate in _font_candidates(report_locale, bold):
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), max(1, int(size))), _font_family(candidate, bold)
            except OSError:
                continue
    if report_locale == "zh-CN":
        raise PublishLayoutError("cjk_font_missing", "zh-CN requires a compatible CJK font")
    return ImageFont.load_default(), _font_family(None, bold)


def _load_font(size: int, report_locale: str = "en-US") -> Any:
    return _select_font(size, report_locale, False)[0]


def _load_font_bold(size: int, report_locale: str = "en-US") -> Any:
    return _select_font(size, report_locale, True)[0]


def font_family(report_locale: str = "en-US", bold: bool = False) -> str:
    """Return the actual family selected for the requested locale and weight."""

    return _select_font(12, report_locale, bold)[1]


def _text_width(font: Any, text: str) -> float:
    try:
        return float(font.getlength(text))
    except AttributeError:
        box = font.getbbox(text)
        return float(box[2] - box[0])


def _wrap(text: str, font: Any, width: int, locale: str) -> list[str]:
    result: list[str] = []
    for paragraph in str(text).replace("\r\n", "\n").split("\n"):
        if not paragraph:
            result.append("")
            continue
        if locale == "zh-CN":
            units = list(paragraph)
        else:
            units = re.findall(r"\S+\s*", paragraph)
        current = ""
        for unit in units:
            candidate = current + unit
            if not current or _text_width(font, candidate.rstrip()) <= max(1, width):
                current = candidate
            else:
                result.append(current.rstrip())
                current = unit.lstrip()
        if current.strip():
            result.append(current.rstrip())
    return result or [""]


def _fit_text(text: str, width: int, height: int, start: int, minimum: int, locale: str, bold: bool = False) -> dict[str, Any]:
    loader = _load_font_bold if bold else _load_font
    size = max(minimum, start)
    while size >= minimum:
        font = loader(size, locale)
        lines = _wrap(text, font, width, locale)
        try:
            line_height = max(size, font.getbbox("Ag国")[3] - font.getbbox("Ag国")[1]) + 4
        except Exception:
            line_height = size + 4
        if len(lines) * line_height <= height:
            return {"font_size": size, "line_height": line_height, "lines": lines, "truncated": False}
        size -= 2
    font = loader(minimum, locale)
    lines = _wrap(text, font, width, locale)
    line_height = max(1, minimum + 4)
    maximum = max(1, height // line_height)
    truncated = len(lines) > maximum
    lines = lines[:maximum]
    if truncated and lines:
        last = lines[-1]
        while last and _text_width(font, last + "…") > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return {"font_size": minimum, "line_height": line_height, "lines": lines, "truncated": truncated}


def _colour(value: Any, fallback: str) -> str:
    value = str(value or fallback)
    return value if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else fallback


def _palette(direction: Mapping[str, Any] | None) -> dict[str, str]:
    raw = direction.get("palette") if isinstance(direction, Mapping) and isinstance(direction.get("palette"), Mapping) else {}
    return {
        "ground": _colour(raw.get("ground"), "#111820"),
        "ink": _colour(raw.get("ink"), "#F2EEE6"),
        "primary": _colour(raw.get("primary"), "#D36A4A"),
        "secondary": _colour(raw.get("secondary"), "#4D7EA8"),
        "accent": _colour(raw.get("accent"), "#E7C35A"),
        "muted": _colour(raw.get("muted"), "#77818A"),
    }


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    hex_clean = hex_str.lstrip("#")
    return int(hex_clean[0:2], 16), int(hex_clean[2:4], 16), int(hex_clean[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _darken_to_ground(hex_str: str) -> str:
    r, g, b = _hex_to_rgb(hex_str)
    max_c = max(r, g, b)
    if max_c == 0:
        return "#111418"
    scale = min(1.0, 24.0 / max_c)
    dr = max(8, min(36, round(r * scale)))
    dg = max(8, min(36, round(g * scale)))
    db = max(8, min(36, round(b * scale)))
    return _rgb_to_hex((dr, dg, db))


def _derive_page_palette(
    asset_ids: list[str],
    records: Mapping[str, Any],
    global_palette: Mapping[str, str],
) -> dict[str, str]:
    if len(asset_ids) != 1:
        return dict(global_palette)
    asset_id = asset_ids[0]
    record = records.get(asset_id)
    if not isinstance(record, Mapping) or not isinstance(record.get("palette"), Mapping):
        return dict(global_palette)
    dominant = record["palette"].get("dominant_colors", [])
    colors = []
    for item in dominant:
        if isinstance(item, Mapping) and isinstance(item.get("hex"), str):
            hex_c = _colour(item["hex"], "")
            if hex_c:
                colors.append({
                    "hex": hex_c,
                    "lum": float(item.get("luminance", 0.5)),
                    "chroma": float(item.get("chroma", 0.1)),
                    "weight": float(item.get("weight", 0.1)),
                })
    if not colors:
        return dict(global_palette)

    by_lum = sorted(colors, key=lambda c: c["lum"])
    darkest = by_lum[0]
    if darkest["lum"] <= 0.035:
        ground = darkest["hex"]
    else:
        ground = _darken_to_ground(by_lum[0]["hex"])

    by_chroma = sorted(colors, key=lambda c: c["chroma"], reverse=True)
    primary = by_chroma[0]["hex"] if by_chroma else global_palette["primary"]
    secondary = by_chroma[1]["hex"] if len(by_chroma) > 1 else (colors[1]["hex"] if len(colors) > 1 else global_palette["secondary"])

    by_highlight = sorted(colors, key=lambda c: c["lum"] * 0.7 + c["chroma"] * 0.3, reverse=True)
    accent = by_highlight[0]["hex"] if by_highlight else global_palette["accent"]

    by_muted = sorted(colors, key=lambda c: c["chroma"])
    muted = by_muted[0]["hex"] if by_muted else global_palette["muted"]

    ink = global_palette.get("ink", "#F7F4E9")
    return {
        "ground": _colour(ground, global_palette["ground"]),
        "ink": _colour(ink, global_palette["ink"]),
        "primary": _colour(primary, global_palette["primary"]),
        "secondary": _colour(secondary, global_palette["secondary"]),
        "accent": _colour(accent, global_palette["accent"]),
        "muted": _colour(muted, global_palette["muted"]),
    }



def _asset_records(assets: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(assets, Mapping):
        return {}
    rows = assets.get("assets")
    return rows if isinstance(rows, Mapping) else assets


def _asset_geometry(asset_id: str, records: Mapping[str, Any]) -> tuple[int, int] | None:
    record = records.get(asset_id)
    if not isinstance(record, Mapping):
        return None
    width, height = record.get("width"), record.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
        return round(float(width)), round(float(height))
    path = record.get("path")
    if isinstance(path, str) and Path(path).is_file():
        try:
            from PIL import Image

            with Image.open(path) as image:
                return image.size
        except OSError:
            return None
    return None


def _require_atlas_portrait_asset(
    subject: Mapping[str, Any],
    records: Mapping[str, Any],
    *,
    page: int,
    index: int,
) -> str:
    """Return an atlas asset ID only when it is a Steam portrait game asset."""

    asset_id = str(subject.get("asset_id") or "")
    parts = asset_id.split(":")
    field = f"presentation.content.items[{index}].subject.asset_id"
    if len(parts) != 3 or parts[0] != "game" or not parts[1].isdigit() or parts[2] != "portrait":
        raise PublishLayoutError(
            "atlas_portrait_asset_required",
            "series-atlas and pattern-atlas subjects must use game:<appid>:portrait assets",
            page=page,
            field=field,
        )
    geometry = _asset_geometry(asset_id, records)
    if geometry is not None and geometry[0] >= geometry[1]:
        raise PublishLayoutError(
            "atlas_portrait_asset_required",
            f"atlas portrait asset must be taller than wide; recorded geometry is {geometry[0]}x{geometry[1]}",
            page=page,
            field=field,
        )
    return asset_id


def _require_comparison_landscape_asset(
    subject: Mapping[str, Any],
    records: Mapping[str, Any],
    *,
    page: int,
    index: int,
) -> str:
    """Return a comparison asset ID only when its source is landscape."""

    asset_id = str(subject.get("asset_id") or "")
    parts = asset_id.split(":")
    field = f"presentation.content.items[{index}].subject.asset_id"
    if len(parts) == 3 and parts[0] == "game" and parts[2] != "header":
        raise PublishLayoutError(
            "comparison_landscape_asset_required",
            "qualitative-comparison game subjects must use game:<appid>:header assets",
            page=page,
            field=field,
        )
    geometry = _asset_geometry(asset_id, records)
    if geometry is None:
        raise PublishLayoutError(
            "comparison_landscape_asset_required",
            "qualitative-comparison assets must have known landscape geometry",
            page=page,
            field=field,
        )
    if geometry[0] <= geometry[1]:
        raise PublishLayoutError(
            "comparison_landscape_asset_required",
            f"qualitative-comparison asset must be wider than tall; recorded geometry is {geometry[0]}x{geometry[1]}",
            page=page,
            field=field,
        )
    return asset_id


def _require_comparison_portrait_asset(
    subject: Mapping[str, Any],
    records: Mapping[str, Any],
    *,
    page: int,
    index: int,
) -> str:
    """Return a quantitative comparison asset ID only when it is a vertical Steam portrait game asset."""

    asset_id = str(subject.get("asset_id") or "")
    parts = asset_id.split(":")
    field = f"presentation.content.items[{index}].subject.asset_id"
    if len(parts) != 3 or parts[0] != "game" or not parts[1].isdigit() or parts[2] != "portrait":
        raise PublishLayoutError(
            "comparison_portrait_asset_required",
            "quantitative-comparison game subjects must use game:<appid>:portrait assets",
            page=page,
            field=field,
        )
    geometry = _asset_geometry(asset_id, records)
    if geometry is not None and geometry[0] >= geometry[1]:
        raise PublishLayoutError(
            "comparison_portrait_asset_required",
            f"quantitative comparison portrait asset must be taller than wide; recorded geometry is {geometry[0]}x{geometry[1]}",
            page=page,
            field=field,
        )
    return asset_id


def _require_anomaly_portrait_asset(
    subject: Mapping[str, Any],
    records: Mapping[str, Any],
    *,
    page: int,
) -> str:
    """Return an anomaly asset ID only when it is a vertical Steam portrait game asset."""

    asset_id = str(subject.get("asset_id") or "")
    parts = asset_id.split(":")
    field = "presentation.content.item.subject.asset_id"
    if len(parts) != 3 or parts[0] != "game" or not parts[1].isdigit() or parts[2] != "portrait":
        raise PublishLayoutError(
            "anomaly_portrait_asset_required",
            "achievement-anomaly game subjects must use game:<appid>:portrait assets",
            page=page,
            field=field,
        )
    geometry = _asset_geometry(asset_id, records)
    if geometry is not None and geometry[0] >= geometry[1]:
        raise PublishLayoutError(
            "anomaly_portrait_asset_required",
            f"achievement anomaly portrait asset must be taller than wide; recorded geometry is {geometry[0]}x{geometry[1]}",
            page=page,
            field=field,
        )
    return asset_id


def _asset_safe(asset_id: str, target: tuple[int, int], records: Mapping[str, Any]) -> tuple[bool, float]:
    geometry = _asset_geometry(asset_id, records)
    if geometry is None:
        return True, 1.0
    source_width, source_height = geometry
    target_width, target_height = target
    scale = max(target_width / source_width, target_height / source_height)
    return scale <= 1.5, round(scale, 6)


def _ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        return [str(item) for item in value if str(item)]
    return []


def _image(element_id: str, asset_id: str, box: tuple[int, int, int, int], *, evidence: Iterable[str], treatment: str, records: Mapping[str, Any], crop: Mapping[str, Any] | None = None, angle: float = 0.0, corner_radius: int = 0, perspective: str | None = None) -> dict[str, Any]:
    x, y, width, height = box
    evidence_ids = list(evidence)
    safe, scale = _asset_safe(asset_id, (width, height), records)
    actual_treatment = treatment
    actual_box = (x, y, width, height)
    if not safe:
        geometry = _asset_geometry(asset_id, records)
        if geometry:
            # Flooring keeps the measured linear scale at or below the
            # contract's 1.5x ceiling even for odd source dimensions.
            max_width = max(1, math.floor(geometry[0] * 1.5))
            max_height = max(1, math.floor(geometry[1] * 1.5))
            reduced = min(1.0, max_width / max(1, width), max_height / max(1, height))
            reduced_width = max(1, min(width, round(width * reduced)))
            reduced_height = max(1, min(height, round(height * reduced)))
            actual_box = (x + (width - reduced_width) // 2, y + (height - reduced_height) // 2, reduced_width, reduced_height)
            actual_treatment = "contain"
            scale = round(max(reduced_width / geometry[0], reduced_height / geometry[1]), 6)
    actual_x, actual_y, actual_width, actual_height = actual_box
    result = {
        "id": element_id,
        "type": "image",
        "asset_id": asset_id,
        "x": actual_x,
        "y": actual_y,
        "w": actual_width,
        "h": actual_height,
        "width": actual_width,
        "height": actual_height,
        "treatment": actual_treatment,
        "crop": {"focus_x": float((crop or {}).get("focus_x", 0.5)), "focus_y": float((crop or {}).get("focus_y", 0.5))},
        "evidence_ids": evidence_ids,
        "requires_evidence": bool(evidence_ids),
        "scale_factor": scale,
        "asset_role": "source-artwork",
    }
    if angle:
        result["angle"] = round(float(angle), 2)
    if corner_radius:
        result["corner_radius"] = int(corner_radius)
    if perspective:
        result["perspective"] = str(perspective)
    return result


def _text(element_id: str, value: Any, box: tuple[int, int, int, int], *, semantic_role: str, locale: str, palette: Mapping[str, str], evidence: Iterable[str], size: int, minimum: int, bold: bool = False, align: str = "left") -> dict[str, Any] | None:
    content = str(value or "").strip()
    if not content:
        return None
    x, y, width, height = box
    fit = _fit_text(content, width, height, size, minimum, locale, bold)
    actual_height = min(height, fit["line_height"] * len(fit["lines"]))
    return {
        "id": element_id,
        "type": "text",
        "semantic_role": semantic_role,
        "text": content,
        "lines": fit["lines"],
        "x": x,
        "y": y,
        "w": width,
        "h": actual_height,
        "width": width,
        "height": actual_height,
        "font_size": fit["font_size"],
        "line_height": fit["line_height"],
        "color": palette["ink"],
        "background_color": palette["ground"],
        "align": align,
        "bold": bool(bold),
        "evidence_ids": list(evidence),
        "requires_evidence": True,
        "truncated": bool(fit["truncated"]),
    }


def _mark(element_id: str, box: tuple[int, int, int, int], color: str, *, shape: str = "rect", evidence: Iterable[str] = (), decorative: bool = False, opacity: float = 1.0) -> dict[str, Any]:
    x, y, width, height = box
    return {"id": element_id, "type": "mark", "shape": shape, "x": x, "y": y, "w": width, "h": height, "width": width, "height": height, "color": color, "opacity": opacity, "evidence_ids": list(evidence), "requires_evidence": not decorative, "decorative": decorative}


def _copy(page: Mapping[str, Any]) -> Mapping[str, Any]:
    value = page.get("reader_copy")
    return value if isinstance(value, Mapping) else {}


def _content(page: Mapping[str, Any]) -> Mapping[str, Any]:
    presentation = page.get("presentation")
    return presentation.get("content", {}) if isinstance(presentation, Mapping) and isinstance(presentation.get("content"), Mapping) else {}


def _subjects(content: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            subject = value.get("subject")
            if isinstance(subject, Mapping) and subject.get("game_id") and subject.get("asset_id"):
                found.append(subject)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(content)
    unique: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for subject in found:
        key = str(subject.get("game_id")) + "|" + str(subject.get("asset_id"))
        if key not in seen:
            seen.add(key)
            unique.append(subject)
    return unique


def _subject_label(subject: Mapping[str, Any]) -> str:
    """Return an optional compact public label before the canonical fallback."""
    for key in ("short_label", "display_name"):
        value = str(subject.get(key) or "").strip()
        if value:
            return value
    return str(subject.get("game_id") or "")


def _base_elements(page: Mapping[str, Any], width: int, height: int, palette: Mapping[str, str], locale: str) -> list[dict[str, Any]]:
    copy = _copy(page)
    evidence = _ids(page.get("evidence_ids"))
    elements: list[dict[str, Any]] = []
    headline = _text("headline", copy.get("headline"), (72, 80, width - 144, 260), semantic_role="headline", locale=locale, palette=palette, evidence=evidence, size=76, minimum=30, bold=True)
    if headline:
        elements.append(headline)
    support = _text("support", copy.get("support"), (72, 370, width - 144, 180), semantic_role="support", locale=locale, palette=palette, evidence=evidence, size=31, minimum=20)
    if support:
        elements.append(support)
    caption = _text("caption", copy.get("caption"), (72, height - 90, width - 144, 64), semantic_role="caption", locale=locale, palette=palette, evidence=evidence, size=20, minimum=15)
    if caption:
        elements.append(caption)
    return elements


def _composition_family(kind: str, *, safe_equal: bool = True) -> str:
    """Name the fixed geometry family actually used by a page."""

    if kind == "quantitative-comparison":
        return "comparison-bars"
    if kind == "qualitative-comparison":
        return "comparison-equal-cards" if safe_equal else "comparison-stacked-cards"
    if kind in {"series-atlas", "pattern-atlas"}:
        return "atlas-grid"
    if kind == "archive-density":
        return "archive-waffle"
    if kind == "evidence-ledger":
        return "evidence-ledger"
    if kind == "temporal-strata":
        return "temporal-rows"
    if kind == "achievement-anomaly":
        return "anomaly-split"
    return "hero-anchor"


def _build_page(page: Mapping[str, Any], width: int, height: int, palette: Mapping[str, str], locale: str, records: Mapping[str, Any]) -> dict[str, Any]:
    number = int(page["page"])
    kind = str(page.get("presentation", {}).get("kind") or "")
    content = _content(page)
    owner = str(page.get("visible_identity_owner") or "none")
    evidence = _ids(page.get("evidence_ids"))
    subjects = _subjects(content)
    page_asset_ids = _ids(page.get("asset_ids"))
    if not page_asset_ids and subjects:
        page_asset_ids = [str(s["asset_id"]) for s in subjects if s.get("asset_id")]
    ribbon_id = None
    if kind == "opening":
        ribbon_id = next((aid for aid, rec in records.items() if isinstance(rec, Mapping) and rec.get("ribbon_role") == "opening"), None)
    elif kind == "closing":
        ribbon_id = next((aid for aid, rec in records.items() if isinstance(rec, Mapping) and rec.get("ribbon_role") == "closing"), None)
    if not page_asset_ids and ribbon_id:
        page_asset_ids = [ribbon_id]
    page_palette = _derive_page_palette(page_asset_ids, records, palette)
    composition = _composition_family(kind)
    elements = _base_elements(page, width, height, page_palette, locale)
    labeled_games: set[str] = set()

    def add(item: dict[str, Any] | None) -> None:
        if item is not None:
            elements.append(item)

    def subject_image(subject: Mapping[str, Any], box: tuple[int, int, int, int], index: int, *, label_height: int = 34, show_label: bool = True, angle: float = 0.0, corner_radius: int = 0, perspective: str | None = None, label_size: int = 28, label_min: int = 18, label_align: str = "left") -> None:
        asset_id = str(subject.get("asset_id") or "")
        game_id = str(subject.get("game_id") or "")
        if not asset_id or not game_id:
            return
        x, y, image_width, image_height = box
        add(_image(f"image-{index}", asset_id, (x, y, image_width, image_height), evidence=[game_id], treatment="crop", records=records, angle=angle, corner_radius=corner_radius, perspective=perspective))
        if show_label and owner == "renderer" and game_id not in labeled_games:
            add(_text(f"identity-{index}", _subject_label(subject), (x, y + image_height + 10, image_width, label_height), semantic_role="game-label", locale=locale, palette=page_palette, evidence=[game_id], size=label_size, minimum=label_min, bold=True, align=label_align))
            labeled_games.add(game_id)

    if kind in {"opening", "hero", "abstract-portrait", "closing"}:
        ribbon_id = None
        if kind == "opening":
            ribbon_id = next((aid for aid, rec in records.items() if isinstance(rec, Mapping) and rec.get("ribbon_role") == "opening"), None)
        elif kind == "closing":
            ribbon_id = next((aid for aid, rec in records.items() if isinstance(rec, Mapping) and rec.get("ribbon_role") == "closing"), None)

        if subjects:
            subject = subjects[0]
            subject_image(subject, (width // 2, 640, width // 2 - 72, 600), 0, label_height=64)
        elif isinstance(content.get("raw_visual_asset"), str):
            image_box = (72, 580, width - 144, 720) if kind in {"opening", "closing"} else (width // 2, 640, width // 2 - 72, 600)
            add(_image("image-0", str(content["raw_visual_asset"]), image_box, evidence=[], treatment="contain", records=records))
        elif ribbon_id is not None and ribbon_id in records:
            add(_image("image-0", ribbon_id, (72, 580, width - 144, 720), evidence=[], treatment="contain", records=records))
        else:
            add(_mark("focus", (width // 2, 650, width // 2 - 72, 360), page_palette["primary"], shape="rect", evidence=evidence, opacity=0.18))
            add(_mark("focus-line", (width // 2, 1010, width // 2 - 72, 2), page_palette["accent"], shape="line", evidence=evidence))
    elif kind == "archive-density":
        bins = content.get("bins", []) if isinstance(content.get("bins"), list) else []
        start_x, start_y = 72, 620
        legend_height = len(bins) * 54 + 48
        available_height = max(200, height - 150 - start_y - legend_height - 20)
        cell = min(58, max(20, (available_height // 10) - 5))
        colors = [page_palette["primary"], page_palette["secondary"], page_palette["accent"], page_palette["muted"]]
        unit = 0
        display_unit = 0
        for bin_index, row in enumerate(bins):
            allocation = int(row.get("allocation_units", 0)) if isinstance(row, Mapping) else 0
            for _ in range(max(0, allocation)):
                # Keep the waffle readable while keeping the visual quality
                # packet below its fixed context budget: each displayed cell
                # represents roughly two allocation units.
                if unit % 2 == 0:
                    row_index, column = divmod(display_unit, 10)
                    add(_mark(f"waffle-{display_unit}", (start_x + column * (cell + 5), start_y + row_index * (cell + 5), cell, cell), colors[bin_index % len(colors)], evidence=_ids(row.get("evidence_ids", evidence)) if isinstance(row, Mapping) else evidence, shape="rect"))
                    display_unit += 1
                unit += 1
        grid_rows = max(1, (display_unit + 9) // 10)
        legend_y = start_y + grid_rows * (cell + 5) + 20
        for index, row in enumerate(bins):
            if not isinstance(row, Mapping):
                continue
            add(_mark(f"legend-mark-{index}", (72, legend_y + index * 54 + 12, 25, 25), colors[index % len(colors)], evidence=_ids(row.get("evidence_ids", evidence))))
            label = str(row.get("label") or "")
            if isinstance(row.get("measure"), Mapping):
                value = format_visible_measure(row["measure"], locale)
            else:
                value = str(row.get("value") if row.get("value") is not None else "")
            add(_text(f"legend-{index}", f"{label}  {value}".strip(), (112, legend_y + index * 54, width - 184, 48), semantic_role="archive-label", locale=locale, palette=page_palette, evidence=_ids(row.get("evidence_ids", evidence)), size=23, minimum=16))
    elif kind == "evidence-ledger":
        facts = content.get("facts", []) if isinstance(content.get("facts"), list) else []
        row_height = max(135, min(220, 720 // max(1, len(facts))))
        for index, row in enumerate(facts):
            if not isinstance(row, Mapping):
                continue
            y = 620 + index * row_height
            measure = row.get("measure", {}) if isinstance(row.get("measure"), Mapping) else {}
            add(_text(f"measure-{index}", format_visible_measure(measure, locale), (72, y, width // 3, 80), semantic_role="measure", locale=locale, palette=page_palette, evidence=_ids(row.get("evidence_ids", evidence)), size=42, minimum=24, bold=True))
            add(_text(f"label-{index}", row.get("label"), (width // 3 + 30, y, width - width // 3 - 100, 58), semantic_role="ledger-label", locale=locale, palette=page_palette, evidence=_ids(row.get("evidence_ids", evidence)), size=27, minimum=18, bold=True))
            add(_text(f"note-{index}", row.get("note"), (width // 3 + 30, y + 64, width - width // 3 - 100, 74), semantic_role="ledger-note", locale=locale, palette=page_palette, evidence=_ids(row.get("evidence_ids", evidence)), size=20, minimum=15))
            add(_mark(f"ledger-rule-{index}", (72, y + row_height - 12, width - 144, 2), page_palette["muted"], shape="line", evidence=_ids(row.get("evidence_ids", evidence)), decorative=False))
    elif kind == "quantitative-comparison":
        items = content.get("items", []) if isinstance(content.get("items"), list) else []
        max_value = max((float(item.get("measure", {}).get("canonical_value", 0)) for item in items if isinstance(item, Mapping) and isinstance(item.get("measure"), Mapping)), default=1.0)
        wall_w = 220
        wall_h = 440
        for index, item in enumerate(items[:2]):
            if not isinstance(item, Mapping) or not isinstance(item.get("subject"), Mapping):
                continue
            _require_comparison_portrait_asset(item["subject"], records, page=number, index=index)
            persp = "left_wall" if index == 0 else "right_wall"
            x = 80 if index == 0 else (width - 80 - wall_w)
            y = 600
            subject_image(item["subject"], (x, y, wall_w, wall_h), index, show_label=False, perspective=persp)

        corridor_x = 310
        corridor_w = width - corridor_x * 2  # 460px
        bar_max_w = 320
        add(_mark("vs-badge", (width // 2 - 40, 615, 80, 34), page_palette["accent"], shape="rect", decorative=True))
        add(_text("vs-label", "VS", (width // 2 - 40, 619, 80, 26), semantic_role="badge", locale=locale, palette={"ink": page_palette["ground"], "ground": page_palette["accent"]}, evidence=evidence, size=18, minimum=14, bold=True, align="center"))

        for index, item in enumerate(items[:2]):
            if not isinstance(item, Mapping):
                continue
            measure = item.get("measure", {}) if isinstance(item.get("measure"), Mapping) else {}
            ratio = float(measure.get("canonical_value", 0)) / max_value if max_value else 0
            val_y = 700 + index * 125
            item_ev = _ids(item.get("evidence_ids", evidence))
            subject = item.get("subject") if isinstance(item.get("subject"), Mapping) else {}
            game_id = str(subject.get("game_id") or "")
            add(_text(f"comparison-subject-{index}", _subject_label(subject), (corridor_x, val_y - 38, corridor_w, 30), semantic_role="game-label", locale=locale, palette=page_palette, evidence=[game_id] if game_id else item_ev, size=18, minimum=13, bold=True, align="center"))
            add(_text(f"value-{index}", format_visible_measure(measure, locale), (corridor_x, val_y, corridor_w, 42), semantic_role="measure", locale=locale, palette=page_palette, evidence=item_ev, size=32, minimum=18, bold=True, align="center"))
            bar_w = max(8, round(bar_max_w * ratio))
            add(_mark(f"bar-{index}", (corridor_x + round((corridor_w - bar_w) / 2), val_y + 48, bar_w, 20), page_palette["primary"] if index == 0 else page_palette["secondary"], evidence=item_ev))
    elif kind == "qualitative-comparison":
        items = content.get("items", []) if isinstance(content.get("items"), list) else []
        safe_equal = True
        card_width = (width - 180) // 2
        image_width = max(1, card_width - 48)
        image_height = 145
        target_ratio = image_width / image_height
        for index, item in enumerate(items[:2]):
            if isinstance(item, Mapping) and isinstance(item.get("subject"), Mapping):
                asset_id = _require_comparison_landscape_asset(item["subject"], records, page=number, index=index)
                geometry = _asset_geometry(asset_id, records)
                if geometry:
                    source_ratio = geometry[0] / geometry[1]
                    retained = min(1.0, source_ratio / target_ratio) if source_ratio < target_ratio else min(1.0, target_ratio / source_ratio)
                    if retained < 0.42 or not _asset_safe(asset_id, (image_width, image_height), records)[0]:
                        safe_equal = False
        if not safe_equal:
            # An unequal source pair gets a vertical rhythm, never a fake
            # equal-weight comparison.
            card_width = width - 144
        composition = _composition_family(kind, safe_equal=safe_equal)
        for index, item in enumerate(items[:2]):
            if not isinstance(item, Mapping) or not isinstance(item.get("subject"), Mapping):
                continue
            x = 72 if not safe_equal or index == 0 else width // 2 + 18
            y = 620 if safe_equal else 620 + index * 330
            card_height = 320 if safe_equal else 310
            add(_mark(f"card-{index}", (x, y, card_width, card_height), "#202A33", shape="rect", decorative=True, opacity=1.0))
            image_box = (x + 24, y + 24, card_width - 48, 145) if safe_equal else (x + 24, y + 24, min(card_width - 48, 420), 150)
            subject_image(item["subject"], image_box, index, label_height=32)
            statement_y = y + (230 if safe_equal else 205)
            add(_text(f"statement-{index}", item.get("statement"), (x + 24, statement_y, card_width - 48, 66), semantic_role="qualitative-statement", locale=locale, palette=page_palette, evidence=_ids(item.get("evidence_ids", evidence)), size=22, minimum=16))
        if safe_equal:
            add(_mark("comparison-divider", (width // 2 - 1, 620, 2, 320), page_palette["muted"], shape="line", decorative=True))
    elif kind == "series-atlas":
        items = content.get("items", []) if isinstance(content.get("items"), list) else []
        n = len(items)
        if n <= 3:
            img_w, img_h = 210, 295
            step_x, step_y = 52, 65
            start_x, start_y = 90, 625
            angles = [-7.0, 0.0, 7.0]
            start_y_r, row_step_r = 620, 160
            stat_size = 18
            val_size = 28
        else:
            img_w, img_h = 185, 260
            step_x, step_y = 42, 54
            start_x, start_y = 80, 620
            angles = [-9.0, -3.0, 3.0, 9.0]
            start_y_r, row_step_r = 615, 125
            stat_size = 16
            val_size = 24

        # 1. Left side fanned out cascading cover images with rotational angles
        for index, item in enumerate(items):
            if not isinstance(item, Mapping) or not isinstance(item.get("subject"), Mapping):
                continue
            _require_atlas_portrait_asset(item["subject"], records, page=number, index=index)
            card_x = start_x + index * step_x
            card_y = start_y + index * step_y
            ang = angles[index] if index < len(angles) else 0.0
            subject_image(item["subject"], (card_x, card_y, img_w, img_h), index, show_label=False, angle=ang, corner_radius=10)

        # 2. Right side milestone narrative timeline cards
        right_x = 490
        right_w = width - 72 - right_x
        for index, item in enumerate(items):
            if not isinstance(item, Mapping) or not isinstance(item.get("subject"), Mapping):
                continue
            subj = item["subject"]
            game_id = str(subj.get("game_id") or "")
            title = _subject_label(subj)
            labeled_games.add(game_id)
            item_ev = _ids(item.get("evidence_ids", evidence))
            measure = item.get("measure", {}) if isinstance(item.get("measure"), Mapping) else {}
            visible_measure = format_visible_measure(measure, locale) if measure else ""
            statement = str(item.get("statement") or "")

            stat_fit = _fit_text(statement, right_w - 52, 100, stat_size, 13, locale, False)
            stat_h = max(22, min(100, stat_fit["line_height"] * max(1, len(stat_fit["lines"]))))
            card_h_r = stat_h + 46
            row_y = start_y_r + index * row_step_r

            add(_mark(f"atlas-card-{index}", (right_x, row_y, right_w, card_h_r), "#202A33", shape="rect", decorative=True))
            add(_mark(f"atlas-dot-{index}", (right_x + 18, row_y + 14, 8, 8), page_palette["accent"], shape="rect", decorative=True))
            header_text = f"{title} · {visible_measure}" if visible_measure else title
            add(_text(f"atlas-header-{index}", header_text, (right_x + 36, row_y + 8, right_w - 52, 26), semantic_role="game-label", locale=locale, palette=page_palette, evidence=[game_id] if game_id else item_ev, size=18, minimum=14, bold=True))
            add(_text(f"atlas-statement-{index}", statement, (right_x + 36, row_y + 36, right_w - 52, stat_h), semantic_role="item-statement", locale=locale, palette=page_palette, evidence=item_ev, size=stat_size, minimum=13))
    elif kind == "pattern-atlas":
        items = content.get("items", []) if isinstance(content.get("items"), list) else []
        columns = 2
        card_width = (width - 180) // columns
        for index, item in enumerate(items):
            if not isinstance(item, Mapping) or not isinstance(item.get("subject"), Mapping):
                continue
            _require_atlas_portrait_asset(item["subject"], records, page=number, index=index)
            row, column = divmod(index, columns)
            x, y = 72 + column * (card_width + 36), 620 + row * ATLAS_ROW_STEP
            add(_mark(f"atlas-card-{index}", (x, y, card_width, ATLAS_CARD_HEIGHT), "#202A33", shape="rect", decorative=True))
            image_width, image_height = ATLAS_IMAGE_SIZE
            subject_image(item["subject"], (x + 18, y + 18, image_width, image_height), index, label_height=48)
            measure = item.get("measure", {}) if isinstance(item.get("measure"), Mapping) else {}
            text_x = x + 160
            text_width = card_width - 182
            visible_measure = format_visible_measure(measure, locale) if measure else ""
            add(_text(f"atlas-value-{index}", visible_measure, (text_x, y + 32, text_width, 60), semantic_role="measure", locale=locale, palette=page_palette, evidence=_ids(item.get("evidence_ids", evidence)), size=30, minimum=20, bold=True))
            add(_text(f"atlas-statement-{index}", item.get("statement"), (text_x, y + 100, text_width, 78), semantic_role="item-statement", locale=locale, palette=page_palette, evidence=_ids(item.get("evidence_ids", evidence)), size=18, minimum=14))
    elif kind == "temporal-strata":
        strata = content.get("strata", []) if isinstance(content.get("strata"), list) else []
        row_height = max(88, (height - 780) // max(1, len(strata)))
        image_height = max(50, min(90, row_height - 20))
        for index, item in enumerate(strata):
            if not isinstance(item, Mapping) or not isinstance(item.get("subject"), Mapping):
                continue
            y = 620 + index * row_height
            subject_image(item["subject"], (72, y, 250, image_height), index, label_height=28)
            add(_text(f"stratum-label-{index}", item.get("label"), (350, y + 10, 300, 52), semantic_role="stratum-label", locale=locale, palette=page_palette, evidence=_ids(item.get("evidence_ids", evidence)), size=25, minimum=18, bold=True))
            measure = item.get("measure", {}) if isinstance(item.get("measure"), Mapping) else {}
            add(_text(f"stratum-value-{index}", format_visible_measure(measure, locale), (width - 300, y + 6, 220, 60), semantic_role="measure", locale=locale, palette=page_palette, evidence=_ids(item.get("evidence_ids", evidence)), size=30, minimum=20, bold=True, align="right"))
            add(_mark(f"stratum-rule-{index}", (72, y + row_height - 16, width - 144, 2), page_palette["muted"], shape="line", decorative=True))
    elif kind == "achievement-anomaly":
        item = content.get("item", {}) if isinstance(content.get("item"), Mapping) else {}
        if isinstance(item.get("subject"), Mapping):
            _require_anomaly_portrait_asset(item["subject"], records, page=number)
            subject_image(item["subject"], (72, 600, 320, 480), 0, label_height=48)
        achievement = item.get("achievement", {}) if isinstance(item.get("achievement"), Mapping) else {}
        add(_text("achievement", achievement.get("display_name") or achievement.get("achievement_id"), (440, 640, width - 440 - 72, 90), semantic_role="achievement-label", locale=locale, palette=page_palette, evidence=_ids(item.get("evidence_ids", evidence)), size=29, minimum=19, bold=True))
        measure = item.get("statistic", {}) if isinstance(item.get("statistic"), Mapping) else {}
        add(_text("anomaly-stat", format_visible_measure(measure, locale), (440, 770, width - 440 - 72, 80), semantic_role="measure", locale=locale, palette=page_palette, evidence=_ids(item.get("evidence_ids", evidence)), size=40, minimum=24, bold=True))
        add(_text("anomaly-note", item.get("note"), (440, 880, width - 440 - 72, 140), semantic_role="item-note", locale=locale, palette=page_palette, evidence=_ids(item.get("evidence_ids", evidence)), size=22, minimum=16))

    visible_text = [item for item in elements if item.get("type") == "text"]
    visible_images = [item for item in elements if item.get("type") == "image"]
    card_metrics = []
    card_marks = [item for item in elements if str(item.get("id", "")).startswith(("card-", "atlas-card-"))]
    for card in card_marks:
        card_x, card_y = float(card.get("x", 0)), float(card.get("y", 0))
        card_w, card_h = float(card.get("w", 1)), float(card.get("h", 1))
        content_items = [
            item for item in elements
            if item is not card and not item.get("decorative")
            and card_x <= float(item.get("x", 0)) <= card_x + card_w
            and card_y <= float(item.get("y", 0)) <= card_y + card_h
        ]
        if content_items:
            left = min(float(item.get("x", card_x)) for item in content_items)
            top = min(float(item.get("y", card_y)) for item in content_items)
            right = max(float(item.get("x", card_x)) + float(item.get("w", 0)) for item in content_items)
            bottom = max(float(item.get("y", card_y)) + float(item.get("h", 0)) for item in content_items)
            bbox = [round(left), round(top), round(max(0, right - left)), round(max(0, bottom - top))]
            occupancy = ((right - left) * (bottom - top)) / max(1.0, card_w * card_h)
        else:
            bbox = [round(card_x), round(card_y), 0, 0]
            occupancy = 0.0
        card_metrics.append({"element_id": card.get("id"), "content_bbox": bbox, "occupancy_ratio": round(min(1.0, occupancy), 6)})
    evidence_hash = hashlib.sha256(json_bytes(evidence)).hexdigest()
    return {
        "page": number,
        "background": page_palette["ground"],
        "reader_title": str(_copy(page).get("headline") or ""),
        "composition": composition,
        "visible_identity_owner": owner,
        "visible_game_ids": _ids(page.get("visible_game_ids")),
        "asset_ids": _ids(page.get("asset_ids")),
        "elements": elements,
        "layout_metrics": {
            "card_content": card_metrics,
            "visible_text_count": len(visible_text),
            "visible_image_count": len(visible_images),
            "lower_anchor": any(
                (
                    item.get("type") in {"text", "image"}
                    or (item.get("type") == "mark" and not item.get("decorative"))
                )
                and not item.get("decorative")
                and float(item.get("y", 0)) + float(item.get("h", 0)) > height * 0.55
                for item in elements
            ),
        },
        "machine_metadata": {"role": kind, "claim_id": str(page.get("claim", {}).get("claim_id") or ""), "evidence_hash": "sha256:" + evidence_hash, "narrative_move": str(page.get("narrative_move") or "")},
    }


def json_bytes(value: Any) -> bytes:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _validate_visual_brief(
    compiled_deck: Mapping[str, Any],
    visual_brief: Mapping[str, Any] | None,
    assets: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if not isinstance(visual_brief, Mapping):
        raise PublishLayoutError("visual_brief_required", "compose_publish_layout requires the current visual-brief")
    from .planning import validate_schema_document

    try:
        validate_schema_document("visual-brief.json", "visual-brief.schema.json", visual_brief)
    except (TypeError, ValueError) as exc:
        raise PublishLayoutError("visual_brief_invalid", str(exc), field="visual_brief") from exc
    asset_manifest = assets if isinstance(assets, Mapping) else {}
    asset_fingerprint = compute_asset_manifest_fingerprint(asset_manifest)
    if visual_brief.get("compiled_deck_fingerprint") != compiled_deck.get("compiled_deck_fingerprint"):
        raise PublishLayoutError("visual_brief_compiled_stale", "visual brief does not match the compiled deck", field="visual_brief.compiled_deck_fingerprint")
    if visual_brief.get("asset_manifest_fingerprint") != asset_fingerprint:
        raise PublishLayoutError("visual_brief_assets_stale", "visual brief does not match the asset manifest", field="visual_brief.asset_manifest_fingerprint")
    expected_brief_fingerprint = compute_visual_brief_fingerprint(visual_brief)
    if visual_brief.get("visual_brief_fingerprint") != expected_brief_fingerprint:
        raise PublishLayoutError("visual_brief_fingerprint_mismatch", "visual brief fingerprint does not match its contents", field="visual_brief.visual_brief_fingerprint")
    return visual_brief


def _validate_exposure_policy(
    pages: list[Mapping[str, Any]],
    visual_brief: Mapping[str, Any],
) -> set[str]:
    policy = visual_brief.get("deck_policy")
    if not isinstance(policy, Mapping):
        raise PublishLayoutError("deck_policy_missing", "visual brief must provide deck_policy", field="visual_brief.deck_policy")
    max_game_pages = int(policy.get("max_pages_per_game", 0))
    max_asset_pages = int(policy.get("max_pages_per_asset", 0))
    min_gap = int(policy.get("min_page_gap_for_repeated_game", 0))
    candidate_assets = visual_brief.get("candidate_assets")
    if not isinstance(candidate_assets, list):
        raise PublishLayoutError("candidate_assets_missing", "visual brief must provide candidate_assets", field="visual_brief.candidate_assets")
    candidate_ids = {str(row.get("asset_id")) for row in candidate_assets if isinstance(row, Mapping) and row.get("asset_id")}
    game_pages: dict[str, list[int]] = {}
    asset_pages: dict[str, list[int]] = {}
    for page in pages:
        page_number = int(page.get("page", 0))
        game_ids = _ids(page.get("visible_game_ids"))
        asset_ids = _ids(page.get("asset_ids"))
        missing_candidates = sorted(set(asset_ids) - candidate_ids)
        if missing_candidates:
            raise PublishLayoutError("asset_not_candidate", "layout uses assets outside visual brief candidates: " + ", ".join(missing_candidates), page=page_number, field="asset_ids")
        for game_id in set(game_ids):
            game_pages.setdefault(game_id, []).append(page_number)
        for asset_id in set(asset_ids):
            asset_pages.setdefault(asset_id, []).append(page_number)
    for game_id, page_numbers in game_pages.items():
        if len(page_numbers) > max_game_pages:
            raise PublishLayoutError("game_exposure_policy", f"{game_id} appears on too many pages", field="visible_game_ids")
        for first, second in zip(sorted(page_numbers), sorted(page_numbers)[1:]):
            if second - first < min_gap:
                raise PublishLayoutError("game_exposure_gap_policy", f"repeated game {game_id} violates the minimum page gap", field="visible_game_ids")
    for asset_id, page_numbers in asset_pages.items():
        if len(page_numbers) > max_asset_pages:
            raise PublishLayoutError("asset_exposure_policy", f"{asset_id} appears on too many pages", field="asset_ids")
    return candidate_ids


def compose_publish_layout(
    compiled_deck: Mapping[str, Any] | Any,
    art_direction: Mapping[str, Any] | None,
    assets: Mapping[str, Any] | None,
    visual_brief: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose a safe, adaptive publish layout from the current visual inputs."""

    if hasattr(compiled_deck, "to_dict"):
        compiled_deck = compiled_deck.to_dict()
    if not isinstance(compiled_deck, Mapping) or compiled_deck.get("format") != "steam-visualogue-compiled-deck":
        raise PublishLayoutError("compiled_deck_required", "compose_publish_layout accepts only compiled-deck")
    pages = compiled_deck.get("pages")
    if not isinstance(pages, list) or not 12 <= len(pages) <= 18:
        raise PublishLayoutError("page_count_invalid", "compiled deck must contain 12–18 pages")
    locale = normalize_report_locale(str(compiled_deck.get("locale") or ""))
    visual_brief = _validate_visual_brief(compiled_deck, visual_brief, assets)
    direction = art_direction if isinstance(art_direction, Mapping) else {}
    candidate_ids = _validate_exposure_policy(pages, visual_brief)
    working_size = direction.get("working_size", list(DEFAULT_WORKING_SIZE))
    if not isinstance(working_size, list) or len(working_size) != 2:
        working_size = list(DEFAULT_WORKING_SIZE)
    width = int(direction.get("working_width", working_size[0]))
    height = int(direction.get("working_height", working_size[1]))
    final_size = direction.get("final_size", list(DEFAULT_FINAL_SIZE))
    if not isinstance(final_size, list) or len(final_size) != 2:
        final_size = list(DEFAULT_FINAL_SIZE)
    if width <= 0 or height <= 0 or any(not isinstance(value, int) or value <= 0 for value in final_size):
        raise PublishLayoutError("size_invalid", "publish layout sizes must be positive integers")
    palette = _palette(direction)
    records = _asset_records(assets)
    output_pages: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, Mapping):
            raise PublishLayoutError("page_invalid", "compiled page must be an object")
        built = _build_page(page, width, height, palette, locale, records)
        non_candidate = sorted(set(_ids(built.get("asset_ids"))) - candidate_ids)
        if non_candidate:
            raise PublishLayoutError("asset_not_candidate", "layout uses assets outside visual brief candidates: " + ", ".join(non_candidate), page=int(page.get("page", 0)), field="asset_ids")
        output_pages.append(built)
    regular_family = font_family(locale, False)
    bold_family = font_family(locale, True)
    layout_input_fingerprint = compute_layout_input_fingerprint(
        str(compiled_deck.get("compiled_deck_fingerprint") or ""),
        direction,
        str(visual_brief.get("visual_brief_fingerprint") or ""),
        assets if isinstance(assets, Mapping) else {},
    )
    payload = {
        "format": PUBLISH_LAYOUT_FORMAT,
        "locale": locale,
        "catalog_version": str(compiled_deck.get("catalog_version") or ""),
        "label_fingerprint": str(compiled_deck.get("label_fingerprint") or ""),
        "working_size": [width, height],
        "final_size": [int(final_size[0]), int(final_size[1])],
        "palette": palette,
        "deck_schema_fingerprint": compiled_deck.get("deck_schema_fingerprint"),
        "compiled_deck_fingerprint": compiled_deck.get("compiled_deck_fingerprint"),
        "visual_brief_fingerprint": visual_brief.get("visual_brief_fingerprint"),
        "layout_input_fingerprint": layout_input_fingerprint,
        "font_families": {"regular": regular_family, "bold": bold_family},
        "pages": output_pages,
        "machine_metadata": {"page_count": len(output_pages), "visible_chrome": ["page-number"], "internal_fields": ["role", "claim_id", "evidence_hash", "narrative_move"]},
    }
    from .planning import validate_schema_document

    validate_schema_document("publish-layout", "publish-layout.schema.json", payload)
    return payload


__all__ = ["PUBLISH_LAYOUT_FORMAT", "PublishLayoutError", "compose_publish_layout", "font_family", "_load_font", "_load_font_bold"]
