"""Contact-sheet assembly for deck-level visual QA."""

from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Iterable

try:
    from PIL import Image, ImageDraw, ImageOps
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Steam Visualogue's visual layer requires Pillow. Install it with "
        "`python -m pip install Pillow`."
    ) from exc

from .publish_layout import _load_font


def _wrap_label(text: str, font: object, width: int, report_locale: str) -> list[str]:
    units = list(text) if report_locale == "zh-CN" else re.findall(r"\S+\s*", text)
    lines: list[str] = []
    current = ""
    for unit in units:
        candidate = current + unit
        try:
            measured = float(font.getlength(candidate.rstrip()))  # type: ignore[attr-defined]
        except AttributeError:
            box = font.getbbox(candidate.rstrip())  # type: ignore[attr-defined]
            measured = float(box[2] - box[0])
        if not current or measured <= width:
            current = candidate
        else:
            lines.append(current.rstrip())
            current = unit.lstrip()
    if current.strip():
        lines.append(current.rstrip())
    return lines or [""]


def _collect_pages(value: str | Path | Iterable[str | Path]) -> tuple[list[Path], Path | None]:
    if isinstance(value, (str, Path)):
        path = Path(value)
        if path.is_dir():
            pages = sorted(
                candidate
                for candidate in path.glob("*.png")
                if candidate.is_file()
                and re.fullmatch(r"\d{2}\.png", candidate.name.lower())
            )
            return pages, path
        return [path], path.parent
    return [Path(item) for item in value], None


def make_contact_sheet(
    pages: str | Path | Iterable[str | Path],
    output_path: str | Path | None = None,
    *,
    columns: int = 4,
    thumbnail_width: int = 360,
    gap: int = 28,
    background: str = "#101214",
    report_locale: str = "en-US",
    layout: dict | None = None,
) -> str:
    """Build a reader-facing contact sheet from rendered page PNGs.

    ``pages`` may be a deck output directory, one image path, or an iterable of
    image paths. When a directory is supplied the default output is
    ``<directory>/contact-sheet.png``.
    """

    if columns < 1 or columns > 12:
        raise ValueError("columns must be between 1 and 12")
    if thumbnail_width < 80:
        raise ValueError("thumbnail_width must be at least 80 pixels")
    if gap < 0:
        raise ValueError("gap cannot be negative")

    paths, inferred_dir = _collect_pages(pages)
    paths = [path for path in paths if path.is_file()]
    if not paths:
        raise ValueError("No rendered page images were found for the contact sheet")
    destination = Path(output_path) if output_path is not None else (inferred_dir or Path.cwd()) / "contact-sheet.png"
    destination.parent.mkdir(parents=True, exist_ok=True)

    thumbs: list[tuple[Path, Image.Image]] = []
    maximum_height = 0
    for path in paths:
        try:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                thumb_height = round(thumbnail_width * image.height / image.width)
                thumbnail = image.resize((thumbnail_width, thumb_height), Image.Resampling.LANCZOS)
        except OSError as exc:
            raise ValueError(f"Cannot read contact-sheet page: {path}") from exc
        thumbs.append((path, thumbnail))
        maximum_height = max(maximum_height, thumbnail.height)

    font = _load_font(max(18, round(thumbnail_width * 0.055)), report_locale)
    label_line_height = max(22, round(thumbnail_width * 0.07))
    label_height = max(44, label_line_height * 3 + 8)
    rows = math.ceil(len(thumbs) / columns)
    canvas_width = gap + columns * (thumbnail_width + gap)
    cell_height = maximum_height + label_height
    canvas_height = gap + rows * (cell_height + gap)
    canvas = Image.new("RGB", (canvas_width, canvas_height), background)
    draw = ImageDraw.Draw(canvas)
    for index, (path, thumbnail) in enumerate(thumbs):
        row, column = divmod(index, columns)
        x = gap + column * (thumbnail_width + gap)
        y = gap + row * (cell_height + gap)
        canvas.paste(thumbnail, (x, y))
        reader_title = ""
        if isinstance(layout, dict):
            pages_in_layout = layout.get("pages", [])
            if isinstance(pages_in_layout, list) and index < len(pages_in_layout):
                page = pages_in_layout[index]
                reader_title = str(page.get("reader_title") or "") if isinstance(page, dict) else ""
        label = f"{index + 1:02d}  {reader_title}".rstrip()
        lines = _wrap_label(label, font, thumbnail_width, report_locale)
        if len(lines) > 3:
            lines = lines[:3]
            last = lines[-1].rstrip()
            while last and _wrap_label(last + "…", font, thumbnail_width, report_locale)[0] != last + "…":
                last = last[:-1].rstrip()
            lines[-1] = (last + "…") if last else "…"
        for line_index, line in enumerate(lines):
            draw.text((x, y + maximum_height + max(4, gap // 5) + line_index * label_line_height), line, fill="#F3F0E8", font=font)

    canvas.save(destination, format="PNG", compress_level=9)
    return str(destination.resolve())


__all__ = ["make_contact_sheet"]
