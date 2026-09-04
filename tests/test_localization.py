from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.current_contract_fixture import current_plan_and_evidence
from steam_visualogue.cache_db import CacheDB  # noqa: E402
from steam_visualogue.io_utils import read_json, write_json  # noqa: E402
from steam_visualogue.cli import _ensure_localized_labels  # noqa: E402
from steam_visualogue.label_localization import localized_labels_current, materialize_localized_labels, scan_label_references  # noqa: E402
from steam_visualogue.locales import ensure_run_config  # noqa: E402


class LocalizationTests(unittest.TestCase):
    def test_references_are_scanned_from_typed_current_deck(self) -> None:
        plan, _ = current_plan_and_evidence()
        references = scan_label_references(plan)
        self.assertEqual(["game:1", "game:2", "game:3", "game:4", "game:5"], references["games"])

    def test_english_labels_use_canonical_evidence_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            plan, evidence = current_plan_and_evidence()
            ensure_run_config(root, "en-US")
            write_json(root / "profile.json", {"run_id": "localization-fixture", "games": [{"appid": index} for index in range(1, 6)]})
            write_json(root / "evidence.json", evidence)
            write_json(root / "deck-plan.json", plan)
            cache = CacheDB(Path(temporary) / "cache.sqlite")
            try:
                document = materialize_localized_labels(root, cache)
            finally:
                cache.close()
            self.assertEqual("steam-visualogue-localized-labels", document["format"])
            self.assertEqual("Game 1", document["games"]["game:1"]["display_name"])
            self.assertEqual(document["label_fingerprint"], read_json(root / "localized-labels.json")["label_fingerprint"])

    def test_stale_catalog_and_reference_sets_are_rematerialized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            cache_path = Path(temporary) / "cache.sqlite"
            plan, evidence = current_plan_and_evidence()
            ensure_run_config(root, "en-US")
            write_json(root / "profile.json", {"run_id": "localization-fixture", "games": [{"appid": index} for index in range(1, 6)]})
            write_json(root / "evidence.json", evidence)
            write_json(root / "deck-plan.json", plan)
            cache = CacheDB(cache_path)
            try:
                document = materialize_localized_labels(root, cache)
            finally:
                cache.close()

            stale = copy.deepcopy(document)
            stale["catalog_version"] = "stale-catalog"
            write_json(root / "localized-labels.json", stale)
            args = SimpleNamespace(cache=str(cache_path), force_labels=False)
            refreshed = _ensure_localized_labels(args, root, plan)
            self.assertTrue(localized_labels_current(refreshed, plan, "en-US"))
            self.assertNotEqual("stale-catalog", refreshed["catalog_version"])

            changed_plan = copy.deepcopy(plan)
            changed_plan["pages"][1]["presentation"]["content"]["subject"]["game_id"] = "game:2"
            changed_plan["pages"][1]["presentation"]["content"]["subject"]["asset_id"] = "game:2:portrait"
            write_json(root / "deck-plan.json", changed_plan)
            write_json(root / "localized-labels.json", refreshed)
            rematerialized = _ensure_localized_labels(args, root, changed_plan)
            self.assertNotIn("game:1", rematerialized["games"])
            self.assertIn("game:2", rematerialized["games"])


if __name__ == "__main__":
    unittest.main()
