from __future__ import annotations

import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError


from PIL import Image  # noqa: E402

from steam_visualogue.cache_db import CacheDB  # noqa: E402
from steam_visualogue.assets import materialize_selected_assets  # noqa: E402
from steam_visualogue.visual_signals import build_library_palette_signals  # noqa: E402


def image_bytes(color: tuple[int, int, int], *, size: tuple[int, int] = (32, 32)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeResponse:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = {"Content-Type": "image/png"}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.payload


class VisualSignalTests(unittest.TestCase):
    def test_sampled_library_palette_caches_content_and_weights_playtime(self) -> None:
        urls = {
            "https://cdn.cloudflare.steamstatic.com/red.png": image_bytes((220, 20, 20)),
            "https://cdn.cloudflare.steamstatic.com/blue.png": image_bytes((20, 20, 220)),
            "https://cdn.cloudflare.steamstatic.com/unplayed.png": image_bytes((20, 220, 20)),
        }
        calls: list[str] = []
        lock = threading.Lock()

        def opener(request: object, timeout: int) -> FakeResponse:
            self.assertEqual(30, timeout)
            url = getattr(request, "full_url")
            with lock:
                calls.append(url)
            return FakeResponse(urls[url])

        profile = {
            "run_id": "palette-fixture",
            "games": [
                {"appid": 1, "playtime_minutes": 3600, "artwork_url": next(iter(urls))},
                {"appid": 2, "playtime_minutes": 100, "artwork_url": list(urls)[1]},
                {"appid": 3, "playtime_minutes": 0, "artwork_url": list(urls)[2]},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            cache = CacheDB(Path(temporary) / "cache.sqlite", clock=lambda: 200.0)
            for game in profile["games"]:
                cache.upsert_app_metadata(
                    game["appid"],
                    {"name": str(game["appid"]), "header_image_url": game["artwork_url"]},
                    fetched_at=100.0,
                )
            progress: list[tuple[int | None, int | None]] = []
            first = build_library_palette_signals(
                profile,
                cache,
                opener=opener,
                sleeper=lambda _: None,
                max_workers=4,
                progress=lambda _message, current, total: progress.append(
                    (current, total)
                ),
            )
            self.assertEqual(2, len(calls))
            self.assertNotIn(profile["games"][2]["artwork_url"], calls)
            self.assertEqual(2, first["sampling"]["eligible_games"])
            self.assertEqual(2, first["sampling"]["selected_games"])
            self.assertEqual(2, first["sampling"]["successful_games"])
            self.assertEqual("high", first["confidence"])
            self.assertEqual({"titles": 1.0, "lived_weight": 1.0}, first["sampling"]["representation_coverage"])
            self.assertEqual(2, len(first["breadth_palette"]["dominant_colors"]))
            self.assertEqual("#DC1414", first["library_palette"]["dominant_colors"][0]["hex"])
            self.assertEqual((0, 2), progress[0])
            self.assertEqual((2, 2), progress[-1])
            self.assertEqual(
                sorted(current for current, _ in progress if current is not None),
                [current for current, _ in progress if current is not None],
            )

            assets = materialize_selected_assets(
                profile,
                {"shortlist": ["game:1:header"]},
                Path(temporary) / "assets",
                cache=cache,
                opener=lambda *_args, **_kwargs: self.fail(
                    "palette artwork bytes should be reused by assets"
                ),
            )
            self.assertEqual("cached", assets["assets"]["game:1:header"]["cache_status"])

            second = build_library_palette_signals(
                profile,
                cache,
                opener=lambda *_args, **_kwargs: self.fail("warm palette scan should use cache"),
                sleeper=lambda _: None,
            )
            self.assertEqual(first["library_palette"], second["library_palette"])
            self.assertEqual(first["visual_fingerprint"], second["visual_fingerprint"])
            cache.close()

    def test_artwork_429_defers_the_shared_host_limiter(self) -> None:
        url = "https://cdn.cloudflare.steamstatic.com/limited.png"
        payload = image_bytes((40, 80, 120))
        calls = 0

        class RecordingLimiter:
            def __init__(self) -> None:
                self.waits: list[str] = []
                self.deferrals: list[tuple[str, float]] = []

            def wait(self, value: str) -> float:
                self.waits.append(value)
                return 0.0

            def defer(self, value: str, delay: float) -> float:
                self.deferrals.append((value, delay))
                return delay

        limiter = RecordingLimiter()

        def opener(request: object, timeout: int) -> FakeResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPError(url, 429, "limited", {"Retry-After": "2"}, None)
            return FakeResponse(payload)

        profile = {
            "run_id": "limited-fixture",
            "games": [{"appid": 1, "playtime_minutes": 10, "artwork_url": url}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            cache = CacheDB(Path(temporary) / "cache.sqlite", clock=lambda: 200.0)
            cache.upsert_app_metadata(
                1, {"name": "One", "header_image_url": url}, fetched_at=100.0
            )
            result = build_library_palette_signals(
                profile,
                cache,
                opener=opener,
                sleeper=lambda _: self.fail("429 should use the shared limiter cooldown"),
                rate_limiter=limiter,
                randomizer=lambda: 0.0,
                max_workers=4,
            )
            self.assertEqual(2, len(limiter.waits))
            self.assertEqual([(url, 2.0)], limiter.deferrals)
            self.assertEqual(1, result["sampling"]["successful_games"])
            cache.close()

    def test_portrait_variant_fetches_steam_library_artwork(self) -> None:
        expected_url = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/42/library_600x900.jpg"
        calls: list[str] = []

        def opener(request: object, timeout: int) -> FakeResponse:
            self.assertEqual(30, timeout)
            url = getattr(request, "full_url")
            calls.append(url)
            return FakeResponse(image_bytes((90, 120, 150), size=(600, 900)))

        profile = {"run_id": "portrait-fixture", "games": [{"appid": 42, "name": "Portrait game"}]}
        with tempfile.TemporaryDirectory() as temporary:
            result = materialize_selected_assets(
                profile,
                {"shortlist": ["game:42:portrait"]},
                Path(temporary) / "assets",
                opener=opener,
                sleeper=lambda _: None,
                host_interval=0,
            )
        record = result["assets"]["game:42:portrait"]
        self.assertEqual([expected_url], calls)
        self.assertEqual("portrait", record["variant"])
        self.assertEqual(600, record["width"])
        self.assertEqual(900, record["height"])
        self.assertEqual("ready", record["status"])


if __name__ == "__main__":
    unittest.main()
