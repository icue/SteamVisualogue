import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


from steam_visualogue.api_coordination import (  # noqa: E402
    APICoordination,
    CoordinationQuotaExceeded,
)
from steam_visualogue.credentials import api_coordination_path  # noqa: E402
from steam_visualogue.rate_limit import RateLimiter  # noqa: E402
from steam_visualogue.steam_api import HTTPResult, SteamAPI, SteamRateLimitError, SteamRequestError  # noqa: E402
from tests.support import FakeTime, close_apis  # noqa: E402


class QueueTransport:
    def __init__(self, results: list[HTTPResult]) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self.in_transaction: list[bool] = []
        self.coordination: APICoordination | None = None

    def __call__(self, url: str, timeout: float) -> HTTPResult:
        self.calls.append(url)
        if self.coordination is not None:
            self.in_transaction.append(self.coordination._connection.in_transaction)
        return self.results.pop(0)


class APICoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.apis: list[SteamAPI] = []
        self.addCleanup(self._close_apis)
        self.path = Path(self.temporary.name) / "coordination.sqlite"

    def _close_apis(self) -> None:
        close_apis(*self.apis)

    def _api(self, results: list[HTTPResult], fake: FakeTime, *, key: str = "K", ceiling: int = 90_000, max_retries: int = 5) -> tuple[SteamAPI, QueueTransport]:
        transport = QueueTransport(results)
        api = SteamAPI(
            key,
            transport=transport,
            rate_limiter=RateLimiter({}, sleeper=fake.sleep, clock=fake.clock),
            sleeper=fake.sleep,
            clock=fake.clock,
            daily_request_ceiling=ceiling,
            max_retries=max_retries,
            coordination_path=self.path,
        )
        self.apis.append(api)
        return api, transport

    @staticmethod
    def _owned_response() -> HTTPResult:
        return HTTPResult(200, {}, b'{"response":{"game_count":0}}')

    def test_clients_share_one_key_ledger_and_commit_before_transport(self) -> None:
        fake = FakeTime()
        api_one, transport_one = self._api([self._owned_response()], fake, key="shared")
        api_two, transport_two = self._api([self._owned_response()], fake, key="shared")
        coordinator = APICoordination(self.path, clock=fake.clock)
        transport_one.coordination = coordinator
        transport_two.coordination = coordinator
        api_one.get_owned_games("76561198000000000")
        api_two.get_owned_games("76561198000000000")
        row = coordinator._connection.execute("SELECT request_count FROM api_request_ledger").fetchone()
        self.assertEqual(2, row[0])
        self.assertEqual([False], transport_one.in_transaction)
        self.assertEqual([False], transport_two.in_transaction)
        coordinator.close()

    def test_production_coordination_path_is_workspace_scoped(self) -> None:
        self.assertEqual(Path.cwd() / ".steam-visualogue-api-coordination.sqlite", api_coordination_path())

    def test_combined_clients_cannot_exceed_configured_ceiling(self) -> None:
        fake = FakeTime()
        api_one, transport_one = self._api([self._owned_response()], fake, key="shared", ceiling=2)
        api_two, transport_two = self._api([self._owned_response(), self._owned_response()], fake, key="shared", ceiling=2)
        api_one.get_owned_games("76561198000000000")
        api_two.get_owned_games("76561198000000000")
        with self.assertRaisesRegex(SteamRequestError, "safety ceiling"):
            api_two.get_owned_games("76561198000000000")
        self.assertEqual(1, len(transport_one.calls))
        self.assertEqual(1, len(transport_two.calls))

    def test_retries_consume_request_reservations(self) -> None:
        fake = FakeTime()
        api, transport = self._api(
            [HTTPResult(503, {}, b"{}"), self._owned_response()],
            fake,
            ceiling=2,
        )
        api.get_owned_games("76561198000000000")
        connection = sqlite3.connect(self.path)
        count = connection.execute("SELECT request_count FROM api_request_ledger").fetchone()[0]
        connection.close()
        self.assertEqual(2, count)
        self.assertEqual(2, len(transport.calls))

    def test_committed_reservation_survives_a_crash_before_transport(self) -> None:
        fake = FakeTime()
        coordinator = APICoordination(self.path, clock=fake.clock)
        coordinator.reserve_request("https://api.steampowered.com/x", api_key_sha256="a" * 64, daily_ceiling=1)
        coordinator.close()
        second_coordinator = APICoordination(self.path, clock=fake.clock)
        with self.assertRaises(CoordinationQuotaExceeded):
            second_coordinator.reserve_request(
                "https://api.steampowered.com/x", api_key_sha256="a" * 64, daily_ceiling=1
            )
        second_coordinator.close()

    def test_utc_day_rollover_uses_a_new_counter(self) -> None:
        fake = FakeTime(1_700_000_000.0)
        coordinator = APICoordination(self.path, clock=fake.clock)
        coordinator.reserve_request("https://api.steampowered.com/x", api_key_sha256="a" * 64, daily_ceiling=1)
        fake.now = 1_700_000_000.0 + 86_400
        coordinator.reserve_request("https://api.steampowered.com/x", api_key_sha256="a" * 64, daily_ceiling=1)
        self.assertEqual(2, coordinator._connection.execute("SELECT COUNT(*) FROM api_request_ledger").fetchone()[0])
        coordinator.close()

    def test_429_cooldown_is_shared_and_later_writer_cannot_shorten_it(self) -> None:
        fake = FakeTime()
        api_one, _ = self._api([HTTPResult(429, {"Retry-After": "5"}, b"{}")], fake, key="shared", ceiling=5, max_retries=0)
        with self.assertRaises(SteamRateLimitError):
            api_one._request_json("https://api.steampowered.com", {}, resource="x", official=True, authenticated=True)
        api_two, transport_two = self._api([self._owned_response()], fake, key="shared", ceiling=5)
        api_two.get_owned_games("76561198000000000")
        self.assertIn(5.0, fake.sleeps)
        coordinator = APICoordination(self.path, clock=fake.clock)
        coordinator.record_cooldown("https://api.steampowered.com", scope=hashlib.sha256(b"shared").hexdigest(), blocked_until=fake.clock() + 10)
        coordinator.record_cooldown("https://api.steampowered.com", scope=hashlib.sha256(b"shared").hexdigest(), blocked_until=fake.clock() + 1)
        state = coordinator._connection.execute("SELECT blocked_until FROM api_host_state WHERE scope = ?", (hashlib.sha256(b"shared").hexdigest(),)).fetchone()[0]
        self.assertGreaterEqual(state, fake.clock() + 9.9)
        self.assertEqual(1, len(transport_two.calls))
        coordinator.close()

    def test_store_calls_are_paced_without_using_the_key_ledger(self) -> None:
        fake = FakeTime()
        api, transport = self._api(
            [HTTPResult(200, {}, b'{"10":{"success":false}}'), HTTPResult(200, {}, b'{"10":{"success":false}}')],
            fake,
            ceiling=0,
        )
        api.get_app_details(10)
        api.get_app_details(10)
        self.assertEqual(2, len(transport.calls))
        connection = sqlite3.connect(self.path)
        self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM api_request_ledger").fetchone()[0])
        connection.close()

    def test_corrupt_coordination_state_fails_closed_without_transport(self) -> None:
        self.path.write_bytes(b"not sqlite")
        fake = FakeTime()
        transport = QueueTransport([self._owned_response()])
        api = SteamAPI(
            "secret",
            transport=transport,
            rate_limiter=RateLimiter({}),
            clock=fake.clock,
            sleeper=fake.sleep,
            coordination_path=self.path,
        )
        self.apis.append(api)
        with self.assertRaisesRegex(SteamRequestError, "coordination"):
            api.get_owned_games("76561198000000000")
        self.assertEqual([], transport.calls)

    def test_raw_key_is_not_stored_or_exposed_by_safe_errors(self) -> None:
        fake = FakeTime()
        raw_key = "TOP-SECRET"
        api, transport = self._api([HTTPResult(403, {}, b"{}")], fake, key=raw_key)
        with self.assertRaises(SteamRequestError) as caught:
            api.get_global_achievement_percentages(10)
        self.assertNotIn(raw_key, str(caught.exception))
        payload = self.path.read_bytes()
        self.assertNotIn(raw_key.encode(), payload)
        connection = sqlite3.connect(self.path)
        rows = list(connection.execute("SELECT * FROM api_request_ledger"))
        connection.close()
        self.assertNotIn(raw_key, json.dumps(rows))
        self.assertEqual(1, len(transport.calls))


if __name__ == "__main__":
    unittest.main()
