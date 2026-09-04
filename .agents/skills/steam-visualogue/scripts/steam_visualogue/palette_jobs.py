"""Content-addressed palette resolution shared by report and page stages.

The public resolver deliberately has no knowledge of network clients or image
paths.  The caller supplies verified bytes; this module owns cache lookup,
content-level de-duplication, bounded process work, and deterministic result
assembly in the parent process.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import os
import re
from collections.abc import Hashable, Iterable, Mapping
from typing import Any

from .palette import DEFAULT_COLOR_COUNT, PALETTE_ALGORITHM, extract_palette_bytes


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ALGORITHM_RE = re.compile(r"[^\s]{1,128}")
_GAME_ASSET_RE = re.compile(r"^game:([1-9][0-9]*):(header|portrait)$")


@dataclass(frozen=True, order=True)
class PaletteCacheKey:
    """The complete identity of one cached image palette."""

    content_sha256: str
    algorithm: str = PALETTE_ALGORITHM
    color_count: int = DEFAULT_COLOR_COUNT

    def __post_init__(self) -> None:
        if not isinstance(self.content_sha256, str) or not _SHA256_RE.fullmatch(self.content_sha256):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.algorithm, str) or not _ALGORITHM_RE.fullmatch(self.algorithm):
            raise ValueError("algorithm must be a non-empty, whitespace-free string")
        if isinstance(self.color_count, bool) or not isinstance(self.color_count, int):
            raise ValueError("color_count must be an integer between 1 and 32")
        if not 1 <= self.color_count <= 32:
            raise ValueError("color_count must be an integer between 1 and 32")

    def as_tuple(self) -> tuple[str, str, int]:
        return self.content_sha256, self.algorithm, self.color_count


@dataclass
class PaletteResolution:
    """Deterministic resolution results and work counters.

    ``palettes`` and ``failures`` are keyed by the caller's input key.  Cache
    and extraction counters describe unique content/cache keys, which is the
    useful unit for measuring the work avoided by content de-duplication.
    """

    palettes: dict[Hashable, dict[str, Any]]
    failures: dict[Hashable, dict[str, Any]]
    cache_hits: int = 0
    extracted: int = 0
    submitted: int = 0
    unique_images: int = 0
    cache_write_failed: bool = False

    @property
    def results(self) -> dict[Hashable, dict[str, Any]]:
        """Return successful palettes, retained for callers that want one map."""

        return self.palettes

    @property
    def extraction_failures(self) -> int:
        return sum(
            1
            for failure in self.failures.values()
            if failure.get("status") == "extraction-failed"
        )

    def __getitem__(self, key: Hashable) -> dict[str, Any]:
        return self.palettes[key]


@dataclass(frozen=True)
class _ImageRequest:
    caller_key: Hashable
    cache_key: PaletteCacheKey
    payload: bytes


def _sort_key(value: Hashable) -> tuple[str, Any]:
    if isinstance(value, (int, float, str)):
        return type(value).__name__, value
    return type(value).__name__, str(value)


def _field(item: Any, *names: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        for name in names:
            if name in item:
                return item[name]
        return default
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _failure(status: str, error: BaseException | str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if error is not None:
        result["error"] = type(error).__name__ if isinstance(error, BaseException) else str(error)
    return result


def _palette_matches_key(palette: Any, key: PaletteCacheKey) -> bool:
    if not isinstance(palette, dict):
        return False
    if palette.get("algorithm") != key.algorithm:
        return False
    if palette.get("source_image_hash") != key.content_sha256:
        return False
    dominant = palette.get("dominant_colors")
    return isinstance(dominant, list) and bool(dominant) and len(dominant) <= key.color_count


def _normalise_request(item: Any, index: int) -> tuple[_ImageRequest | None, Hashable, dict[str, Any] | None]:
    raw_key = _field(item, "key", "image_key", "asset_id", "appid", "id", default=index)
    try:
        hash(raw_key)
    except TypeError:
        raw_key = str(raw_key)
    payload = _field(item, "payload", "bytes", "image_bytes", default=None)
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return None, raw_key, _failure("invalid-image", "payload is not bytes-like")
    try:
        raw_payload = bytes(payload)
    except (TypeError, ValueError):
        return None, raw_key, _failure("invalid-image", "payload is not bytes-like")
    if not raw_payload:
        return None, raw_key, _failure("invalid-image", "payload is empty")

    supplied_hash = _field(
        item,
        "content_sha256",
        "sha256",
        "source_image_hash",
        default=None,
    )
    if supplied_hash is None:
        content_sha256 = hashlib.sha256(raw_payload).hexdigest()
    elif not isinstance(supplied_hash, str):
        return None, raw_key, _failure("invalid-content-hash", "content_sha256 is invalid")
    else:
        content_sha256 = supplied_hash
    if not _SHA256_RE.fullmatch(content_sha256):
        return None, raw_key, _failure("invalid-content-hash", "content_sha256 is invalid")
    if hashlib.sha256(raw_payload).hexdigest() != content_sha256:
        return None, raw_key, _failure("invalid-content-hash", "content_sha256 does not match payload")

    raw_color_count = _field(item, "color_count", "colors", default=DEFAULT_COLOR_COUNT)
    raw_algorithm = _field(item, "algorithm", default=PALETTE_ALGORITHM)
    if isinstance(raw_color_count, bool) or not isinstance(raw_color_count, int):
        return None, raw_key, _failure("invalid-cache-key", "color_count is invalid")
    color_count = raw_color_count
    if not 1 <= color_count <= 32:
        return None, raw_key, _failure("invalid-cache-key", "color_count is invalid")
    if not isinstance(raw_algorithm, str) or not _ALGORITHM_RE.fullmatch(raw_algorithm):
        return None, raw_key, _failure("invalid-cache-key", "algorithm is invalid")
    algorithm = raw_algorithm
    try:
        cache_key = PaletteCacheKey(content_sha256, algorithm, color_count)
    except ValueError as error:
        return None, raw_key, _failure("invalid-cache-key", error)
    return _ImageRequest(raw_key, cache_key, raw_payload), raw_key, None


def _cache_key_parts(key: Any) -> tuple[str, str, int] | None:
    if isinstance(key, PaletteCacheKey):
        return key.as_tuple()
    if isinstance(key, Mapping):
        values = (key.get("content_sha256"), key.get("algorithm"), key.get("color_count"))
    elif isinstance(key, (tuple, list)) and len(key) == 3:
        values = tuple(key)
    else:
        values = (
            getattr(key, "content_sha256", None),
            getattr(key, "algorithm", None),
            getattr(key, "color_count", None),
        )
    try:
        return PaletteCacheKey(*values).as_tuple()
    except (TypeError, ValueError):
        return None


def _resolve_workers(process_workers: int | None) -> int:
    if process_workers is not None:
        return max(1, int(process_workers))
    return min(4, max(1, (os.cpu_count() or 2) - 1))


def _ordered_mapping(values: Mapping[Hashable, dict[str, Any]]) -> dict[Hashable, dict[str, Any]]:
    return {key: values[key] for key in sorted(values, key=_sort_key)}


def resolve_image_palettes(
    images: Iterable[Any],
    cache: Any,
    *,
    force_palette: bool = False,
    process_workers: int | None = None,
) -> PaletteResolution:
    """Resolve caller-keyed image palettes with content-level de-duplication."""

    palettes: dict[Hashable, dict[str, Any]] = {}
    failures: dict[Hashable, dict[str, Any]] = {}
    unique: dict[PaletteCacheKey, list[_ImageRequest]] = {}
    for index, item in enumerate(images):
        request, caller_key, invalid = _normalise_request(item, index)
        if invalid is not None or request is None:
            failures[caller_key] = invalid or _failure("invalid-image")
            continue
        unique.setdefault(request.cache_key, []).append(request)

    resolution = PaletteResolution(
        palettes={},
        failures={},
        unique_images=len(unique),
    )
    cached_by_parts: dict[tuple[str, str, int], dict[str, Any]] = {}
    if unique and cache is not None and not force_palette:
        cached = cache.get_image_palettes(list(unique))
        if isinstance(cached, Mapping):
            for cache_key, palette in cached.items():
                parts = _cache_key_parts(cache_key)
                if parts is not None:
                    cached_by_parts[parts] = palette

    misses: list[tuple[PaletteCacheKey, _ImageRequest]] = []
    for cache_key, requests in unique.items():
        palette = cached_by_parts.get(cache_key.as_tuple())
        if _palette_matches_key(palette, cache_key):
            resolution.cache_hits += 1
            for request in requests:
                palettes[request.caller_key] = palette
        else:
            misses.append((cache_key, requests[0]))

    computed: dict[PaletteCacheKey, dict[str, Any]] = {}

    def accept_result(cache_key: PaletteCacheKey, result: Any = None, error: BaseException | None = None) -> None:
        if error is not None:
            failures_for_content = _failure("extraction-failed", error)
        elif not _palette_matches_key(result, cache_key):
            failures_for_content = _failure("extraction-failed", "palette does not match cache key")
        else:
            computed[cache_key] = result
            resolution.extracted += 1
            return
        for duplicate in unique[cache_key]:
            failures[duplicate.caller_key] = dict(failures_for_content)

    resolution.submitted = len(misses)
    if len(misses) == 1:
        cache_key, request = misses[0]
        try:
            palette = extract_palette_bytes(request.payload, cache_key.color_count)
            accept_result(cache_key, palette)
        except Exception as error:  # one damaged image must not cancel the batch
            accept_result(cache_key, error=error)
    elif len(misses) > 1:
        workers = _resolve_workers(process_workers)
        batch_size = max(1, 2 * workers)
        with ProcessPoolExecutor(max_workers=workers) as executor:
            for offset in range(0, len(misses), batch_size):
                batch = misses[offset:offset + batch_size]
                futures = {
                    executor.submit(
                        extract_palette_bytes,
                        request.payload,
                        cache_key.color_count,
                    ): (cache_key, request)
                    for cache_key, request in batch
                }
                for future in as_completed(futures):
                    cache_key, request = futures[future]
                    try:
                        accept_result(cache_key, future.result())
                    except Exception as error:  # isolate worker failures by content
                        accept_result(cache_key, error=error)

    if computed:
        computed_rows = [(cache_key, palette) for cache_key, palette in computed.items()]
        if cache is not None:
            try:
                # This is the only palette write in a resolution batch and is
                # deliberately executed after all worker futures are complete.
                cache.upsert_image_palettes(computed_rows)
            except Exception:
                resolution.cache_write_failed = True
        for cache_key, palette in computed.items():
            for request in unique[cache_key]:
                palettes[request.caller_key] = palette

    for key, failure in failures.items():
        resolution.failures[key] = failure
    resolution.palettes = _ordered_mapping(palettes)
    resolution.failures = _ordered_mapping(resolution.failures)
    return resolution


def _valid_game_asset_id(value: Any) -> str | None:
    candidate = str(value or "")
    return candidate if _GAME_ASSET_RE.fullmatch(candidate) else None


def page_palette_driver(page: Any) -> str | None:
    """Return a page's first game artwork palette driver."""

    if isinstance(page, Mapping):
        candidates = page.get("asset_ids")
        if isinstance(candidates, list):
            return next((value for value in (_valid_game_asset_id(item) for item in candidates) if value), None)
        subject = page.get("subject")
        if isinstance(subject, Mapping):
            return _valid_game_asset_id(subject.get("asset_id"))
    return _valid_game_asset_id(getattr(page, "primary_asset_id", None))


def page_palette_drivers(deck_plan: Any) -> set[str]:
    """Return de-duplicated game artwork drivers from the current deck."""

    if isinstance(deck_plan, Mapping):
        pages = deck_plan.get("pages", [])
    else:
        pages = getattr(deck_plan, "pages", ())
    if not isinstance(pages, (list, tuple)):
        raise TypeError("page_palette_drivers expects a deck object")
    drivers: set[str] = set()
    for page in pages:
        if isinstance(page, Mapping):
            found: list[str] = []
            def visit(value: Any) -> None:
                if isinstance(value, Mapping):
                    asset_id = _valid_game_asset_id(value.get("asset_id"))
                    if asset_id:
                        found.append(asset_id)
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)
            visit(page)
            drivers.update(found)
        elif (driver := page_palette_driver(page)) is not None:
            drivers.add(driver)
    return drivers


__all__ = [
    "PaletteCacheKey",
    "PaletteResolution",
    "page_palette_driver",
    "page_palette_drivers",
    "resolve_image_palettes",
]
