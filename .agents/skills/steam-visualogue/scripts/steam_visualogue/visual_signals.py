from __future__ import annotations

import math
import random
import time
import urllib.parse
import hashlib
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Callable

from .assets import (
    ARTWORK_CACHE_TTL,
    ARTWORK_HOST_INTERVAL,
    _download_artwork,
    _mime_type,
    _safe_artwork_url,
)
from .cache_db import CacheDB
from .fingerprint import compute_evidence_fingerprint, compute_visual_fingerprint
from .palette import DEFAULT_COLOR_COUNT, aggregate_palettes
from .palette_jobs import PaletteResolution, resolve_image_palettes
from .rate_limit import RateLimiter


MAX_LIBRARY_PALETTE_GAMES = 64
TOP_PLAYTIME_GAMES = 32
TAIL_STRATA = 32
LIBRARY_SAMPLE_STRATEGY = "top32-stratified-tail32"


@dataclass(frozen=True)
class SamplePlan:
    """The complete, deterministic plan for one report-level sample."""

    samples: tuple[dict[str, Any], ...]
    eligible_games: int
    total_playtime: float
    total_title_weight: float
    total_lived_weight: float
    strategy: str = LIBRARY_SAMPLE_STRATEGY

    @property
    def selected_games(self) -> int:
        return len(self.samples)

    def __len__(self) -> int:
        return len(self.samples)

    def __iter__(self):
        return iter(self.samples)

    def __getitem__(self, key: str) -> Any:
        values = {
            "samples": self.samples,
            "eligible_games": self.eligible_games,
            "selected_games": self.selected_games,
            "total_playtime": self.total_playtime,
            "total_title_weight": self.total_title_weight,
            "total_lived_weight": self.total_lived_weight,
            "strategy": self.strategy,
        }
        if key not in values:
            raise KeyError(key)
        return values[key]


def _playtime(value: Any) -> float:
    try:
        result = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _eligible_games(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    games = profile.get("games", []) if isinstance(profile, Mapping) else []
    candidates: list[dict[str, Any]] = []
    if not isinstance(games, list):
        return candidates
    for game in games:
        if not isinstance(game, Mapping) or not game.get("appid"):
            continue
        minutes = _playtime(game.get("playtime_minutes"))
        if minutes <= 0:
            continue
        try:
            appid = int(game["appid"])
        except (TypeError, ValueError):
            continue
        if appid <= 0:
            continue
        candidates.append({**dict(game), "appid": appid, "playtime_minutes": minutes})
    return sorted(candidates, key=lambda game: (-game["playtime_minutes"], game["appid"]))


def _sample_record(
    game: Mapping[str, Any],
    *,
    selection_role: str,
    title_weight: float,
    lived_weight: float,
    stratum_index: int | None = None,
    stratum_size: int = 1,
) -> dict[str, Any]:
    return {
        "appid": int(game["appid"]),
        "game": dict(game),
        "playtime_minutes": float(game["playtime_minutes"]),
        "artwork_url": str(game.get("artwork_url") or ""),
        "selection_role": selection_role,
        "title_weight": float(title_weight),
        "lived_weight": float(lived_weight),
        "stratum_index": stratum_index,
        "stratum_size": int(stratum_size),
    }


def select_library_palette_sample(profile: dict[str, Any]) -> SamplePlan:
    """Select at most 64 played games without external or random state."""

    candidates = _eligible_games(profile)
    eligible_count = len(candidates)
    total_playtime = sum(game["playtime_minutes"] for game in candidates)
    total_lived_weight = sum(math.sqrt(game["playtime_minutes"]) for game in candidates)
    if eligible_count <= MAX_LIBRARY_PALETTE_GAMES:
        samples = tuple(
            _sample_record(
                game,
                selection_role="complete",
                title_weight=1.0,
                lived_weight=math.sqrt(game["playtime_minutes"]),
            )
            for game in candidates
        )
    else:
        top = candidates[:TOP_PLAYTIME_GAMES]
        tail = candidates[TOP_PLAYTIME_GAMES:]
        base, remainder = divmod(len(tail), TAIL_STRATA)
        selected = [
            _sample_record(
                game,
                selection_role="top",
                title_weight=1.0,
                lived_weight=math.sqrt(game["playtime_minutes"]),
            )
            for game in top
        ]
        cursor = 0
        for stratum_index in range(TAIL_STRATA):
            size = base + (1 if stratum_index < remainder else 0)
            members = tail[cursor:cursor + size]
            cursor += size
            if not members:
                continue
            representative = members[(size - 1) // 2]
            selected.append(
                _sample_record(
                    representative,
                    selection_role="tail",
                    title_weight=float(size),
                    lived_weight=sum(math.sqrt(item["playtime_minutes"]) for item in members),
                    stratum_index=stratum_index,
                    stratum_size=size,
                )
            )
        samples = tuple(selected)
    return SamplePlan(
        samples=samples,
        eligible_games=eligible_count,
        total_playtime=total_playtime,
        total_title_weight=float(eligible_count),
        total_lived_weight=total_lived_weight,
    )


def _download_artwork_only(
    url: str,
    *,
    opener: Callable[..., Any],
    sleeper: Callable[[float], None],
    rate_limiter: RateLimiter,
    randomizer: Callable[[], float],
) -> tuple[bytes, str | None]:
    """Download one image; palette extraction intentionally does not happen here."""

    return _download_artwork(
        url,
        opener=opener,
        sleeper=sleeper,
        rate_limiter=rate_limiter,
        randomizer=randomizer,
    )


def _verified_artwork(shared: Mapping[str, Any] | None) -> tuple[bytes, str] | None:
    if not isinstance(shared, Mapping):
        return None
    try:
        payload = bytes(shared.get("payload") or b"")
    except (TypeError, ValueError):
        return None
    if not payload:
        return None
    digest = hashlib.sha256(payload).hexdigest()
    return (payload, digest) if shared.get("content_sha256") == digest else None


def _round_weight(value: float) -> float:
    return round(float(value), 6)


def _sampling_summary(plan: SamplePlan, successful_appids: set[int]) -> dict[str, Any]:
    successful_title = sum(
        float(sample["title_weight"])
        for sample in plan.samples
        if int(sample["appid"]) in successful_appids
    )
    successful_lived = sum(
        float(sample["lived_weight"])
        for sample in plan.samples
        if int(sample["appid"]) in successful_appids
    )
    title_coverage = successful_title / plan.total_title_weight if plan.total_title_weight else 0.0
    lived_coverage = successful_lived / plan.total_lived_weight if plan.total_lived_weight else 0.0
    selected_playtime = sum(float(sample["playtime_minutes"]) for sample in plan.samples)
    return {
        "strategy": plan.strategy,
        "eligible_games": plan.eligible_games,
        "selected_games": plan.selected_games,
        "successful_games": len(successful_appids),
        "sample_fraction": round(
            plan.selected_games / plan.eligible_games if plan.eligible_games else 0.0,
            6,
        ),
        "selected_playtime_fraction": round(
            selected_playtime / plan.total_playtime if plan.total_playtime else 0.0,
            6,
        ),
        "representation_coverage": {
            "titles": round(max(0.0, min(1.0, title_coverage)), 6),
            "lived_weight": round(max(0.0, min(1.0, lived_coverage)), 6),
        },
    }


def _confidence(sampling: Mapping[str, Any]) -> str:
    if int(sampling.get("successful_games") or 0) <= 0:
        return "low"
    coverage = sampling.get("representation_coverage")
    if not isinstance(coverage, Mapping):
        return "low"
    score = min(float(coverage.get("titles") or 0), float(coverage.get("lived_weight") or 0))
    return "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"


def build_library_palette_signals(
    profile: dict[str, Any],
    cache: CacheDB,
    *,
    force_artwork: bool = False,
    force_palette: bool = False,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
    max_workers: int = 4,
    process_workers: int | None = None,
    rate_limiter: RateLimiter | None = None,
    randomizer: Callable[[], float] = random.random,
    host_interval: float = ARTWORK_HOST_INTERVAL,
    progress: Callable[[str, int | None, int | None], None] | None = None,
    work_summary: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build report-level sampled visual signals from a profile."""
    plan = select_library_palette_sample(profile)
    total = plan.selected_games
    completed = 0
    images: list[dict[str, Any]] = []
    image_by_appid: dict[int, dict[str, Any]] = {}
    pending: list[tuple[dict[str, Any], str, Mapping[str, Any] | None]] = []
    failures: list[dict[str, Any]] = []
    work = work_summary if work_summary is not None else {}
    workers = max(1, min(4, int(max_workers)))

    def count(name: str, amount: int = 1) -> None:
        work[name] = int(work.get(name, 0)) + amount

    def complete_one() -> None:
        nonlocal completed
        completed += 1
        if progress is not None:
            progress("Resolving sampled library artwork", completed, total)

    if progress is not None:
        progress("Resolving sampled library artwork", 0, total)
    for sample in plan.samples:
        appid = int(sample["appid"])
        url = str(sample.get("artwork_url") or "")
        if not url:
            failures.append({"appid": appid, "status": "unavailable"})
            continue
        if not _safe_artwork_url(url):
            failures.append({"appid": appid, "status": "rejected-url"})
            continue
        source_url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        shared = cache.get_steam_artwork(
            source_url_hash,
            max_age_seconds=ARTWORK_CACHE_TTL,
            allow_stale=True,
        )
        verified = _verified_artwork(shared)
        if (
            not force_artwork
            and verified is not None
            and isinstance(shared, Mapping)
            and shared.get("cache_status") == "cached"
        ):
            payload, digest = verified
            image = {"key": appid, "content_sha256": digest, "payload": payload}
            images.append(image)
            image_by_appid[appid] = image
            count("artwork_cache_hits")
            continue
        pending.append((sample, url, shared if verified is not None else None))

    if rate_limiter is None:
        hosts = {
            urllib.parse.urlsplit(url).hostname or ""
            for _, url, _ in pending
        }
        rate_limiter = RateLimiter(
            {host: max(0.0, float(host_interval)) for host in hosts if host},
            sleeper=sleeper,
        )

    if pending:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _download_artwork_only,
                    url,
                    opener=opener,
                    sleeper=sleeper,
                    rate_limiter=rate_limiter,
                    randomizer=randomizer,
                ): (sample, url, stale_shared)
                for sample, url, stale_shared in pending
            }
            for future in as_completed(futures):
                sample, url, stale_shared = futures[future]
                appid = int(sample["appid"])
                try:
                    payload, content_type = future.result()
                    digest = hashlib.sha256(payload).hexdigest()
                    try:
                        stored = cache.upsert_steam_artwork(
                            hashlib.sha256(url.encode("utf-8")).hexdigest(),
                            payload,
                            _mime_type(content_type, url),
                        )
                        stored_digest = str(stored.get("content_sha256") or "")
                        if stored_digest == digest:
                            digest = stored_digest
                    except Exception:
                        pass
                    image = {"key": appid, "content_sha256": digest, "payload": payload}
                    images.append(image)
                    image_by_appid[appid] = image
                    count("downloads")
                except Exception as error:
                    stale = _verified_artwork(stale_shared)
                    if stale is not None:
                        payload, digest = stale
                        image = {"key": appid, "content_sha256": digest, "payload": payload}
                        images.append(image)
                        image_by_appid[appid] = image
                        failures.append({"appid": appid, "status": "cached-stale"})
                        count("stale_fallbacks")
                    else:
                        failures.append(
                            {"appid": appid, "status": "unavailable", "error": type(error).__name__}
                        )

    images.sort(key=lambda item: int(item["key"]))
    resolution: PaletteResolution = resolve_image_palettes(
        images,
        cache,
        force_palette=force_palette,
        process_workers=process_workers,
    )
    count("palette_cache_hits", resolution.cache_hits)
    count("palette_submissions", resolution.submitted)
    count("palette_extractions", resolution.extracted)
    palette_by_appid: dict[int, dict[str, Any]] = {
        int(key): palette
        for key, palette in resolution.palettes.items()
        if isinstance(key, int)
    }
    for key, failure in resolution.failures.items():
        try:
            appid = int(key)
        except (TypeError, ValueError):
            continue
        failures.append({"appid": appid, **failure})
        if failure.get("status") == "extraction-failed":
            count("extraction_failures")
    for _ in plan.samples:
        complete_one()
    successful_samples = [
        sample for sample in plan.samples if int(sample["appid"]) in palette_by_appid
    ]
    if progress is not None:
        progress("Aggregating sampled visual palettes", total, total)
    available_palettes = [palette_by_appid[int(sample["appid"])] for sample in successful_samples]
    library_palette = aggregate_palettes(
        available_palettes,
        [float(sample["lived_weight"]) for sample in successful_samples],
        colors=DEFAULT_COLOR_COUNT,
    )
    breadth_palette = aggregate_palettes(
        available_palettes,
        [float(sample["title_weight"]) for sample in successful_samples],
        colors=DEFAULT_COLOR_COUNT,
    )
    sampling = _sampling_summary(plan, set(palette_by_appid))
    confidence = _confidence(sampling)
    sources = [
        {
            "appid": int(sample["appid"]),
            "selection_role": str(sample["selection_role"]),
            "content_sha256": image_by_appid[int(sample["appid"])]["content_sha256"],
            "title_weight": _round_weight(sample["title_weight"]),
            "lived_weight": _round_weight(sample["lived_weight"]),
        }
        for sample in successful_samples
        if int(sample["appid"]) in image_by_appid
    ]
    failures = sorted(
        failures,
        key=lambda item: (int(item.get("appid") or 0), str(item.get("status") or "")),
    )
    evidence_fingerprint = profile.get("evidence_fingerprint") or compute_evidence_fingerprint(profile)
    result = {
        "run_id": profile.get("run_id"),
        "evidence_fingerprint": evidence_fingerprint,
        "library_palette": library_palette,
        "breadth_palette": breadth_palette,
        "sampling": sampling,
        "sources": sources,
        "failures": failures,
        "source": "sampled_steam_artwork",
        "confidence": confidence,
    }
    result["visual_fingerprint"] = compute_visual_fingerprint(result)
    return result


__all__ = [
    "LIBRARY_SAMPLE_STRATEGY",
    "MAX_LIBRARY_PALETTE_GAMES",
    "SamplePlan",
    "TAIL_STRATA",
    "TOP_PLAYTIME_GAMES",
    "build_library_palette_signals",
    "select_library_palette_sample",
]
