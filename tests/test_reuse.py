from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.current_contract_fixture import current_plan_and_evidence
from steam_visualogue.cache_db import CacheDB  # noqa: E402
from steam_visualogue.editorial_reuse import reuse_editorial  # noqa: E402
from steam_visualogue.fingerprint import compute_evidence_fingerprint, compute_visual_fingerprint  # noqa: E402
from steam_visualogue.io_utils import write_json  # noqa: E402
from steam_visualogue.locales import ensure_run_config  # noqa: E402


class CurrentReuseTests(unittest.TestCase):
    def test_reuse_refuses_to_overlay_current_deck_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            plan, evidence = current_plan_and_evidence()
            profile = {"run_id": "reuse-fixture", "games": []}
            evidence["evidence_fingerprint"] = compute_evidence_fingerprint(profile)
            visual = {"evidence_fingerprint": evidence["evidence_fingerprint"], "sampling": {}, "palette": []}
            visual["visual_fingerprint"] = compute_visual_fingerprint(visual)
            ensure_run_config(root, "en-US")
            write_json(root / "profile.json", profile)
            write_json(root / "evidence.json", evidence)
            write_json(root / "visual-signals.json", visual)
            write_json(root / "deck-plan.json", plan)
            cache = CacheDB(Path(temporary) / "cache.sqlite")
            try:
                result = reuse_editorial(root, cache)
            finally:
                cache.close()
            self.assertEqual("conflict", result["status"])
            self.assertEqual("current-deck-artifacts-exist", result["reason"])

    def test_reuse_miss_does_not_read_historical_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            profile = {"run_id": "reuse-fixture", "games": []}
            evidence = {"run_id": "reuse-fixture", "metrics": [], "games": [], "achievements": [], "patterns": [], "cards": []}
            evidence["evidence_fingerprint"] = compute_evidence_fingerprint(profile)
            visual = {"evidence_fingerprint": evidence["evidence_fingerprint"], "sampling": {}, "palette": []}
            visual["visual_fingerprint"] = compute_visual_fingerprint(visual)
            ensure_run_config(root, "en-US")
            write_json(root / "profile.json", profile)
            write_json(root / "evidence.json", evidence)
            write_json(root / "visual-signals.json", visual)
            cache = CacheDB(Path(temporary) / "cache.sqlite")
            try:
                result = reuse_editorial(root, cache)
            finally:
                cache.close()
            self.assertEqual("miss", result["status"])


if __name__ == "__main__":
    unittest.main()
