"""Automatic game selection and dual-row slanted ribbon composer for editorial decks."""

from __future__ import annotations

import math
from typing import Any, Sequence

try:
    from PIL import Image, ImageDraw, ImageOps
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Steam Visualogue's ribbon composer requires Pillow. Install it with "
        "`python -m pip install Pillow`."
    ) from exc


def select_ribbon_game_ids(
    profile: dict[str, Any],
    count_per_ribbon: int = 12,
    candidate_pool_size: int = 36,
) -> tuple[list[int], list[int]]:
    """Select two disjoint, strictly deduplicated lists of top played games for opening and closing ribbons."""
    games = profile.get("games", []) if isinstance(profile, dict) else []
    if not isinstance(games, list):
        return [], []

    valid_games: list[dict[str, Any]] = []
    seen_appids: set[int] = set()

    for game in games:
        if not isinstance(game, dict):
            continue
        try:
            appid = int(game.get("appid", 0))
        except (ValueError, TypeError):
            continue
        if appid <= 0 or appid in seen_appids:
            continue
        seen_appids.add(appid)
        valid_games.append(game)

    def sort_key(g: dict[str, Any]) -> tuple[int, int]:
        pt = g.get("playtime_minutes") or g.get("playtime_forever") or g.get("playtime") or 0
        try:
            pt_forever = int(pt or 0)
        except (ValueError, TypeError):
            pt_forever = 0
        try:
            pt_2weeks = int(g.get("playtime_2weeks", 0) or 0)
        except (ValueError, TypeError):
            pt_2weeks = 0
        return (pt_forever, pt_2weeks)

    valid_games.sort(key=sort_key, reverse=True)

    sorted_appids = [int(g["appid"]) for g in valid_games]

    total_needed = count_per_ribbon * 2
    if len(sorted_appids) >= total_needed:
        opening_ids = sorted_appids[:count_per_ribbon]
        closing_ids = sorted_appids[count_per_ribbon:total_needed]
    elif len(sorted_appids) >= count_per_ribbon:
        opening_ids = sorted_appids[:count_per_ribbon]
        remaining = sorted_appids[count_per_ribbon:]
        closing_ids = remaining if remaining else list(opening_ids)
    else:
        opening_ids = list(sorted_appids)
        closing_ids = list(sorted_appids)

    return opening_ids, closing_ids


def select_ribbon_candidate_pool(
    profile: dict[str, Any],
    pool_size: int = 40,
) -> list[int]:
    """Select a broad candidate pool of top played games for fetching portraits."""
    games = profile.get("games", []) if isinstance(profile, dict) else []
    if not isinstance(games, list):
        return []

    valid_games: list[dict[str, Any]] = []
    seen_appids: set[int] = set()

    for game in games:
        if not isinstance(game, dict):
            continue
        try:
            appid = int(game.get("appid", 0))
        except (ValueError, TypeError):
            continue
        if appid <= 0 or appid in seen_appids:
            continue
        seen_appids.add(appid)
        valid_games.append(game)

    def sort_key(g: dict[str, Any]) -> tuple[int, int]:
        pt = g.get("playtime_minutes") or g.get("playtime_forever") or g.get("playtime") or 0
        try:
            pt_forever = int(pt or 0)
        except (ValueError, TypeError):
            pt_forever = 0
        try:
            pt_2weeks = int(g.get("playtime_2weeks", 0) or 0)
        except (ValueError, TypeError):
            pt_2weeks = 0
        return (pt_forever, pt_2weeks)

    valid_games.sort(key=sort_key, reverse=True)
    return [int(g["appid"]) for g in valid_games[:pool_size]]


def prepare_ribbon_card(
    image: Image.Image,
    size: tuple[int, int] = (280, 420),
    corner_radius: int = 18,
    border_color: tuple[int, int, int, int] = (255, 255, 255, 60),
    border_width: int = 3,
) -> Image.Image:
    """Resize, round corners, and apply a subtle border to a game cover card."""
    fitted = ImageOps.fit(image.convert("RGBA"), size, Image.Resampling.LANCZOS)
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0], size[1]), radius=corner_radius, fill=255)

    output = Image.new("RGBA", size, (0, 0, 0, 0))
    output.paste(fitted, (0, 0), mask)

    border_draw = ImageDraw.Draw(output)
    border_draw.rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1),
        radius=corner_radius,
        outline=border_color,
        width=border_width,
    )
    return output


def compose_dual_row_ribbon(
    card_images: Sequence[Image.Image],
    angle: float = -5.5,
    target_size: tuple[int, int] = (1872, 1440),
    card_size: tuple[int, int] = (280, 420),
    gap: int = 24,
    corner_radius: int = 18,
    fade_x: float = 0.18,
    fade_y: float = 0.10,
) -> Image.Image:
    """Compose multiple cover images into a dual-row tilted ribbon with smooth edge feathering."""
    if not card_images:
        return Image.new("RGBA", target_size, (0, 0, 0, 0))

    cards = [prepare_ribbon_card(img, size=card_size, corner_radius=corner_radius) for img in card_images]

    half = (len(cards) + 1) // 2
    row1 = cards[:half]
    row2 = cards[half:]

    card_w, card_h = card_size
    max_cols = max(len(row1), len(row2), 1)

    total_w = max_cols * (card_w + gap) + 800
    total_h = 2 * card_h + gap + 400
    canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))

    start_x1 = 120
    y1 = 100
    for i, card in enumerate(row1):
        canvas.paste(card, (start_x1 + i * (card_w + gap), y1), card)

    start_x2 = start_x1 - (card_w + gap) // 2
    y2 = y1 + card_h + gap
    for i, card in enumerate(row2):
        canvas.paste(card, (start_x2 + i * (card_w + gap), y2), card)

    rotated = canvas.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)

    target_w, target_h = target_size
    center_x, center_y = rotated.width // 2, rotated.height // 2
    crop_box = (
        max(0, center_x - target_w // 2),
        max(0, center_y - target_h // 2),
        min(rotated.width, center_x + target_w // 2),
        min(rotated.height, center_y + target_h // 2),
    )
    cropped = rotated.crop(crop_box)
    if cropped.size != (target_w, target_h):
        final_img = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        final_img.paste(cropped, ((target_w - cropped.width) // 2, (target_h - cropped.height) // 2))
    else:
        final_img = cropped

    w, h = final_img.size
    alpha_mask = Image.new("L", (w, h), 255)
    alpha_pixels = alpha_mask.load()

    for y in range(h):
        norm_y = y / max(1, h - 1)
        if norm_y < fade_y:
            fy = 0.5 - 0.5 * math.cos(math.pi * norm_y / fade_y)
        elif norm_y > (1.0 - fade_y):
            fy = 0.5 - 0.5 * math.cos(math.pi * (1.0 - norm_y) / fade_y)
        else:
            fy = 1.0

        for x in range(w):
            norm_x = x / max(1, w - 1)
            if norm_x < fade_x:
                fx = 0.5 - 0.5 * math.cos(math.pi * norm_x / fade_x)
            elif norm_x > (1.0 - fade_x):
                fx = 0.5 - 0.5 * math.cos(math.pi * (1.0 - norm_x) / fade_x)
            else:
                fx = 1.0

            alpha_pixels[x, y] = int(255 * fx * fy)

    r, g, b, orig_a = final_img.split()
    combined_a = Image.composite(orig_a, Image.new("L", (w, h), 0), alpha_mask)
    return Image.merge("RGBA", (r, g, b, combined_a))
