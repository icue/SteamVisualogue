import copy
import random
import tempfile
import unittest
from pathlib import Path


from tests import SKILL_ROOT

from steam_visualogue.analytics import derive_signals  # noqa: E402
from steam_visualogue.semantic_candidates import (  # noqa: E402
    MAX_ACHIEVEMENT_CANDIDATE_GAMES,
    achievement_analysis_contract_fingerprint,
    select_achievement_candidates,
)


class SemanticCandidateTests(unittest.TestCase):
    def test_achievement_contract_fingerprint_tracks_relevant_context_only(self) -> None:
        context_source = (SKILL_ROOT / "references" / "agent-context.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            context_path = Path(temporary) / "agent-context.md"
            context_path.write_text(context_source + "\nUnrelated quality documentation.\n", encoding="utf-8")
            base = achievement_analysis_contract_fingerprint(context_path=context_path)
            context_path.write_text(context_source.replace("selected candidates", "selected achievement candidates"), encoding="utf-8")
            relevant = achievement_analysis_contract_fingerprint(context_path=context_path)
            self.assertNotEqual(base, relevant)
            context_path.write_text(context_source + "\nUnrelated quality documentation.\n", encoding="utf-8")
            unrelated = achievement_analysis_contract_fingerprint(context_path=context_path)
            self.assertEqual(base, unrelated)

    @staticmethod
    def _library(count: int = 8) -> tuple[dict, dict, dict]:
        games = []
        achievement_records = []
        for index in range(count):
            appid = 1000 + index
            achieved = index % 2 == 0
            percent = 2.0 if achieved else 90.0
            api_name = f"ACH_{index}"
            games.append(
                {
                    "appid": appid,
                    "name": f"Game {index}",
                    "playtime_minutes": (index + 1) * 100,
                    "metadata": {
                        "genres": ["Action" if index % 2 else "Strategy"],
                        "release_date": f"{2000 + (index % 3) * 10}-01-01",
                    },
                    "achievements": {
                        "status": "ok",
                        "items": [
                            {
                                "api_name": api_name,
                                "name": f"Achievement {index}",
                                "achieved": achieved,
                                "unlock_time": 1_700_000_000 + index if achieved else 0,
                                "global_percent": percent,
                            }
                        ],
                    },
                }
            )
            achievement_records.append(
                {
                    "id": f"achievement:{appid}:{api_name}",
                    "type": "achievement",
                    "facts": [
                        {"name": "state", "value": "unlocked" if achieved else "locked"},
                        {"name": "global_percent", "value": percent},
                        {"name": "unlock_time", "value": 1_700_000_000 + index if achieved else 0},
                    ],
                    "strength": 0.9,
                }
            )
        profile = {"run_id": "candidate-fixture", "games": games}
        signals = {
            "achievements": {
                "top_surprising_unlocks": {"value": [{"appid": 1000, "api_name": "ACH_0"}]},
                "top_surprising_misses": {"value": [{"appid": 1001, "api_name": "ACH_1"}]},
                "inversion_candidates": {
                    "value": [{
                        "appid": 1000,
                        "rarest_unlocked": {"appid": 1000, "api_name": "ACH_0"},
                        "easiest_missing": {"appid": 1001, "api_name": "ACH_1"},
                    }]
                },
                "timelines": {"value": [{"appid": 1000}]},
                "comeback_games": {"value": [{"appid": 1001}]},
                "burst_games": {"value": [{"appid": 1002}]},
                "completion_by_game": {"value": [{"appid": 1000, "completion": 1.0}]},
            },
            "series_groups": {"value": [{"games": [{"appid": 1000}, {"appid": 1001}]}]},
            "cross_game_patterns": {"value": [{"games": [{"appid": 1001}, {"appid": 1002}]}]},
        }
        evidence = {
            "evidence_fingerprint": None,
            "metrics": [],
            "games": [],
            "achievements": achievement_records,
            "patterns": [],
            "cards": [],
        }
        return profile, signals, evidence

    def test_selection_is_deterministic_under_shuffled_source_order(self) -> None:
        profile, signals, evidence = self._library()
        shuffled_profile = copy.deepcopy(profile)
        shuffled_signals = copy.deepcopy(signals)
        shuffled_evidence = copy.deepcopy(evidence)
        random.Random(17).shuffle(shuffled_profile["games"])
        random.Random(19).shuffle(shuffled_evidence["achievements"])
        random.Random(23).shuffle(shuffled_signals["achievements"]["timelines"]["value"])
        first = select_achievement_candidates(profile, signals, evidence)
        second = select_achievement_candidates(shuffled_profile, shuffled_signals, shuffled_evidence)
        self.assertEqual(first, second)

    def test_excludes_unusable_achievement_inputs_and_requires_evidence_records(self) -> None:
        profile, signals, evidence = self._library(6)
        profile["games"].extend(
            [
                {"appid": 2000, "name": "Unplayed", "playtime_minutes": 0, "achievements": {"status": "ok", "items": []}},
                {"appid": 2001, "name": "Private", "playtime_minutes": 20, "achievements": {"status": "private", "items": []}},
                {"appid": 2002, "name": "Unsupported", "playtime_minutes": 20, "achievements": {"status": "unsupported", "items": []}},
                {"appid": 2003, "name": "No evidence", "playtime_minutes": 20, "achievements": {"status": "ok", "items": [{"api_name": "NOPE", "achieved": True}]}},
            ]
        )
        result = select_achievement_candidates(profile, signals, evidence)
        selected = {row["game_id"] for row in result["selected"]}
        self.assertNotIn("game:2000", selected)
        self.assertNotIn("game:2001", selected)
        self.assertNotIn("game:2002", selected)
        self.assertNotIn("game:2003", selected)
        excluded = result["summary"]["excluded_counts"]
        self.assertEqual(excluded["unplayed"], 1)
        self.assertEqual(excluded["private_achievements"], 1)
        self.assertEqual(excluded["unsupported_achievements"], 1)
        self.assertEqual(excluded["no_usable_achievement_candidate"], 1)

    def test_excludes_ordinary_records_without_an_observable_semantic_reason(self) -> None:
        profile, signals, evidence = self._library(1)
        item = profile["games"][0]["achievements"]["items"][0]
        item.update({"achieved": False, "global_percent": 10.0, "unlock_time": 0})
        profile["games"][0]["achievements"]["items"].extend(
            [
                {"api_name": "extra-1", "achieved": True, "global_percent": 10.0, "unlock_time": 0},
                {"api_name": "extra-2", "achieved": True, "global_percent": 10.0, "unlock_time": 0},
                {"api_name": "extra-3", "achieved": False, "global_percent": 10.0, "unlock_time": 0},
                {"api_name": "extra-4", "achieved": False, "global_percent": 10.0, "unlock_time": 0},
            ]
        )
        evidence["achievements"][0]["facts"] = [
            {"name": "state", "value": "locked"},
            {"name": "global_percent", "value": 10.0},
        ]
        result = select_achievement_candidates(profile, {}, evidence)
        self.assertEqual([], result["selected"])
        self.assertEqual(1, result["summary"]["excluded_counts"]["no_observable_semantic_reason"])

    def test_reserves_available_families_and_representation_strata(self) -> None:
        profile, signals, evidence = self._library()
        result = select_achievement_candidates(profile, signals, evidence)
        summary = result["summary"]
        for family in ("rare-unlocked", "common-miss", "inversion", "completion-pole", "high-playtime-low-completion", "low-playtime-high-completion", "dated-activity", "comeback", "burst", "series-linked", "verified-cross-game"):
            self.assertIn(family, summary["counts_by_achievement_family"] | summary["counts_by_selection_reason"])
        self.assertTrue(summary["counts_by_genre_stratum"])
        self.assertTrue(summary["counts_by_release_era_stratum"])
        self.assertTrue(summary["counts_by_playtime_stratum"])
        self.assertLessEqual(summary["selected_game_count"], MAX_ACHIEVEMENT_CANDIDATE_GAMES)

    def test_candidate_limit_does_not_mutate_full_library_signals(self) -> None:
        profile, signals, evidence = self._library(30)
        before = derive_signals(copy.deepcopy(profile))
        select_achievement_candidates(profile, signals, evidence, max_games=60)
        after = derive_signals(copy.deepcopy(profile))
        self.assertEqual(before, after)

    def test_large_library_is_bounded_to_sixty_examples(self) -> None:
        profile, signals, evidence = self._library(1845)
        result = select_achievement_candidates(profile, signals, evidence)
        self.assertEqual(1845, result["summary"]["all_played_game_count"])
        self.assertLessEqual(result["summary"]["selected_game_count"], 60)
        self.assertLessEqual(len(result["selected"]), 60)


if __name__ == "__main__":
    unittest.main()
