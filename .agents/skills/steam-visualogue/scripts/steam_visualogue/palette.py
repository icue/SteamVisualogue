"""Deterministic Oklch colour analysis for Steam artwork.

Pixels are scored in a perceptual colour space, clustered in Oklab, and
represented by an actual high-chroma pixel (a peak medoid) instead of an
averaged colour that can turn complementary artwork muddy.
"""

from __future__ import annotations

from collections import Counter
import colorsys
import hashlib
from io import BytesIO
import math
from typing import Iterable

try:
    from PIL import Image, ImageOps
except ImportError as exc:  # pragma: no cover - exercised only without the dependency
    raise RuntimeError(
        "Steam Visualogue's visual layer requires Pillow. Install it with "
        "`python -m pip install Pillow`."
    ) from exc


RGBColor = tuple[int, int, int]
OklabColor = tuple[float, float, float]
OklchColor = tuple[float, float, float]

_MAX_SAMPLE_EDGE = 256
_ALPHA_THRESHOLD = 16

# Keep this identifier complete enough to describe every deterministic choice
# that affects an image palette.  It is also part of the persistent cache key.
PALETTE_ALGORITHM = "weighted-oklch-peak-medoid"
DEFAULT_COLOR_COUNT = 5


def srgb_to_linear(channel: int | float) -> float:
    """Convert one sRGB channel in the 0..255 range to linear light."""

    value = float(channel) / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _cuberoot(value: float) -> float:
    return math.copysign(abs(value) ** (1.0 / 3.0), value)


def rgb_to_oklab(rgb: RGBColor) -> OklabColor:
    """Convert sRGB to Oklab using the published D65 matrices."""

    red, green, blue = (srgb_to_linear(channel) for channel in rgb)
    l = _cuberoot(0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue)
    m = _cuberoot(0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue)
    s = _cuberoot(0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue)
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


def rgb_to_oklch(rgb: RGBColor) -> OklchColor:
    """Convert an RGB triplet to Oklch ``(L, C, h_degrees)``."""

    lightness, a_value, b_value = rgb_to_oklab(rgb)
    chroma = math.hypot(a_value, b_value)
    hue = math.degrees(math.atan2(b_value, a_value)) % 360.0 if chroma > 1e-12 else 0.0
    return lightness, chroma, hue


def _linear_channel(channel: int) -> float:
    return srgb_to_linear(channel)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    red, green, blue = (_linear_channel(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _colour_record(rgb: RGBColor, weight: float) -> dict:
    lightness, chroma, hue = rgb_to_oklch(rgb)
    return {
        "hex": "#{:02X}{:02X}{:02X}".format(*rgb),
        "rgb": list(rgb),
        "weight": round(weight, 6),
        "luminance": round(_relative_luminance(rgb), 6),
        "oklch": [round(lightness, 6), round(chroma, 6), round(hue, 4)],
        "chroma": round(chroma, 6),
    }


def _saliency_weight(rgb: RGBColor, alpha: int) -> tuple[float, float, float]:
    """Return saliency weight, Oklab lightness, and Oklch chroma."""

    lightness, chroma, _ = rgb_to_oklch(rgb)
    alpha_weight = max(0.0, min(1.0, alpha / 255.0))
    saliency = alpha_weight * (1.0 + 8.0 * chroma**1.4)
    saliency *= math.exp(-((lightness - 0.52) ** 4) / 0.15)
    return saliency, lightness, chroma


def _distance_squared(left: Iterable[float], right: Iterable[float]) -> float:
    return sum((a - b) ** 2 for a, b in zip(left, right))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * max(0.0, min(1.0, percentile))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _peak_medoid(
    members: list[tuple[RGBColor, float]],
    oklab: dict[RGBColor, OklabColor],
    oklch: dict[RGBColor, OklchColor],
) -> RGBColor:
    """Select a vivid, locally dense *real* pixel from a cluster."""

    chroma_floor = _percentile([oklch[rgb][1] for rgb, _ in members], 0.80)
    candidates = [
        (rgb, weight)
        for rgb, weight in members
        if oklch[rgb][1] + 1e-12 >= chroma_floor
    ]
    candidates.sort(key=lambda item: (-oklch[item[0]][1], -item[1], item[0]))
    candidates = candidates[:128]
    member_weight = {rgb: weight for rgb, weight in members}
    # Density is a tie-breaker, not a second full-resolution scan. Keeping
    # the heaviest members makes the cost bounded for large source artworks
    # while preserving deterministic results and the cluster's visual peaks.
    density_members = sorted(members, key=lambda item: (-item[1], item[0]))[:2048]

    def score(candidate: RGBColor) -> tuple[float, float, float, tuple[int, int, int]]:
        density = 0.0
        for other, _ in density_members:
            distance = _distance_squared(oklab[candidate], oklab[other])
            density += member_weight[other] / (1.0 + distance * 180.0)
        return (
            density,
            member_weight[candidate],
            oklch[candidate][1],
            tuple(-channel for channel in candidate),
        )

    return max((rgb for rgb, _ in candidates), key=score)


def _cluster_colours(
    histogram: Counter[RGBColor], requested: int
) -> list[tuple[RGBColor, float]]:
    """Run weighted, deterministically seeded k-means in Oklab space."""

    items = sorted(histogram.items(), key=lambda item: item[0])
    oklab = {rgb: rgb_to_oklab(rgb) for rgb, _ in items}
    oklch = {rgb: rgb_to_oklch(rgb) for rgb, _ in items}
    cluster_count = min(requested, len(items))

    first = min(items, key=lambda item: (-item[1], item[0]))[0]
    centroids = [oklab[first]]
    chosen = {first}
    while len(centroids) < cluster_count:
        candidates = []
        for rgb, count in items:
            if rgb in chosen:
                continue
            distance = min(_distance_squared(oklab[rgb], centroid) for centroid in centroids)
            candidates.append((distance * math.sqrt(count), rgb))
        if not candidates:
            break
        _, selected = max(candidates, key=lambda item: (item[0], tuple(-x for x in item[1])))
        chosen.add(selected)
        centroids.append(oklab[selected])

    assignments: list[list[tuple[RGBColor, int]]] = []
    for _ in range(24):
        assignments = [[] for _ in centroids]
        for rgb, count in items:
            index = min(
                range(len(centroids)),
                key=lambda candidate: (
                    _distance_squared(oklab[rgb], centroids[candidate]),
                    candidate,
                ),
            )
            assignments[index].append((rgb, count))

        updated = []
        for index, members in enumerate(assignments):
            if not members:
                updated.append(centroids[index])
                continue
            total = sum(count for _, count in members)
            updated.append(
                tuple(
                    sum(oklab[rgb][axis] * count for rgb, count in members) / total
                    for axis in range(3)
                )
            )
        if all(_distance_squared(a, b) < 1e-12 for a, b in zip(centroids, updated)):
            centroids = updated
            break
        centroids = updated

    result = []
    for members in assignments:
        if not members:
            continue
        total = sum(count for _, count in members)
        representative = _peak_medoid(members, oklab, oklch)
        result.append((representative, total))
    return sorted(result, key=lambda item: (-item[1], item[0]))


def _extract_palette(opened: Image.Image, source_hash: str, colors: int, source_label: str) -> dict:
    if isinstance(colors, bool) or not isinstance(colors, int) or colors < 1 or colors > 32:
        raise ValueError("colors must be an integer between 1 and 32")
    image = ImageOps.exif_transpose(opened).convert("RGBA")
    image.thumbnail((_MAX_SAMPLE_EDGE, _MAX_SAMPLE_EDGE), Image.Resampling.BOX)
    weighted_pixels: Counter[RGBColor] = Counter()
    luminance_total = 0.0
    saturation_total = 0.0
    chroma_total = 0.0
    lightness_total = 0.0
    alpha_total = 0
    valid_pixels = 0
    pixels = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
    for red, green, blue, alpha in pixels:
        if alpha < _ALPHA_THRESHOLD:
            continue
        rgb = (red, green, blue)
        saliency, lightness, chroma = _saliency_weight(rgb, alpha)
        weighted_pixels[rgb] += saliency
        alpha_weight = alpha / 255.0
        luminance_total += _relative_luminance(rgb) * alpha_weight
        saturation_total += colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)[1] * alpha_weight
        chroma_total += chroma * alpha_weight
        lightness_total += lightness * alpha_weight
        alpha_total += alpha_weight
        valid_pixels += 1

    if not weighted_pixels or alpha_total == 0:
        raise ValueError(f"Palette source contains no visible pixels: {source_label}")

    clusters = _cluster_colours(weighted_pixels, colors)
    total_weight = sum(weight for _, weight in clusters)
    dominant = [_colour_record(rgb, weight / total_weight) for rgb, weight in clusters]

    return {
        "dominant_colors": dominant,
        "mean_luminance": round(luminance_total / alpha_total, 6),
        "mean_saturation": round(saturation_total / alpha_total, 6),
        "mean_chroma": round(chroma_total / alpha_total, 6),
        "mean_oklab_lightness": round(lightness_total / alpha_total, 6),
        "source_image_hash": source_hash,
        "valid_pixel_count": valid_pixels,
        "palette_space": "oklch",
        "algorithm": PALETTE_ALGORITHM,
    }


def extract_palette_bytes(payload: bytes, colors: int = DEFAULT_COLOR_COUNT) -> dict:
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("Palette source bytes are empty")
    source_hash = hashlib.sha256(payload).hexdigest()
    try:
        with Image.open(BytesIO(payload)) as opened:
            return _extract_palette(opened, source_hash, colors, "memory image")
    except OSError as error:
        raise ValueError("Palette source bytes are not a readable image") from error


def aggregate_palettes(
    palettes: Iterable[dict], weights: Iterable[float], colors: int = DEFAULT_COLOR_COUNT
) -> dict:
    """Aggregate per-artwork palettes with the same deterministic peak picker."""

    weighted: Counter[RGBColor] = Counter()
    luminance_total = 0.0
    saturation_total = 0.0
    chroma_total = 0.0
    lightness_total = 0.0
    game_weight_total = 0.0
    for palette, raw_game_weight in zip(palettes, weights):
        game_weight = max(0.0, float(raw_game_weight))
        if game_weight <= 0 or not isinstance(palette, dict):
            continue
        used = False
        for item in palette.get("dominant_colors", []):
            if not isinstance(item, dict):
                continue
            rgb = item.get("rgb")
            if not isinstance(rgb, list) or len(rgb) != 3:
                hex_value = item.get("hex")
                if isinstance(hex_value, str) and len(hex_value) == 7:
                    try:
                        rgb = [int(hex_value[index:index + 2], 16) for index in (1, 3, 5)]
                    except ValueError:
                        continue
                else:
                    continue
            color_weight = max(0.0, float(item.get("weight", 0.0)))
            if color_weight <= 0:
                continue
            rgb_value = tuple(int(max(0, min(255, channel))) for channel in rgb)
            weighted[rgb_value] += game_weight * color_weight
            used = True
        if used:
            luminance_total += game_weight * float(palette.get("mean_luminance") or 0.0)
            saturation_total += game_weight * float(palette.get("mean_saturation") or 0.0)
            chroma_total += game_weight * float(palette.get("mean_chroma") or 0.0)
            lightness_total += game_weight * float(palette.get("mean_oklab_lightness") or 0.0)
            game_weight_total += game_weight
    if not weighted or game_weight_total <= 0:
        return {
            "dominant_colors": [],
            "mean_luminance": None,
            "mean_saturation": None,
            "mean_chroma": None,
            "mean_oklab_lightness": None,
            "palette_space": "oklch",
            "algorithm": PALETTE_ALGORITHM,
        }
    clusters = _cluster_colours(weighted, colors)
    total = sum(weight for _, weight in clusters)
    return {
        "dominant_colors": [_colour_record(rgb, weight / total) for rgb, weight in clusters],
        "mean_luminance": round(luminance_total / game_weight_total, 6),
        "mean_saturation": round(saturation_total / game_weight_total, 6),
        "mean_chroma": round(chroma_total / game_weight_total, 6),
        "mean_oklab_lightness": round(lightness_total / game_weight_total, 6),
        "palette_space": "oklch",
        "algorithm": PALETTE_ALGORITHM,
    }


__all__ = [
    "DEFAULT_COLOR_COUNT",
    "PALETTE_ALGORITHM",
    "aggregate_palettes",
    "extract_palette_bytes",
    "rgb_to_oklab",
    "rgb_to_oklch",
    "srgb_to_linear",
]
