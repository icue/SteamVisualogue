from __future__ import annotations

import hashlib
import io
import mimetypes
import random
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from .context_budget import sha256_path_hex
from .fingerprint import image_pixel_sha256
from .io_utils import read_json, write_json
from .palette_jobs import page_palette_drivers, resolve_image_palettes
from .rate_limit import RateLimiter, jittered_backoff


MAX_ARTWORK_BYTES = 20 * 1024 * 1024
MAX_GENERATED_PIXELS = 40_000_000
ARTWORK_CACHE_TTL = 90 * 24 * 60 * 60
ARTWORK_HOST_INTERVAL = 0.500
STEAM_ARTWORK_VARIANTS = frozenset({"header", "portrait"})
STEAM_PORTRAIT_ARTWORK_FILENAME = "library_600x900.jpg"
GENERATED_ASSET_ID = re.compile(r"^generated:sha256:[0-9a-f]{64}$")
GENERATED_ASSET_KINDS = {
    "abstract-background",
    "abstract-motif",
    "isolated-shape",
    "single-illustration",
}
ALLOWED_HOST_SUFFIXES = (
    ".steamstatic.com",
    ".akamaihd.net",
    ".steamusercontent.com",
    ".steamcommunity.com",
)


def _selected_asset_ids(deck_plan: dict[str, Any] | Any) -> set[str]:
    """Collect asset IDs from the current deck plan or an explicit shortlist."""

    if hasattr(deck_plan, "asset_ids"):
        return {str(value) for value in getattr(deck_plan, "asset_ids", ()) if str(value)}
    if not isinstance(deck_plan, dict):
        raise TypeError("deck plan must be an object")
    if isinstance(deck_plan.get("shortlist"), list):
        return {str(value) for value in deck_plan["shortlist"] if isinstance(value, str) and value}

    found: set[str] = set()
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("asset_id", "raw_visual_asset"):
                asset_id = value.get(key)
                if isinstance(asset_id, str) and asset_id:
                    found.add(asset_id)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(deck_plan)
    return found


def _steam_artwork_url(game: dict[str, Any] | None, appid: str, variant: str) -> str:
    """Resolve Steam game artwork, including the vertical library portrait."""

    if variant not in STEAM_ARTWORK_VARIANTS:
        return ""
    if variant == "portrait":
        return (
            "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/"
            f"{appid}/{STEAM_PORTRAIT_ARTWORK_FILENAME}"
        )
    if variant == "header":
        return str(
            (game or {}).get("artwork_url")
            or (game or {}).get("metadata", {}).get("header_image")
            or ""
        )
    return ""


def _steam_achievement_url(
    game: dict[str, Any] | None,
    api_name: str,
    state: str,
) -> str:
    achievements = (game or {}).get("achievements", {})
    items = achievements.get("items", []) if isinstance(achievements, dict) else []
    if not isinstance(items, list):
        return ""
    field = "icon_url" if state == "unlocked" else "icon_gray_url"
    for item in items:
        if isinstance(item, dict) and str(item.get("api_name") or "") == api_name:
            return str(item.get(field) or "")
    return ""


def _steam_asset_url(game: dict[str, Any] | None, asset_id: str) -> str:
    parts = asset_id.split(":", 3)
    if len(parts) >= 3 and parts[0] == "game":
        return _steam_artwork_url(game, parts[1], parts[2])
    if len(parts) == 4 and parts[0] == "achievement":
        return _steam_achievement_url(game, parts[2], parts[3])
    return ""


def _image_geometry(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as image:
            transposed = ImageOps.exif_transpose(image)
            width, height = transposed.size
        if width > 0 and height > 0:
            return {
                "width": width,
                "height": height,
                "aspect_ratio": round(width / height, 6),
            }
    except (ImportError, OSError, ValueError):
        pass
    return {}


def _manifest_candidate(destination: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    raw = Path(relative)
    if raw.is_absolute():
        return None
    root = destination.resolve()
    candidate = (destination / raw).resolve()
    return candidate if candidate != root and candidate.is_relative_to(root) else None


def _load_asset_records(destination: Path) -> dict[str, Any]:
    manifest_path = destination / "manifest.json"
    try:
        payload = read_json(manifest_path) if manifest_path.is_file() else {}
    except (OSError, ValueError, AttributeError):
        return {}
    assets = payload.get("assets", {}) if isinstance(payload, dict) else {}
    return assets if isinstance(assets, dict) else {}


def _without_palette(record: dict[str, Any]) -> dict[str, Any]:
    """Remove palette fields before the current page drivers are resolved."""

    cleaned = dict(record)
    for field in ("palette", "palette_status", "palette_role"):
        cleaned.pop(field, None)
    return cleaned


def _validated_generated_record(
    asset_id: str,
    record: Any,
    destination: Path,
) -> dict[str, Any] | None:
    if not GENERATED_ASSET_ID.fullmatch(asset_id) or not isinstance(record, dict):
        return None
    if record.get("status") != "ready" or record.get("source") != "generated-raw":
        return None
    if record.get("pixel_sha256") != asset_id.rsplit(":", 1)[-1]:
        return None
    candidate = _manifest_candidate(destination, record.get("path"))
    expected = record.get("sha256")
    if candidate is None or not candidate.is_file() or not isinstance(expected, str):
        return None
    try:
        if sha256_path_hex(candidate) != expected:
            return None
        if image_pixel_sha256(candidate) != asset_id.rsplit(":", 1)[-1]:
            return None
    except OSError:
        return None
    return dict(record)


def register_generated_asset(
    source_path: str | Path,
    assets_dir: str | Path,
    review: dict[str, Any],
) -> dict[str, Any]:
    """Normalize and register one reviewed, already-generated local raster image."""
    source = Path(source_path)
    if not source.is_file():
        raise ValueError("Generated asset source must be a local file")
    return _register_generated_asset_source(source, source.stat().st_size, assets_dir, review)


def register_generated_asset_bytes(
    payload: bytes,
    assets_dir: str | Path,
    review: dict[str, Any],
) -> dict[str, Any]:
    """Normalize and register reviewed generated bytes restored from private cache."""
    raw = bytes(payload)
    return _register_generated_asset_source(io.BytesIO(raw), len(raw), assets_dir, review)


def _register_generated_asset_source(
    source: Any,
    source_size: int,
    assets_dir: str | Path,
    review: dict[str, Any],
) -> dict[str, Any]:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - dependency failure is environment-specific
        raise RuntimeError("Generated asset registration requires Pillow.") from exc

    if not isinstance(review, dict):
        raise ValueError("Generated asset review must be an object")
    required_review = {
        "approved": True,
        "no_text": True,
        "no_numbers": True,
        "no_logos_or_ui": True,
        "no_personal_identifiers": True,
        "no_reconstructed_game_art": True,
        "not_factual_evidence": True,
        "not_final_page": True,
    }
    kind = str(review.get("kind") or "")
    if kind not in GENERATED_ASSET_KINDS:
        raise ValueError("Generated asset review has an unsupported kind")
    failed = [name for name, expected in required_review.items() if review.get(name) is not expected]
    if failed:
        raise ValueError("Generated asset review is incomplete: " + ", ".join(sorted(failed)))

    if source_size > MAX_ARTWORK_BYTES:
        raise ValueError("Generated asset exceeds the 20 MiB safety limit")

    try:
        with Image.open(source) as opened:
            if str(opened.format or "").upper() not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Generated asset must be PNG, JPEG, or WebP")
            if int(getattr(opened, "n_frames", 1)) != 1:
                raise ValueError("Generated asset must contain exactly one frame")
            width, height = opened.size
            if width < 64 or height < 64 or width * height > MAX_GENERATED_PIXELS:
                raise ValueError("Generated asset dimensions are outside the supported range")
            opened.load()
            transposed = ImageOps.exif_transpose(opened)
            has_alpha = "A" in transposed.getbands() or "transparency" in opened.info
            normalized = transposed.convert("RGBA" if has_alpha else "RGB")
    except ValueError:
        raise
    except (OSError, SyntaxError) as exc:
        raise ValueError("Generated asset is not a readable raster image") from exc

    pixel_digest = hashlib.sha256()
    pixel_digest.update(f"{normalized.mode}:{normalized.width}x{normalized.height}\0".encode("ascii"))
    pixel_digest.update(normalized.tobytes())
    pixel_sha256 = pixel_digest.hexdigest()
    asset_id = f"generated:sha256:{pixel_sha256}"

    buffer = io.BytesIO()
    normalized.save(buffer, format="PNG", compress_level=9)
    payload = buffer.getvalue()
    file_sha256 = hashlib.sha256(payload).hexdigest()

    destination = Path(assets_dir)
    generated_dir = destination / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    generated_root = generated_dir.resolve()
    if not generated_root.is_relative_to(destination_root):
        raise ValueError("Generated asset directory escapes the assets directory")
    relative_path = f"generated/{pixel_sha256}.png"
    output_path = (destination / Path(relative_path)).resolve()
    if not output_path.is_relative_to(destination_root):
        raise ValueError("Generated asset path escapes the assets directory")
    if not output_path.is_file() or sha256_path_hex(output_path) != file_sha256:
        output_path.write_bytes(payload)

    record: dict[str, Any] = {
        "status": "ready",
        "source": "generated-raw",
        "kind": kind,
        "path": relative_path,
        "mime_type": "image/png",
        "sha256": file_sha256,
        "pixel_sha256": pixel_sha256,
        "width": normalized.width,
        "height": normalized.height,
        "mode": normalized.mode,
        "has_alpha": has_alpha,
        "metadata_stripped": True,
        "review": required_review,
    }
    previous = _load_asset_records(destination)
    previous[asset_id] = record
    write_json(destination / "manifest.json", {"assets": previous})
    return {"asset_id": asset_id, **record}


def _safe_artwork_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(hostname.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def _extension(content_type: str | None, url: str) -> str:
    if content_type:
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime in {"image/jpeg", "image/png", "image/webp"}:
            return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]
    guessed = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return guessed if guessed in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def _mime_type(content_type: str | None, url: str) -> str:
    if content_type:
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime in {"image/jpeg", "image/png", "image/webp"}:
            return mime
    guessed = mimetypes.guess_type(urllib.parse.urlparse(url).path)[0]
    return guessed if guessed in {"image/jpeg", "image/png", "image/webp"} else "image/jpeg"


def _download_artwork(
    url: str,
    *,
    opener: Callable[..., Any],
    sleeper: Callable[[float], None],
    rate_limiter: RateLimiter,
    randomizer: Callable[[], float],
) -> tuple[bytes, str | None]:
    """Download one Steam artwork with conservative pacing and bounded retries."""
    request = urllib.request.Request(url, headers={"User-Agent": "SteamVisualogue/1.0"})
    for attempt in range(6):
        rate_limiter.wait(url)
        try:
            with opener(request, timeout=30) as response:
                status = int(getattr(response, "status", 200))
                if status == 429 or 500 <= status <= 599:
                    raise HTTPError(
                        url,
                        status,
                        "temporary artwork failure",
                        getattr(response, "headers", {}),
                        None,
                    )
                content_type = response.headers.get("Content-Type")
                if content_type and not content_type.lower().startswith("image/"):
                    raise ValueError("Artwork response is not an image")
                payload = response.read(MAX_ARTWORK_BYTES + 1)
            if len(payload) > MAX_ARTWORK_BYTES:
                raise ValueError("Artwork exceeds the 20 MiB safety limit")
            return payload, content_type
        except HTTPError as error:
            if error.code != 429 and not 500 <= error.code <= 599:
                error.close()
                raise ValueError(f"Artwork returned HTTP {error.code}") from None
            if attempt >= 5:
                error.close()
                raise ValueError("Artwork is temporarily unavailable") from None
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                delay = float(retry_after) if retry_after is not None else jittered_backoff(
                    attempt, randomizer=randomizer
                )
            except (TypeError, ValueError):
                delay = jittered_backoff(attempt, randomizer=randomizer)
            delay = max(0.0, delay)
            if error.code == 429:
                rate_limiter.defer(url, delay)
            else:
                sleeper(delay)
            error.close()
        except (URLError, OSError, TimeoutError, ConnectionError):
            if attempt >= 5:
                raise ValueError("Artwork is temporarily unavailable") from None
            sleeper(jittered_backoff(attempt, randomizer=randomizer))
    raise ValueError("Artwork is temporarily unavailable")


def _artwork_hosts(requested: set[str], games: dict[int, dict[str, Any]]) -> set[str]:
    hosts: set[str] = set()
    for asset_id in requested:
        parts = asset_id.split(":", 3)
        if len(parts) < 3 or parts[0] not in {"game", "achievement"} or not parts[1].isdigit():
            continue
        game = games.get(int(parts[1]))
        url = _steam_asset_url(game, asset_id)
        if _safe_artwork_url(url):
            host = urllib.parse.urlparse(url).hostname
            if host:
                hosts.add(host.lower())
    return hosts


def materialize_selected_assets(
    profile: dict[str, Any],
    deck_plan: dict[str, Any],
    assets_dir: str | Path,
    *,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rate_limiter: RateLimiter | None = None,
    randomizer: Callable[[], float] = random.random,
    host_interval: float = ARTWORK_HOST_INTERVAL,
    prune_generated: bool = False,
    cache: Any | None = None,
    force_artwork: bool = False,
    force_palette: bool = False,
    progress: Callable[[str, int | None, int | None], None] | None = None,
) -> dict[str, Any]:
    """Materialize selected Steam artwork and retain verified generated assets."""
    # The compiler owns formal deck validation.  Asset materialization only
    # consumes its current asset projection or an explicit shortlist.
    if not isinstance(deck_plan, dict) and not hasattr(deck_plan, "asset_ids"):
        raise TypeError("deck plan must be an object")
    destination = Path(assets_dir)
    destination.mkdir(parents=True, exist_ok=True)
    requested = _selected_asset_ids(deck_plan)
    has_opening = any(isinstance(p, dict) and p.get("presentation", {}).get("kind") == "opening" for p in (deck_plan.get("pages", []) if isinstance(deck_plan, dict) else []))
    has_closing = any(isinstance(p, dict) and p.get("presentation", {}).get("kind") == "closing" for p in (deck_plan.get("pages", []) if isinstance(deck_plan, dict) else []))
    candidate_appids: list[int] = []
    if has_opening or has_closing:
        from .ribbon_composer import select_ribbon_candidate_pool

        candidate_appids = select_ribbon_candidate_pool(profile, pool_size=40)
        for aid in candidate_appids:
            requested.add(f"game:{aid}:portrait")

    ordered_requested = sorted(requested)
    total = len(ordered_requested)
    if progress is not None:
        progress("Materializing selected assets", 0, total)
    games = {int(game["appid"]): game for game in profile.get("games", []) if game.get("appid")}
    open_url = opener or urllib.request.urlopen
    if rate_limiter is None:
        rate_limiter = RateLimiter(
            {
                host: max(0.0, float(host_interval))
                for host in _artwork_hosts(requested, games)
            },
            sleeper=sleeper,
        )
    previous = _load_asset_records(destination)
    manifest: dict[str, Any] = {"assets": {}}
    if not prune_generated:
        for previous_id, previous_record in previous.items():
            valid = _validated_generated_record(str(previous_id), previous_record, destination)
            if valid is not None:
                manifest["assets"][str(previous_id)] = _without_palette(valid)
    manifest_path = destination / "manifest.json"

    for index, asset_id in enumerate(ordered_requested):
        if progress is not None and index > 0:
            progress("Materializing selected assets", index, total)
        if GENERATED_ASSET_ID.fullmatch(asset_id):
            prior = previous.get(asset_id) if isinstance(previous, dict) else None
            validated = _validated_generated_record(asset_id, prior, destination)
            manifest["assets"][asset_id] = _without_palette(validated) if validated else {
                "status": "unavailable",
                "source": "generated-raw",
                "error": "integrity-or-registration-failed",
            }
            continue
        parts = asset_id.split(":", 3)
        if len(parts) < 3 or parts[0] not in {"game", "achievement"} or not parts[1].isdigit():
            manifest["assets"][asset_id] = {"status": "unsupported-id"}
            continue
        game = games.get(int(parts[1]))
        is_achievement = parts[0] == "achievement" and len(parts) == 4
        variant = parts[3] if is_achievement else parts[2]
        url = _steam_asset_url(game, asset_id)
        if not game or not url:
            manifest["assets"][asset_id] = {"status": "unavailable"}
            continue
        if not _safe_artwork_url(url):
            manifest["assets"][asset_id] = {"status": "rejected-url"}
            continue
        source_url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        prior = previous.get(asset_id) if isinstance(previous, dict) else None
        if not force_artwork and isinstance(prior, dict) and prior.get("status") == "ready":
            prior_path = prior.get("path")
            prior_candidate = (
                (destination / prior_path).resolve() if isinstance(prior_path, str) else None
            )
            if (
                isinstance(prior_path, str)
                and prior.get("source_url_hash") == source_url_hash
                and prior_candidate is not None
                and prior_candidate.is_relative_to(destination.resolve())
                and prior_candidate.is_file()
                and prior_candidate.stat().st_size <= MAX_ARTWORK_BYTES
                and isinstance(prior.get("sha256"), str)
                and sha256_path_hex(prior_candidate) == prior.get("sha256")
            ):
                reused = _without_palette(prior)
                reused["variant"] = variant
                reused.update(_image_geometry(prior_candidate))
                manifest["assets"][asset_id] = reused
                continue
        shared = None
        if cache is not None:
            shared = cache.get_steam_artwork(
                source_url_hash,
                max_age_seconds=ARTWORK_CACHE_TTL,
                allow_stale=True,
            )
        try:
            cache_status = "cached"
            if shared is not None and shared.get("cache_status") == "cached" and not force_artwork:
                payload = shared["payload"]
                content_type = shared["content_type"]
            else:
                try:
                    payload, content_type = _download_artwork(
                        url,
                        opener=open_url,
                        sleeper=sleeper,
                        rate_limiter=rate_limiter,
                        randomizer=randomizer,
                    )
                    cache_status = "network"
                    if cache is not None:
                        cache.upsert_steam_artwork(
                            source_url_hash,
                            payload,
                            _mime_type(content_type, url),
                            max_bytes=MAX_ARTWORK_BYTES,
                        )
                except Exception:
                    if shared is None:
                        raise
                    payload = shared["payload"]
                    content_type = shared["content_type"]
                    cache_status = "cached_stale"
            digest = hashlib.sha256(payload).hexdigest()
            prefix = "achievement" if is_achievement else "game"
            filename = f"{prefix}-{parts[1]}-{digest[:12]}{_extension(content_type, url)}"
            path = destination / filename
            if not path.is_file() or path.stat().st_size > MAX_ARTWORK_BYTES or sha256_path_hex(path) != digest:
                path.write_bytes(payload)
            record: dict[str, Any] = {
                "status": "ready",
                "path": filename,
                "sha256": digest,
                "source_url_hash": source_url_hash,
                "source": "steam-artwork",
                "cache_status": cache_status,
                "variant": variant,
            }
            if is_achievement:
                record.update(
                    {
                        "asset_kind": "achievement-icon",
                        "achievement_id": f"achievement:{parts[1]}:{parts[2]}",
                        "achievement_state": variant,
                    }
                )
            record.update(_image_geometry(path))
            manifest["assets"][asset_id] = record
        except Exception as error:
            manifest["assets"][asset_id] = {
                "status": "unavailable",
                "error": type(error).__name__,
            }

    if (has_opening or has_closing) and candidate_appids:
        try:
            from PIL import Image
            from .ribbon_composer import compose_dual_row_ribbon

            ready_imgs: list[Image.Image] = []
            for aid in candidate_appids:
                rec = manifest["assets"].get(f"game:{aid}:portrait")
                if isinstance(rec, dict) and rec.get("status") == "ready" and rec.get("path"):
                    cand = (destination / rec["path"]).resolve()
                    if cand.is_file():
                        try:
                            with Image.open(cand) as opened:
                                ready_imgs.append(opened.copy())
                        except Exception:
                            pass

            count_per_ribbon = min(12, len(ready_imgs) // 2 if len(ready_imgs) >= 12 else len(ready_imgs))
            opening_cards = ready_imgs[:count_per_ribbon]
            remaining_cards = ready_imgs[count_per_ribbon:count_per_ribbon * 2]
            closing_cards = remaining_cards if len(remaining_cards) >= count_per_ribbon else ready_imgs[count_per_ribbon:]
            if not closing_cards:
                closing_cards = opening_cards

            review = {
                "kind": "abstract-background",
                "approved": True,
                "no_text": True,
                "no_numbers": True,
                "no_logos_or_ui": True,
                "no_personal_identifiers": True,
                "no_reconstructed_game_art": True,
                "not_factual_evidence": True,
                "not_final_page": True,
            }

            if has_opening and opening_cards:
                ribbon = compose_dual_row_ribbon(opening_cards, angle=-5.5)
                buf = io.BytesIO()
                ribbon.save(buf, format="PNG", compress_level=9)
                reg = register_generated_asset_bytes(buf.getvalue(), destination, review)
                for existing_rec in manifest["assets"].values():
                    if isinstance(existing_rec, dict) and existing_rec.get("ribbon_role") == "opening":
                        existing_rec.pop("ribbon_role", None)
                reg["ribbon_role"] = "opening"
                manifest["opening_ribbon_asset_id"] = reg["asset_id"]
                manifest["assets"][reg["asset_id"]] = reg

            if has_closing and closing_cards:
                ribbon = compose_dual_row_ribbon(closing_cards, angle=5.0)
                buf = io.BytesIO()
                ribbon.save(buf, format="PNG", compress_level=9)
                reg = register_generated_asset_bytes(buf.getvalue(), destination, review)
                for existing_rec in manifest["assets"].values():
                    if isinstance(existing_rec, dict) and existing_rec.get("ribbon_role") == "closing":
                        existing_rec.pop("ribbon_role", None)
                reg["ribbon_role"] = "closing"
                manifest["closing_ribbon_asset_id"] = reg["asset_id"]
                manifest["assets"][reg["asset_id"]] = reg
        except Exception:
            pass

    drivers = page_palette_drivers(deck_plan)
    palette_inputs: list[dict[str, Any]] = []
    for asset_id in sorted(manifest["assets"]):
        record = manifest["assets"].get(asset_id)
        if not isinstance(record, dict):
            continue
        record = _without_palette(record)
        manifest["assets"][asset_id] = record
        if asset_id not in drivers or record.get("status") != "ready":
            continue
        candidate = _manifest_candidate(destination, record.get("path"))
        expected = record.get("sha256")
        if candidate is None or not candidate.is_file() or not isinstance(expected, str):
            record["palette_status"] = "unavailable"
            continue
        try:
            payload = candidate.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != expected:
                raise ValueError("asset bytes do not match the manifest hash")
        except (OSError, ValueError):
            record["palette_status"] = "unavailable"
            continue
        palette_inputs.append(
            {"key": asset_id, "content_sha256": digest, "payload": payload}
        )

    resolution = resolve_image_palettes(
        palette_inputs,
        cache,
        force_palette=force_palette,
    )
    for asset_id in sorted(drivers):
        record = manifest["assets"].get(asset_id)
        if not isinstance(record, dict):
            continue
        palette = resolution.palettes.get(asset_id)
        if palette is not None:
            record["palette"] = palette
            record["palette_role"] = "page-driver"
            record.pop("palette_status", None)
        else:
            record.pop("palette", None)
            record["palette_status"] = "unavailable"

    if progress is not None:
        progress("Finalizing the asset manifest", total, total)
    retained = {
        str(record.get("path"))
        for record in manifest["assets"].values()
        if isinstance(record, dict) and record.get("status") == "ready" and record.get("path")
    }
    root = destination.resolve()
    for record in previous.values() if isinstance(previous, dict) else []:
        relative = record.get("path") if isinstance(record, dict) else None
        if (
            not prune_generated
            and isinstance(record, dict)
            and record.get("source") == "generated-raw"
        ):
            continue
        if not isinstance(relative, str) or relative in retained:
            continue
        candidate = (destination / relative).resolve()
        if candidate != root and candidate.is_relative_to(root) and candidate.is_file():
            candidate.unlink()
    write_json(manifest_path, manifest)
    return manifest
