from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image


from steam_visualogue.cache_db import CacheDB
from steam_visualogue.palette import extract_palette_bytes
from steam_visualogue.analytics import derive_signals
from steam_visualogue.rate_limit import DEFAULT_HOST_INTERVALS, RateLimiter, jittered_backoff
from steam_visualogue.steam_api import (
    HTTPResult,
    OwnedGamesUnavailable,
    ResolvedIdentity,
    SteamAPI,
    SteamAuthenticationError,
    SteamDataCollector,
    SteamRateLimitError,
    SteamRequestError,
    identity_key_hash,
    normalize_identity_key,
)
from tests.support import FakeTime, close_apis  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class QueueTransport:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> HTTPResult:
        self.urls.append(url)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class DataLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordination_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.coordination_temp.cleanup)
        self.coordination_path = Path(self.coordination_temp.name) / "coordination.sqlite"
        self.apis: list[SteamAPI] = []
        self.addCleanup(self._close_apis)

    def _track_api(self, api: SteamAPI) -> SteamAPI:
        self.apis.append(api)
        return api

    def _close_apis(self) -> None:
        close_apis(*self.apis)

    def test_default_host_intervals_favor_low_rate_limit_risk(self) -> None:
        self.assertEqual(DEFAULT_HOST_INTERVALS["api.steampowered.com"], 0.25)
        self.assertEqual(DEFAULT_HOST_INTERVALS["store.steampowered.com"], 1.0)

    def test_cache_round_trip_and_scoped_purge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheDB(Path(directory) / "cache.sqlite", clock=lambda: 100.0)
            cache.upsert_app_metadata(
                10,
                {
                    "name": "Ten", "genres": ["Action"], "developers": ["D"],
                    "publishers": ["P"], "platforms": {"windows": True},
                    "categories": ["Achievements"], "achievement_total": 1,
                    "header_image_url": "https://example/header.jpg",
                },
            )
            image = BytesIO()
            Image.new("RGB", (16, 16), "#000000").save(image, format="PNG")
            payload = image.getvalue()
            palette = extract_palette_bytes(payload)
            palette_key = (hashlib.sha256(payload).hexdigest(), palette["algorithm"], 5)
            cache.upsert_image_palettes([(palette_key, palette)])
            cache.replace_achievement_schema(
                10, [{"apiname": "FIRST", "display_name": "First", "hidden": False}]
            )
            cache.replace_achievement_global(
                10, [{"apiname": "FIRST", "global_percent": 12.5}]
            )
            cache.replace_user_games(
                "76561198000000000", [{"appid": 10, "name": "Ten", "playtime_forever": 50}]
            )
            cache.replace_user_achievements(
                "76561198000000000", 10,
                [{"apiname": "FIRST", "achieved": True, "unlocktime": 12}],
                playtime_forever=50,
            )
            cache.record_run("run", "76561198000000000", "Alias", 1)

            self.assertEqual(cache.get_app_metadata(10)["genres"], ["Action"])
            self.assertEqual(
                palette,
                cache.get_image_palettes([palette_key])[palette_key],
            )
            self.assertEqual(
                cache.get_achievement_schema(10)["achievements"][0]["display_name"], "First"
            )
            self.assertEqual(
                cache.get_achievement_global(10)["achievements"][0]["global_percent"], 12.5
            )
            self.assertTrue(
                cache.get_user_achievements("76561198000000000", 10)["achievements"][0]["achieved"]
            )
            self.assertEqual(cache.get_run_identity("run")["steamid"], "76561198000000000")

            self.assertGreater(cache.purge_user("76561198000000000"), 0)
            self.assertEqual(cache.get_user_games("76561198000000000"), {})
            self.assertIsNotNone(cache.get_app_metadata(10), "user purge must retain global cache")
            self.assertGreater(cache.purge_global(), 0)
            self.assertIsNone(cache.get_app_metadata(10))
            self.assertEqual({}, cache.get_image_palettes([palette_key]))
            cache.close()

    def test_semantic_cache_is_content_addressed_integrity_checked_and_user_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheDB(Path(directory) / "cache.sqlite", clock=lambda: 100.0)
            identity = "76561198000000000"
            other_identity = "76561198000000001"
            game_fingerprint = "sha256:" + "1" * 64
            contract_fingerprint = "sha256:" + "2" * 64
            payload = {
                "game_id": "game:10",
                "evidence_ids": ["achievement:10:FIRST"],
                "classifications": ["ordinary"],
            }
            cache.upsert_achievement_semantic_cache(
                identity,
                game_fingerprint,
                contract_fingerprint,
                "en-US",
                payload,
            )
            self.assertEqual(
                payload,
                cache.get_achievement_semantic_cache(
                    identity, game_fingerprint, contract_fingerprint, "en-US"
                ),
            )
            self.assertIsNone(
                cache.get_achievement_semantic_cache(
                    identity, "sha256:" + "3" * 64, contract_fingerprint, "en-US"
                )
            )
            self.assertIsNone(
                cache.get_achievement_semantic_cache(
                    identity, game_fingerprint, contract_fingerprint, "zh-CN"
                )
            )

            cache._connection.execute(
                "UPDATE achievement_semantic_cache SET payload_json = ?",
                ("{not valid json",),
            )
            cache._connection.commit()
            self.assertIsNone(
                cache.get_achievement_semantic_cache(
                    identity, game_fingerprint, contract_fingerprint, "en-US"
                )
            )
            self.assertIsNone(
                cache._connection.execute(
                    "SELECT 1 FROM achievement_semantic_cache WHERE identity_scope = ?",
                    (identity,),
                ).fetchone()
            )

            cache.upsert_achievement_semantic_cache(
                identity, game_fingerprint, contract_fingerprint, "en-US", payload
            )
            cache._connection.execute(
                "UPDATE achievement_semantic_cache SET payload_sha256 = ?",
                ("0" * 64,),
            )
            cache._connection.commit()
            self.assertIsNone(
                cache.get_achievement_semantic_cache(
                    identity, game_fingerprint, contract_fingerprint, "en-US"
                )
            )

            cache.upsert_achievement_semantic_cache(
                identity, game_fingerprint, contract_fingerprint, "en-US", payload
            )
            cache.upsert_achievement_semantic_cache(
                other_identity, game_fingerprint, contract_fingerprint, "en-US", payload
            )
            self.assertGreater(cache.purge_user(identity), 0)
            self.assertIsNone(
                cache.get_achievement_semantic_cache(
                    identity, game_fingerprint, contract_fingerprint, "en-US"
                )
            )
            self.assertEqual(
                payload,
                cache.get_achievement_semantic_cache(
                    other_identity, game_fingerprint, contract_fingerprint, "en-US"
                ),
            )
            cache.close()

    def test_image_palette_schema_replaces_app_palette_and_drops_corrupt_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE app_palette (appid INTEGER PRIMARY KEY, palette_json TEXT)"
            )
            connection.commit()
            connection.close()

            cache = CacheDB(path, clock=lambda: 100.0)
            tables = {
                row[0]
                for row in cache._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertNotIn("app_palette", tables)
            self.assertIn("image_palette", tables)
            self.assertEqual(0, cache._connection.execute("PRAGMA user_version").fetchone()[0])

            image = BytesIO()
            Image.new("RGB", (16, 16), "#224466").save(image, format="PNG")
            payload = image.getvalue()
            palette = extract_palette_bytes(payload)
            key = (hashlib.sha256(payload).hexdigest(), palette["algorithm"], 5)
            cache.upsert_image_palettes([(key, palette)])
            cache._connection.execute(
                "UPDATE image_palette SET palette_json = ?",
                ("{not valid json",),
            )
            cache._connection.commit()

            self.assertEqual({}, cache.get_image_palettes([key]))
            self.assertIsNone(
                cache._connection.execute(
                    "SELECT 1 FROM image_palette WHERE content_sha256 = ?",
                    (key[0],),
                ).fetchone()
            )
            cache.close()

    def test_identity_resolution_is_canonical_and_scoped(self) -> None:
        self.assertEqual(
            normalize_identity_key(" HTTPS://WWW.STEAMCOMMUNITY.COM/id/Friendly-Name/ "),
            "steamcommunity.com/id/friendly-name",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheDB(Path(directory) / "cache.sqlite")
            cache.upsert_identity_resolution(
                identity_key_hash("https://steamcommunity.com/id/Friendly-Name"),
                "76561198000000000",
                "Friendly",
                resolved_at=100,
            )
            cache.upsert_identity_resolution(
                identity_key_hash("https://steamcommunity.com/id/Other"),
                "76561198000000001",
                "Other",
                resolved_at=100,
            )
            self.assertEqual(
                cache.get_identity_resolution(
                    identity_key_hash("steamcommunity.com/id/friendly-name")
                )["player_alias"],
                "Friendly",
            )
            cache.purge_user("76561198000000000")
            self.assertIsNone(
                cache.get_identity_resolution(
                    identity_key_hash("steamcommunity.com/id/friendly-name")
                )
            )
            self.assertIsNotNone(
                cache.get_identity_resolution(identity_key_hash("steamcommunity.com/id/other"))
            )
            cache.close()

    def test_acquisition_snapshot_round_trip_and_atomic_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheDB(Path(directory) / "cache.sqlite", clock=lambda: 100.0)
            collection = {
                "player_alias": "Alias",
                "games": [{"appid": 10, "name": "Ten", "playtime_minutes": 2}],
                "data_status": {"owned_games": "ok"},
            }
            enrichment = {
                "games": [{"appid": 10, "name": "Ten Deluxe", "playtime_minutes": 2}],
                "data_status": {"enrichment": {"requested": 1}},
                "enriched_at": "1970-01-01T00:01:40Z",
            }
            cache.replace_collection_snapshot("user", "svdata-one", "Alias", 100, collection)
            cache.replace_enrichment_snapshot("user", "svdata-one", 100, enrichment)
            row = cache.get_collection_snapshot("user", now=100)
            self.assertEqual(row["collection_payload"], collection)
            self.assertEqual(row["enrichment_payload"], enrichment)
            cache.replace_collection_snapshot(
                "user", "svdata-two", "Alias", 100, {**collection, "games": []}
            )
            replaced = cache.get_acquisition_snapshot("user")
            self.assertEqual(replaced["snapshot_id"], "svdata-two")
            self.assertEqual(replaced["collection_payload"]["games"], [])
            self.assertIsNone(replaced["enrichment_payload"])
            cache.close()

    def test_snapshot_corruption_version_and_future_time_are_misses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite"
            cache = CacheDB(path, clock=lambda: 100.0)
            payload = {
                "player_alias": "Alias", "games": [], "data_status": {"owned_games": "ok"}
            }
            cache.replace_collection_snapshot("user", "svdata-one", "Alias", 100, payload)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE user_acquisition_snapshot SET collection_payload = ?",
                    (sqlite3.Binary(b"not-zlib"),),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertIsNone(cache.get_collection_snapshot("user", now=100))

            cache.replace_collection_snapshot("user", "svdata-two", "Alias", 101, payload)
            self.assertIsNone(cache.get_collection_snapshot("user", now=100))
            cache.close()

    def test_purge_user_removes_snapshot_but_purge_global_keeps_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheDB(Path(directory) / "cache.sqlite", clock=lambda: 100.0)
            payload = {
                "player_alias": "Alias", "games": [], "data_status": {"owned_games": "ok"}
            }
            cache.replace_collection_snapshot("user", "svdata-one", "Alias", 100, payload)
            cache.upsert_identity_resolution("a" * 64, "user", "Alias", resolved_at=100)
            cache.upsert_app_metadata(10, {"name": "Ten"}, fetched_at=100)
            cache.purge_global()
            self.assertIsNotNone(cache.get_acquisition_snapshot("user"))
            self.assertIsNone(cache.get_app_metadata(10))
            cache.purge_user("user")
            self.assertIsNone(cache.get_acquisition_snapshot("user"))
            self.assertIsNone(cache.get_identity_resolution("a" * 64))
            cache.close()

    def test_rate_limiter_paces_hosts_independently(self) -> None:
        fake_time = FakeTime(100.0)
        limiter = RateLimiter(
            {"api.steampowered.com": 0.25, "store.steampowered.com": 0.5},
            sleeper=fake_time.sleep,
            clock=fake_time.clock,
        )
        self.assertEqual(limiter.wait("https://api.steampowered.com/a"), 0)
        self.assertEqual(limiter.wait("https://store.steampowered.com/a"), 0)
        self.assertAlmostEqual(limiter.wait("https://api.steampowered.com/b"), 0.25)
        self.assertAlmostEqual(limiter.wait("https://store.steampowered.com/b"), 0.25)
        self.assertEqual(fake_time.sleeps, [0.25, 0.25])

    def test_rate_limiter_shares_server_cooldown_per_host(self) -> None:
        fake_time = FakeTime(100.0)
        limiter = RateLimiter({}, sleeper=fake_time.sleep, clock=fake_time.clock)
        limiter.wait("https://cdn.example.test/first")
        limiter.defer("https://cdn.example.test/limited", 3.0)
        self.assertEqual(limiter.wait("https://cdn.example.test/other-worker"), 3.0)
        self.assertEqual(limiter.wait("https://other.example.test/unrelated"), 0.0)
        self.assertEqual(fake_time.sleeps, [3.0])

    def test_equal_jitter_backoff_is_bounded_and_deterministic_when_injected(self) -> None:
        self.assertEqual(jittered_backoff(3, randomizer=lambda: 0.0), 4.0)
        self.assertEqual(jittered_backoff(3, randomizer=lambda: 1.0), 8.0)
        self.assertEqual(jittered_backoff(20, randomizer=lambda: 1.0), 30.0)

    def test_http_retry_honors_retry_after_and_exponential_backoff(self) -> None:
        fake_time = FakeTime()
        transport = QueueTransport(
            [
                HTTPResult(429, {"Retry-After": "3"}, b"{}"),
                HTTPResult(503, {}, b"{}"),
                HTTPResult(200, {}, json.dumps(fixture("data_owned.json")).encode()),
            ]
        )
        api = self._track_api(SteamAPI(
            "TOP-SECRET", transport=transport,
            rate_limiter=RateLimiter({}, sleeper=fake_time.sleep, clock=fake_time.clock),
            sleeper=fake_time.sleep, clock=fake_time.clock, randomizer=lambda: 1.0,
            coordination_path=self.coordination_path,
        ))
        games = api.get_owned_games("76561198000000000")
        self.assertEqual([game["appid"] for game in games], [10, 20])
        self.assertEqual(fake_time.sleeps, [3.0, 2.0])
        self.assertEqual(len(transport.urls), 3)

    def test_http_authentication_failure_is_distinct_and_not_retried(self) -> None:
        transport = QueueTransport([HTTPResult(403, {}, b"{}")])
        api = self._track_api(SteamAPI(
            "TOP-SECRET", transport=transport,
            rate_limiter=RateLimiter({}), max_retries=5,
            coordination_path=self.coordination_path,
        ))

        with self.assertRaisesRegex(SteamAuthenticationError, "authentication or authorization"):
            api.get_owned_games("76561198000000000")
        self.assertEqual(len(transport.urls), 1)

    def test_unkeyed_forbidden_response_is_not_misreported_as_a_key_failure(self) -> None:
        transport = QueueTransport([HTTPResult(403, {}, b"{}")])
        api = self._track_api(SteamAPI(
            "TOP-SECRET", transport=transport,
            rate_limiter=RateLimiter({}), max_retries=5,
            coordination_path=self.coordination_path,
        ))

        with self.assertRaises(SteamRequestError) as caught:
            api.get_global_achievement_percentages(10)
        self.assertNotIsInstance(caught.exception, SteamAuthenticationError)
        self.assertRegex(str(caught.exception), "HTTP 403")

    def test_persistent_rate_limit_has_a_distinct_safe_error(self) -> None:
        transport = QueueTransport([HTTPResult(429, {}, b"{}")])
        api = self._track_api(SteamAPI(
            "TOP-SECRET", transport=transport,
            rate_limiter=RateLimiter({}), max_retries=0,
            coordination_path=self.coordination_path,
        ))

        with self.assertRaisesRegex(SteamRateLimitError, "rate limit") as caught:
            api.get_owned_games("76561198000000000")
        self.assertNotIn("TOP-SECRET", str(caught.exception))

    def test_vanity_resolution_and_key_safe_terminal_error(self) -> None:
        success = HTTPResult(
            200, {}, b'{"response":{"success":1,"steamid":"76561198000000000"}}'
        )
        transport = QueueTransport([success])
        api = self._track_api(SteamAPI("TOP-SECRET", transport=transport, rate_limiter=RateLimiter({}), coordination_path=self.coordination_path))
        resolved = api.resolve_identity("https://steamcommunity.com/id/friendly-name/")
        self.assertEqual(resolved, ResolvedIdentity("76561198000000000", "friendly-name"))
        query = parse_qs(urlsplit(transport.urls[0]).query)
        self.assertEqual(query["vanityurl"], ["friendly-name"])

        failing = QueueTransport([OSError("URL contained TOP-SECRET")] * 6)
        no_wait = FakeTime()
        api = self._track_api(SteamAPI(
            "TOP-SECRET", transport=failing,
            rate_limiter=RateLimiter({}, sleeper=no_wait.sleep, clock=no_wait.clock),
            sleeper=no_wait.sleep, clock=no_wait.clock,
            coordination_path=self.coordination_path,
        ))
        with self.assertRaises(SteamRequestError) as caught:
            api.get_owned_games("76561198000000000")
        self.assertNotIn("TOP-SECRET", str(caught.exception))
        self.assertNotIn("key=", str(caught.exception))
        self.assertEqual(len(failing.urls), 6, "five retries follow the initial attempt")

    def test_profiles_url_does_not_make_resolution_request(self) -> None:
        transport = QueueTransport([])
        api = self._track_api(SteamAPI("secret", transport=transport, rate_limiter=RateLimiter({}), coordination_path=self.coordination_path))
        resolved = api.resolve_identity("https://steamcommunity.com/profiles/76561198000000000/")
        self.assertEqual(resolved.steamid, "76561198000000000")
        self.assertEqual(transport.urls, [])

    def test_hidden_library_is_not_mistaken_for_a_public_empty_library(self) -> None:
        hidden = QueueTransport([HTTPResult(200, {}, b'{"response":{}}')])
        hidden_api = self._track_api(SteamAPI("secret", transport=hidden, rate_limiter=RateLimiter({}), coordination_path=self.coordination_path))
        with self.assertRaisesRegex(
            OwnedGamesUnavailable,
            "hidden or the API key may not match",
        ):
            hidden_api.get_owned_games("76561198000000000")

        empty = QueueTransport([HTTPResult(200, {}, b'{"response":{"game_count":0}}')])
        empty_api = self._track_api(SteamAPI("secret", transport=empty, rate_limiter=RateLimiter({}), coordination_path=self.coordination_path))
        self.assertEqual(empty_api.get_owned_games("76561198000000000"), [])


class FakeAPI:
    def __init__(self) -> None:
        self.owned = fixture("data_owned.json")["response"]["games"]
        self.store = fixture("data_store.json")["10"]["data"]
        achievements = fixture("data_achievements.json")
        self.player = achievements["player"]
        self.schema = achievements["schema"]
        self.global_rows = achievements["global"]
        self.calls: list[tuple] = []
        self.recent_failure = False
        self.owned_failure = False

    def resolve_identity(self, identity: str) -> ResolvedIdentity:
        self.calls.append(("resolve", identity))
        return ResolvedIdentity("76561198000000000", "Friendly")

    def get_owned_games(self, steamid: str):
        self.calls.append(("owned",))
        if self.owned_failure:
            raise RuntimeError("private")
        return [dict(game) for game in self.owned]

    def get_recently_played(self, steamid: str):
        self.calls.append(("recent",))
        if self.recent_failure:
            raise RuntimeError("temporary")
        return []

    def get_app_details(self, appid: int):
        self.calls.append(("store", appid))
        if appid == 20:
            raise RuntimeError("store failure")
        return dict(self.store)

    def get_player_achievements(self, steamid: str, appid: int):
        self.calls.append(("player", appid))
        return [dict(row) for row in self.player]

    def get_achievement_schema(self, appid: int):
        self.calls.append(("schema", appid))
        return [dict(row) for row in self.schema]

    def get_global_achievement_percentages(self, appid: int):
        self.calls.append(("global", appid))
        return [dict(row) for row in self.global_rows]


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.time = FakeTime()
        self.cache = CacheDB(Path(self.temp.name) / "cache.sqlite", clock=self.time.clock)
        self.api = FakeAPI()
        self.collector = SteamDataCollector(
            cache=self.cache, api=self.api, clock=self.time.clock, sleeper=self.time.sleep
        )

    def tearDown(self) -> None:
        self.cache.close()
        self.temp.cleanup()

    def test_cold_collection_normalizes_and_locally_degrades(self) -> None:
        self.cache.upsert_app_metadata(
            20,
            {
                "name": "Cached Twenty",
                "genres": ["Strategy"],
                "release_date": "2020-01-01",
                "header_image_url": "https://cdn.cloudflare.steamstatic.com/twenty.jpg",
            },
            fetched_at=self.time.clock(),
        )
        profile = self.collector.collect("input")
        self.assertEqual(
            self.api.calls,
            [("resolve", "input"), ("owned",)],
            "baseline collection must not cold-scan Store or achievements",
        )
        self.assertEqual(profile["games"][0]["data_status"]["metadata"], "deferred")
        self.assertEqual(profile["games"][0]["data_status"]["achievements"], "deferred")
        self.assertEqual(profile["games"][1]["name"], "Twenty")
        self.assertEqual(profile["games"][1]["metadata"]["genres"], [])
        self.assertIsNone(profile["games"][1]["artwork_url"])
        self.assertEqual(profile["games"][1]["data_status"]["metadata"], "excluded_unplayed")
        profile = self.collector.enrich_played_profile(profile)
        encoded = json.dumps(profile)
        self.assertNotIn("76561198000000000", encoded)
        self.assertNotIn("steamid", encoded.lower())
        self.assertEqual(profile["player_alias"], "Friendly")
        self.assertEqual(len(profile["games"]), 2)
        first, second = profile["games"]
        self.assertEqual(first["name"], "Ten Deluxe")
        self.assertEqual(first["playtime_minutes"], 120)
        self.assertEqual(first["metadata"]["genres"], ["Action"])
        self.assertEqual(first["achievements"]["status"], "ok")
        self.assertEqual(first["achievements"]["items"][0]["global_percent"], 12.5)
        self.assertEqual(first["achievements"]["items"][0]["name"], "First Step")
        self.assertEqual(first["achievements"]["items"][0]["api_name"], "FIRST")
        self.assertEqual(
            first["achievements"]["items"][0]["icon_url"],
            "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/10/first-color.jpg",
        )
        self.assertEqual(
            first["achievements"]["items"][0]["icon_gray_url"],
            "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/10/first-gray.jpg",
        )
        signals = derive_signals(profile)
        self.assertEqual(signals["achievements"]["coverage"]["value"]["unlocks"], 1)
        self.assertEqual(second["data_status"]["metadata"], "excluded_unplayed")
        self.assertEqual(second["data_status"]["achievements"], "not_played")
        self.assertNotIn(("store", 20), self.api.calls)

    def test_collection_and_enrichment_report_safe_monotonic_progress(self) -> None:
        collect_events: list[tuple[str, int | None, int | None]] = []
        profile = self.collector.collect(
            "input",
            progress=lambda message, current, total: collect_events.append(
                (message, current, total)
            ),
        )
        self.assertEqual((3, 3), collect_events[-1][1:])

        enrich_events: list[tuple[str, int | None, int | None]] = []
        self.collector.enrich_played_profile(
            profile,
            progress=lambda message, current, total: enrich_events.append(
                (message, current, total)
            ),
        )
        numeric = [event for event in enrich_events if event[1] is not None]
        self.assertEqual((1, 1), numeric[-1][1:])
        self.assertEqual(
            sorted(event[1] for event in numeric),
            [event[1] for event in numeric],
        )
        encoded = json.dumps([collect_events, enrich_events])
        self.assertNotIn("input", encoded)
        self.assertNotIn("76561198000000000", encoded)

    def test_warm_run_reuses_metadata_achievement_schema_and_rarity(self) -> None:
        first = self.collector.collect("input")
        self.collector.enrich_played_profile(first)
        self.api.calls.clear()
        profile = self.collector.collect("input")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(profile["data_snapshot"]["source"], "cached_snapshot")
        self.api.calls.clear()
        self.collector.enrich_played_profile(profile)
        self.assertEqual(self.api.calls, [])

    def test_snapshot_freezes_library_and_enrichment_for_24_hours(self) -> None:
        first = self.collector.collect("input")
        first = self.collector.enrich_played_profile(first)
        original_games = json.loads(json.dumps(first["games"]))
        original_snapshot = dict(first["data_snapshot"])
        self.api.calls.clear()
        self.api.owned[0]["playtime_forever"] = 999
        self.api.owned.append({"appid": 30, "name": "New Game", "playtime_forever": 60})
        self.time.now += 1
        second = self.collector.collect("input")
        self.assertEqual(self.api.calls, [])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(second["data_snapshot"]["id"], original_snapshot["id"])
        self.assertEqual(second["data_snapshot"]["collected_at"], original_snapshot["collected_at"])
        second = self.collector.enrich_played_profile(second)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(second["games"], original_games)
        self.assertEqual(second["data_snapshot"]["enriched_at"], original_snapshot["enriched_at"])
        self.assertEqual(second["data_snapshot"]["source"], "cached_snapshot")

    def test_snapshot_expires_at_exactly_24_hours_and_force_replaces_id(self) -> None:
        first = self.collector.collect("input")
        first_id = first["data_snapshot"]["id"]
        self.time.now += 24 * 60 * 60
        self.api.calls.clear()
        expired = self.collector.collect("input")
        self.assertIn(("owned",), self.api.calls)
        self.assertNotEqual(expired["data_snapshot"]["id"], first_id)
        current_id = expired["data_snapshot"]["id"]
        self.api.calls.clear()
        forced = self.collector.collect("input", force=True)
        self.assertIn(("owned",), self.api.calls)
        self.assertNotEqual(forced["data_snapshot"]["id"], current_id)

    def test_lazy_api_factory_is_not_called_for_a_hot_snapshot(self) -> None:
        first = self.collector.collect("input")
        created: list[bool] = []

        def factory():
            created.append(True)
            return self.api

        hot = SteamDataCollector(cache=self.cache, api_factory=factory, clock=self.time.clock)
        restored = hot.collect("76561198000000000")
        self.assertEqual(created, [])
        self.assertEqual(restored["data_snapshot"]["id"], first["data_snapshot"]["id"])

    def test_vanity_mapping_avoids_resolution_on_a_hot_snapshot(self) -> None:
        first = self.collector.collect("https://steamcommunity.com/id/Friendly-Name/")
        self.assertIn(("resolve", "https://steamcommunity.com/id/Friendly-Name/"), self.api.calls)
        self.api.calls.clear()
        hot = SteamDataCollector(
            cache=self.cache,
            api_factory=lambda: (_ for _ in ()).throw(AssertionError("network adapter created")),
            clock=self.time.clock,
        )
        restored = hot.collect("https://WWW.STEAMCOMMUNITY.COM/id/friendly-name")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(restored["data_snapshot"]["id"], first["data_snapshot"]["id"])

    def test_old_run_cannot_write_enrichment_into_a_replaced_snapshot(self) -> None:
        old = self.collector.collect("input")
        self.api.owned[0]["playtime_forever"] = 121
        current = self.collector.collect("input", force=True)
        self.api.calls.clear()
        enriched_old = self.collector.enrich_played_profile(old)
        self.assertIn(("recent",), self.api.calls)
        self.assertEqual(enriched_old["data_snapshot"]["id"], old["data_snapshot"]["id"])
        stored = self.cache.get_acquisition_snapshot("76561198000000000")
        self.assertEqual(stored["snapshot_id"], current["data_snapshot"]["id"])
        self.assertIsNone(stored["enrichment_payload"])

    def test_enrichment_targets_every_and_only_played_game(self) -> None:
        self.api.owned[1]["playtime_forever"] = 1
        profile = self.collector.collect("input")
        self.api.calls.clear()
        enriched = self.collector.enrich_played_profile(profile)
        self.assertIn(("store", 10), self.api.calls)
        self.assertIn(("store", 20), self.api.calls)
        self.assertEqual(enriched["data_status"]["enrichment"]["requested"], 2)

        for game in self.api.owned:
            game["playtime_forever"] = 0
        empty_played = self.collector.collect("input", force=True)
        self.api.calls.clear()
        unchanged = self.collector.enrich_played_profile(empty_played)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(unchanged["data_status"]["recently_played"], "not_applicable")
        self.assertEqual(unchanged["data_status"]["enrichment"]["requested"], 0)

    def test_playtime_increase_refreshes_only_player_state(self) -> None:
        first = self.collector.collect("input")
        self.collector.enrich_played_profile(first)
        self.api.calls.clear()
        self.api.owned[0]["playtime_forever"] = 121
        profile = self.collector.collect("input", force=True)
        self.collector.enrich_played_profile(profile)
        self.assertIn(("player", 10), self.api.calls)
        self.assertNotIn(("schema", 10), self.api.calls)
        self.assertNotIn(("global", 10), self.api.calls)

    def test_cache_ttls_refresh_sources_at_their_own_boundaries(self) -> None:
        first = self.collector.collect("input")
        self.collector.enrich_played_profile(first)
        self.time.now += 31 * 24 * 60 * 60
        baseline = self.collector.collect("input")
        self.assertEqual(baseline["games"][0]["data_status"]["metadata"], "cached_stale")
        self.api.calls.clear()
        refreshed = self.collector.enrich_played_profile(baseline)
        self.assertIn(("store", 10), self.api.calls)
        self.assertIn(("player", 10), self.api.calls)
        self.assertNotIn(("schema", 10), self.api.calls)
        self.assertIn(("global", 10), self.api.calls)
        self.assertEqual(refreshed["games"][0]["data_status"]["metadata"], "ok")

        self.time.now += 60 * 24 * 60 * 60
        after_ninety_days = self.collector.collect("input")
        self.api.calls.clear()
        self.collector.enrich_played_profile(after_ninety_days)
        self.assertIn(("schema", 10), self.api.calls)

    def test_recent_failure_is_local_and_force_refreshes_enrichment(self) -> None:
        first = self.collector.collect("input")
        self.collector.enrich_played_profile(first)
        self.api.calls.clear()
        self.api.recent_failure = True
        profile = self.collector.collect("input")
        profile = self.collector.enrich_played_profile(profile, force=True)
        self.assertEqual(profile["data_status"]["recently_played"], "unavailable")
        self.assertIn(("store", 10), self.api.calls)
        self.assertIn(("player", 10), self.api.calls)
        self.assertIn(("schema", 10), self.api.calls)
        self.assertIn(("global", 10), self.api.calls)

    def test_failed_store_refresh_preserves_previous_metadata(self) -> None:
        first_profile = self.collector.collect("input")
        self.collector.enrich_played_profile(first_profile)
        self.api.store = None
        original_get = self.api.get_app_details

        def failing_store(appid: int):
            if appid == 10:
                raise RuntimeError("temporary")
            return original_get(appid)

        self.api.get_app_details = failing_store
        profile = self.collector.collect("input")
        profile = self.collector.enrich_played_profile(profile, force=True)
        first = profile["games"][0]
        self.assertEqual(first["name"], "Ten Deluxe")
        self.assertEqual(first["metadata"]["genres"], ["Action"])
        self.assertEqual(first["data_status"]["metadata"], "cached_stale")

    def test_owned_games_failure_is_explicit(self) -> None:
        self.api.owned_failure = True
        with self.assertRaisesRegex(SteamRequestError, "failed unexpectedly"):
            self.collector.collect("input")

    def test_collector_preserves_typed_owned_games_failure(self) -> None:
        def rate_limited(_steamid: str):
            raise SteamRateLimitError("GetOwnedGames rate limit persisted after retries")

        self.api.get_owned_games = rate_limited
        with self.assertRaisesRegex(SteamRateLimitError, "rate limit"):
            self.collector.collect("input")


if __name__ == "__main__":
    unittest.main()
