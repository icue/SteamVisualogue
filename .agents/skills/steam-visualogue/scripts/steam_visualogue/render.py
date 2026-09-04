"""Authoritative high-resolution compositor for Steam Visualogue decks."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Callable

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Steam Visualogue's visual layer requires Pillow. Install it with "
        "`python -m pip install Pillow`."
    ) from exc

from .context_budget import sha256_path_hex
from .fingerprint import image_pixel_sha256
from .planning import validate_schema_document
from .publish_layout import _load_font, _load_font_bold


def _perspective_coeffs(pa: list[tuple[float, float]], pb: list[tuple[float, float]]) -> list[float]:
    matrix = []
    for p1, p2 in zip(pa, pb):
        matrix.append([p2[0], p2[1], 1, 0, 0, 0, -p1[0] * p2[0], -p1[0] * p2[1]])
        matrix.append([0, 0, 0, p2[0], p2[1], 1, -p1[1] * p2[0], -p1[1] * p2[1]])
    n = len(matrix)
    b = [pa[0][0], pa[0][1], pa[1][0], pa[1][1], pa[2][0], pa[2][1], pa[3][0], pa[3][1]]
    M = [matrix[i] + [b[i]] for i in range(n)]
    for i in range(n):
        max_row = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[max_row] = M[max_row], M[i]
        pivot = M[i][i]
        for j in range(i, n + 1):
            M[i][j] /= pivot
        for k in range(n):
            if k != i:
                factor = M[k][i]
                for j in range(i, n + 1):
                    M[k][j] -= factor * M[i][j]
    return [M[i][-1] for i in range(n)]


def _size(layout: dict, key: str) -> tuple[int, int]:
    value = layout.get(key, {})
    if isinstance(value, dict):
        return int(value["width"]), int(value["height"])
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    raise ValueError(f"layout.{key} must define width and height")


def _load_asset_manifest(assets_dir: Path) -> dict:
    path = assets_dir / "manifest.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    assets = payload.get("assets", {}) if isinstance(payload, dict) else {}
    return assets if isinstance(assets, dict) else {}


def _clean_previous_render(output_dir: Path) -> None:
    manifest_path = output_dir / ".render-manifest.json"
    if not manifest_path.is_file():
        return
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    root = output_dir.resolve()
    for page in payload.get("pages", []) if isinstance(payload, dict) else []:
        if not isinstance(page, dict):
            continue
        for key in ("file", "thumbnail"):
            relative = page.get(key)
            if not isinstance(relative, str):
                continue
            candidate = (output_dir / relative).resolve()
            if candidate != root and candidate.is_relative_to(root) and candidate.is_file():
                candidate.unlink()


def _resolve_asset(
    asset_id: str,
    assets_dir: Path,
    manifest: dict,
) -> tuple[Path | None, str, dict]:
    root = assets_dir.resolve()

    def safe_path(value: str) -> Path | None:
        raw = Path(value)
        if raw.is_absolute():
            return None
        candidate = (assets_dir / raw).resolve()
        return candidate if candidate.is_relative_to(root) else None

    entry = manifest.get(asset_id)
    if isinstance(entry, dict):
        status = str(entry.get("status", "")).lower()
        candidate = entry.get("path")
        if status == "ready" and isinstance(candidate, str):
            path = safe_path(candidate)
            if path is not None and path.is_file():
                expected = entry.get("sha256")
                if isinstance(expected, str):
                    try:
                        if sha256_path_hex(path) != expected:
                            return None, "integrity-mismatch", dict(entry)
                    except OSError:
                        return None, "unreadable", dict(entry)
                if asset_id.startswith("generated:"):
                    pixel_digest = asset_id.rsplit(":", 1)[-1]
                    if (
                        entry.get("source") != "generated-raw"
                        or entry.get("pixel_sha256") != pixel_digest
                        or image_pixel_sha256(path) != pixel_digest
                        or not isinstance(expected, str)
                    ):
                        return None, "invalid-generated-registration", dict(entry)
                return path, "manifest", dict(entry)
            return None, "missing", dict(entry)
        return None, status or "not-ready", dict(entry)

    return None, "missing", {}


def _cover(image: Image.Image, size: tuple[int, int], focus_x: float, focus_y: float) -> Image.Image:
    target_width, target_height = size
    scale = max(target_width / image.width, target_height / image.height)
    scaled = image.resize(
        (max(target_width, round(image.width * scale)), max(target_height, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    excess_x = scaled.width - target_width
    excess_y = scaled.height - target_height
    left = min(excess_x, max(0, round(excess_x * focus_x)))
    top = min(excess_y, max(0, round(excess_y * focus_y)))
    return scaled.crop((left, top, left + target_width, top + target_height))


def _contain(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target = Image.new("RGBA", size, (0, 0, 0, 0))
    fitted = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
    if fitted.mode != "RGBA":
        fitted = fitted.convert("RGBA")
    position = ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2)
    target.paste(fitted, position, fitted)
    return target


def _parse_colour(value: str | tuple[int, int, int] | tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    if isinstance(value, tuple):
        channels = tuple(int(channel) for channel in value)
        if len(channels) == 3:
            return (*channels, 255)
        if len(channels) == 4:
            return channels
    if isinstance(value, str) and len(value) == 7 and value.startswith("#"):
        return (
            int(value[1:3], 16),
            int(value[3:5], 16),
            int(value[5:7], 16),
            255,
        )
    return (255, 255, 255, 255)


def _seed_bytes(item: dict) -> bytes:
    seed = str(item.get("seed") or item.get("id") or "mark")
    return hashlib.sha256(seed.encode("utf-8")).digest()


def render_alpha_mark(canvas: Image.Image, item: dict) -> None:
    """Render one of the deterministic data-art marks onto an RGBA canvas."""

    x, y = int(item.get("x", 0)), int(item.get("y", 0))
    width, height = max(1, int(item.get("w", 1))), max(1, int(item.get("h", 1)))
    opacity = min(1.0, max(0.0, float(item.get("opacity", 1.0))))
    base_colour = _parse_colour(item.get("color", "#FFFFFF"))
    fill = (*base_colour[:3], round(base_colour[3] * opacity))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    shape = str(item.get("shape", "rect"))
    stroke = max(1, int(item.get("stroke_width", 1)))
    box = (0, 0, max(0, width - 1), max(0, height - 1))
    radius = max(0, min(int(item.get("corner_radius", 0)), width // 2, height // 2))

    if shape in {"alpha_slab", "rect"}:
        if radius:
            draw.rounded_rectangle(box, radius=radius, fill=fill)
        else:
            draw.rectangle(box, fill=fill)
        rim_value = item.get("rim_color")
        if rim_value:
            rim = (*_parse_colour(str(rim_value))[:3], round(255 * opacity))
            rim_width = max(1, int(item.get("rim_width", stroke)))
            if radius:
                draw.rounded_rectangle(box, radius=radius, outline=rim, width=rim_width)
            else:
                draw.rectangle(box, outline=rim, width=rim_width)
    elif shape == "luminescent_rim":
        rim_value = item.get("rim_color") or item.get("color", "#FFFFFF")
        rim = (*_parse_colour(str(rim_value))[:3], round(220 * opacity))
        rim_width = max(1, int(item.get("rim_width", item.get("stroke_width", 2))))
        glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.rectangle(box, outline=(*rim[:3], max(20, rim[3] // 2)), width=rim_width * 2)
        glow = glow.filter(ImageFilter.GaussianBlur(max(1, rim_width * 2)))
        canvas.alpha_composite(glow, (x, y))
        draw.rectangle(box, outline=rim, width=rim_width)
    elif shape == "stipple_matrix":
        digest = _seed_bytes(item)
        count = min(4096, max(1, int(item.get("density", 384))))
        columns = max(1, round(math.sqrt(count * width / max(1, height))))
        rows = max(1, math.ceil(count / columns))
        cell_w = max(1, width // columns)
        cell_h = max(1, height // rows)
        for index in range(count):
            column, row = index % columns, index // columns
            jitter_x = digest[(index * 3) % len(digest)] % max(1, cell_w)
            jitter_y = digest[(index * 5 + 1) % len(digest)] % max(1, cell_h)
            mark_w = max(1, min(3, cell_w // 3))
            mark_h = max(1, min(3, cell_h // 3))
            point_alpha = max(16, min(160, fill[3] + digest[(index * 7 + 2) % len(digest)] // 4 - 32))
            draw.rectangle(
                (
                    min(width - 1, column * cell_w + jitter_x),
                    min(height - 1, row * cell_h + jitter_y),
                    min(width - 1, column * cell_w + jitter_x + mark_w),
                    min(height - 1, row * cell_h + jitter_y + mark_h),
                ),
                fill=(*fill[:3], point_alpha),
            )
    elif shape == "seismic_strata":
        digest = _seed_bytes(item)
        count = min(1024, max(8, int(item.get("frequency", max(16, width // 8)))))
        for index in range(count):
            position = round(index * max(0, width - 1) / max(1, count - 1))
            amplitude = max(1, round(height * (0.12 + (digest[index % len(digest)] / 255) * 0.72)))
            center = height // 2 + round((digest[(index + 11) % len(digest)] - 128) * height / 768)
            alpha = max(24, min(220, fill[3] - (index % 5) * 8))
            draw.line((position, center - amplitude // 2, position, center + amplitude // 2), fill=(*fill[:3], alpha), width=stroke)
    elif shape == "tectonic_monolith":
        digest = _seed_bytes(item)
        slice_count = min(32, max(1, int(item.get("layers", 7))))
        gap = max(1, height // 240)
        for index in range(slice_count):
            inset = round(index * width * 0.012)
            slice_top = round(index * height / slice_count)
            slice_bottom = round((index + 1) * height / slice_count) - gap
            jitter = (digest[index % len(digest)] % 9) - 4
            alpha = max(24, min(235, fill[3] - index * max(1, 100 // slice_count)))
            draw.rectangle(
                (max(0, inset + jitter), slice_top, min(width - 1, width - 1 - inset), max(slice_top, slice_bottom)),
                fill=(*fill[:3], alpha),
            )
    elif shape == "parametric_totem":
        metrics = [max(0.0, min(1.0, float(value))) for value in item.get("metrics", [0.25, 0.5, 0.75, 0.35])]
        while len(metrics) < 4:
            metrics.append(metrics[-1] if metrics else 0.5)
        layers = min(12, max(3, int(item.get("layers", 7))))
        inset_step = max(2, min(width, height) // (layers * 8))
        for index in range(layers):
            inset = index * inset_step
            alpha = max(30, min(230, fill[3] - index * 16))
            draw.rectangle(
                (inset, inset, max(inset, width - 1 - inset), max(inset, height - 1 - inset)),
                outline=(*fill[:3], alpha),
                width=max(1, stroke - index // 4),
            )
        horizontal = round(height * (0.25 + metrics[0] * 0.5))
        vertical = round(width * (0.25 + metrics[1] * 0.5))
        draw.line((0, horizontal, width - 1, horizontal), fill=(*fill[:3], max(32, fill[3] // 2)), width=stroke)
        draw.line((vertical, 0, vertical, height - 1), fill=(*fill[:3], max(32, fill[3] // 2)), width=stroke)
        bar_height = max(2, round(height * 0.025))
        for index, metric in enumerate(metrics[:4]):
            bar_width = max(2, round(width * (0.18 + metric * 0.62)))
            bar_y = min(height - bar_height, round((index + 1) * height / 5))
            draw.rectangle((0, bar_y, bar_width, bar_y + bar_height), fill=(*fill[:3], max(30, fill[3] - index * 24)))
    elif shape == "crosshair":
        center_x, center_y = width // 2, height // 2
        draw.line((center_x, 0, center_x, height - 1), fill=fill, width=stroke)
        draw.line((0, center_y, width - 1, center_y), fill=fill, width=stroke)
    elif shape == "bracket":
        draw.line((0, 0, width - 1, 0, width - 1, height - 1), fill=fill, width=stroke)
    elif shape == "line":
        draw.line((0, 0, width - 1, height - 1), fill=fill, width=stroke)
    else:
        draw.rectangle(box, fill=fill)

    canvas.alpha_composite(layer, (x, y))


def _page_filename(page: dict, fallback_number: int) -> str:
    page_number = int(page.get("page", fallback_number))
    return f"{page_number:02d}.png"


def _draw_mark(canvas: Image.Image, item: dict) -> None:
    render_alpha_mark(canvas, item)


def _draw_text(draw: ImageDraw.ImageDraw, item: dict, report_locale: str = "en-US") -> None:
    x, y, width = int(item["x"]), int(item["y"]), int(item["w"])
    font_loader = (
        (lambda size: _load_font_bold(size, report_locale))
        if item.get("bold")
        else (lambda size: _load_font(size, report_locale))
    )
    font = font_loader(int(item["font_size"]))
    line_height = int(item.get("line_height", item["font_size"]))
    align = item.get("align", "left")
    color = item.get("color", "#FFFFFF")
    for index, line in enumerate(item.get("lines") or [item.get("text", "")]):
        try:
            measured = draw.textlength(str(line), font=font)
        except AttributeError:
            measured = font.getbbox(str(line))[2]
        if align == "center":
            line_x = x + (width - measured) / 2
        elif align == "right":
            line_x = x + width - measured
        else:
            line_x = x
        draw.text((round(line_x), y + index * line_height), str(line), font=font, fill=color)


def _render_image_element(
    canvas: Image.Image,
    item: dict,
    assets_dir: Path,
    asset_manifest: dict,
    palette: dict,
) -> dict:
    asset_id = str(item.get("asset_id", ""))
    x, y = int(item["x"]), int(item["y"])
    width, height = max(1, int(item["w"])), max(1, int(item["h"]))
    source_path, resolution, asset_record = _resolve_asset(asset_id, assets_dir, asset_manifest)
    source_size = None
    if source_path is None:
        raise ValueError(f"compiled asset {asset_id} is not ready")
    try:
        with Image.open(source_path) as opened:
            transposed = ImageOps.exif_transpose(opened)
            has_alpha = "A" in transposed.getbands() or "transparency" in opened.info
            source = transposed.convert("RGBA" if has_alpha else "RGB")
            source_size = [source.width, source.height]
            treatment = item.get("treatment", "crop")
            if treatment == "contain":
                rendered = _contain(source, (width, height))
            else:
                crop = item.get("crop", {}) if isinstance(item.get("crop"), dict) else {}
                rendered = _cover(
                    source,
                    (width, height),
                    float(crop.get("focus_x", 0.5)),
                    float(crop.get("focus_y", 0.5)),
                )
    except (OSError, ValueError) as exc:
        raise ValueError(f"compiled asset {asset_id} is unreadable") from exc

    corner_radius = int(item.get("corner_radius", 0) or 0)
    angle = float(item.get("angle", 0.0) or 0.0)
    rendered = rendered.convert("RGBA")

    if corner_radius > 0:
        mask = Image.new("L", (width, height), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((0, 0, width, height), radius=corner_radius, fill=255)
        border_draw = ImageDraw.Draw(rendered)
        border_draw.rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=corner_radius,
            outline=(255, 255, 255, 50),
            width=2,
        )
        rendered.putalpha(mask)

    perspective = item.get("perspective")
    if perspective in {"left_wall", "right_wall"}:
        src_w, src_h = source.size
        delta_y = round(height * 0.12)
        src_pts = [(0.0, 0.0), (float(src_w), 0.0), (float(src_w), float(src_h)), (0.0, float(src_h))]
        if perspective == "left_wall":
            dst_pts = [(0.0, 0.0), (float(width), float(delta_y)), (float(width), float(height - delta_y)), (0.0, float(height))]
        else:
            dst_pts = [(0.0, float(delta_y)), (float(width), 0.0), (float(width), float(height)), (0.0, float(height - delta_y))]
        coeffs = _perspective_coeffs(src_pts, dst_pts)
        transformed = source.convert("RGBA").transform(
            (width, height),
            Image.Transform.PERSPECTIVE,
            coeffs,
            Image.Resampling.BICUBIC,
            fillcolor=(0, 0, 0, 0),
        )
        border_draw = ImageDraw.Draw(transformed)
        border_draw.polygon(dst_pts, outline=(255, 255, 255, 75), width=2)

        shadow_layer = Image.new("RGBA", (width + 20, height + 20), (0, 0, 0, 0))
        s_draw = ImageDraw.Draw(shadow_layer)
        if perspective == "left_wall":
            s_draw.polygon([(10, 10), (width + 10, delta_y + 10), (width + 10, height - delta_y + 14), (10, height + 14)], fill=(0, 0, 0, 160))
        else:
            s_draw.polygon([(10, delta_y + 10), (width + 10, 10), (width + 10, height + 14), (10, height - delta_y + 14)], fill=(0, 0, 0, 160))
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=8))

        canvas.paste(shadow_layer, (x - 10, y - 5), shadow_layer)
        canvas.paste(transformed, (x, y), transformed)
    elif angle != 0.0:
        shadow_pad = 20
        shadow_layer = Image.new("RGBA", (width + shadow_pad * 2, height + shadow_pad * 2), (0, 0, 0, 0))
        s_draw = ImageDraw.Draw(shadow_layer)
        s_draw.rounded_rectangle(
            (shadow_pad + 2, shadow_pad + 5, shadow_pad + width + 2, shadow_pad + height + 5),
            radius=corner_radius or 4,
            fill=(0, 0, 0, 160),
        )
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=8))
        combined = Image.new("RGBA", shadow_layer.size, (0, 0, 0, 0))
        combined.paste(shadow_layer, (0, 0), shadow_layer)
        combined.paste(rendered, (shadow_pad, shadow_pad), rendered)

        rotated = combined.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=True)
        cx = x + width / 2
        cy = y + height / 2
        px = round(cx - rotated.width / 2)
        py = round(cy - rotated.height / 2)
        canvas.paste(rotated, (px, py), rotated)
    else:
        canvas.paste(rendered, (x, y), rendered if rendered.mode == "RGBA" else None)

    if item.get("treatment") == "framed":
        frame = ImageDraw.Draw(canvas)
        frame.rectangle(
            (x, y, x + width - 1, y + height - 1),
            outline=palette.get("ink", "#FFFFFF"),
            width=max(2, canvas.width // 540),
        )
    result = {
        "asset_id": asset_id,
        "status": "rendered",
        "resolution": resolution,
        "source_size": source_size,
        "target_size": [width, height],
        "treatment": item.get("treatment", "crop"),
    }
    for key in (
        "source",
        "kind",
        "asset_kind",
        "achievement_id",
        "achievement_state",
        "variant",
        "sha256",
        "pixel_sha256",
        "metadata_stripped",
        "review",
    ):
        if asset_record.get(key) is not None:
            result[key] = asset_record[key]
    return result


def render_deck(
    layout: dict,
    assets_dir: str | Path,
    output_dir: str | Path,
    *,
    progress: Callable[[str, int | None, int | None], None] | None = None,
) -> list[str]:
    """Render pages at working resolution, then downsample them with Lanczos.

    Asset IDs are resolved only through ``assets_dir/manifest.json``. Missing
    or unreadable images fail the compiled render before any output is
    accepted.
    """

    if not isinstance(layout, dict):
        raise TypeError("publish layout must be an object produced by compose_publish_layout")
    if layout.get("format") != "steam-visualogue-publish-layout":
        raise TypeError("render_deck accepts only a publish-layout artifact")
    try:
        validate_schema_document("publish-layout", "publish-layout.schema.json", layout)
    except ValueError as exc:
        raise ValueError(f"publish layout is invalid: {exc}") from exc
    working_size = _size(layout, "working_size")
    final_size = _size(layout, "final_size")
    report_locale = str(layout.get("locale") or "en-US")
    assets_root = Path(assets_dir)
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    _clean_previous_render(output_root)
    thumbnail_root = output_root / "thumbnails"
    thumbnail_root.mkdir(parents=True, exist_ok=True)
    asset_manifest = _load_asset_manifest(assets_root)
    palette = layout.get("palette", {}) if isinstance(layout.get("palette"), dict) else {}

    raw_pages = layout.get("pages", [])
    total = len(raw_pages) if isinstance(raw_pages, list) else 0
    if not isinstance(layout.get("deck_schema_fingerprint"), str) or not layout.get("deck_schema_fingerprint"):
        raise ValueError("publish layout is missing deck schema fingerprint")
    if not isinstance(layout.get("compiled_deck_fingerprint"), str) or not layout.get("compiled_deck_fingerprint"):
        raise ValueError("publish layout is missing compiled deck fingerprint")
    for index, page in enumerate(raw_pages):
        if not isinstance(page, dict):
            raise ValueError(f"publish layout page {index + 1} is not an object")
        if not isinstance(page.get("machine_metadata"), dict):
            raise ValueError(f"publish layout page {index + 1} is missing machine metadata")
    if progress is not None:
        progress("Rendering report pages", 0, total)
    output_paths: list[str] = []
    render_pages = []
    for index, page in enumerate(raw_pages):
        if not isinstance(page, dict):
            raise TypeError(f"layout page {index + 1} must be an object")
        canvas = Image.new("RGBA", working_size, page.get("background", palette.get("ground", "#121416")))
        draw = ImageDraw.Draw(canvas)
        assets = []
        for item in page.get("elements", []):
            if not isinstance(item, dict):
                continue
            element_type = item.get("type")
            if element_type == "image":
                assets.append(_render_image_element(canvas, item, assets_root, asset_manifest, palette))
            elif element_type == "mark":
                _draw_mark(canvas, item)
            elif element_type == "text":
                _draw_text(draw, item, report_locale)

        final = canvas.resize(final_size, Image.Resampling.LANCZOS).convert("RGB")
        page_number = int(page.get("page", index + 1))
        output_path = output_root / _page_filename(page, index + 1)
        # A fresh RGB image saved without pnginfo carries no user/software tags.
        final.save(output_path, format="PNG", compress_level=9)
        output_paths.append(str(output_path.resolve()))

        thumb_width = 360
        thumb_height = round(thumb_width * final.height / final.width)
        thumbnail = final.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
        thumbnail.save(thumbnail_root / output_path.name, format="PNG", compress_level=9)
        render_pages.append(
            {
                "page": page_number,
                "file": output_path.name,
                "thumbnail": f"thumbnails/{output_path.name}",
                "pixel_sha256": image_pixel_sha256(output_path),
                "assets": assets,
            }
        )
        if progress is not None:
            progress("Rendering report pages", index + 1, total)

    manifest = {
        "format": "steam-visualogue-render-manifest",
        "locale": layout.get("locale", "en-US"),
        "catalog_version": layout.get("catalog_version"),
        "label_fingerprint": layout.get("label_fingerprint"),
        "deck_schema_fingerprint": layout.get("deck_schema_fingerprint"),
        "compiled_deck_fingerprint": layout.get("compiled_deck_fingerprint"),
        "visual_brief_fingerprint": layout.get("visual_brief_fingerprint"),
        "layout_input_fingerprint": layout.get("layout_input_fingerprint"),
        "working_size": list(working_size),
        "final_size": list(final_size),
        "deterministic": True,
        "deck_pixel_sha256": hashlib.sha256(
            "".join(str(page.get("pixel_sha256") or "") for page in render_pages).encode("ascii")
        ).hexdigest(),
        "pages": render_pages,
    }
    (output_root / ".render-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output_paths


__all__ = ["render_alpha_mark", "render_deck"]
