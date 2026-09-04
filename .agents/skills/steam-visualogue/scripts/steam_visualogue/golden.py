"""Pixel-level fingerprints used by deterministic visual regression checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Steam Visualogue's visual layer requires Pillow. Install it with "
        "`python -m pip install Pillow`."
    ) from exc


def pixel_sha256(path: str | Path) -> str:
    """Hash image mode, dimensions, and pixels while ignoring PNG metadata."""

    source = Path(path)
    try:
        with Image.open(source) as image:
            image.load()
            normalized = image.convert("RGB")
            digest = hashlib.sha256()
            digest.update(f"RGB:{normalized.width}x{normalized.height}\0".encode("ascii"))
            digest.update(normalized.tobytes())
            return digest.hexdigest()
    except (OSError, SyntaxError):
        # Preserve deterministic hashing for non-image export inputs.
        return hashlib.sha256(source.read_bytes()).hexdigest()


def deck_pixel_sha256(paths: Iterable[str | Path]) -> str:
    """Return one stable fingerprint for ordered page pixels."""

    digest = hashlib.sha256()
    for path in paths:
        digest.update(pixel_sha256(path).encode("ascii"))
    return digest.hexdigest()


__all__ = ["deck_pixel_sha256", "pixel_sha256"]
