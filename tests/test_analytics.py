from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

from steam_visualogue.analytics import derive_signals  # noqa: E402
from steam_visualogue.evidence import _condensed_cards, build_evidence  # noqa: E402


FIXTURE = Path(__file__).parent / "fixtures" / "analytics_profile.json"


def load_profile():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class AnalyticsTests(unittest.TestCase):
    def test_condensed_cards_reserve_capacity_for_every_evidence_category(self):
        def records(category: str):
            return [
                {
                    "id": f"{category}:{index}",
                    "type": category,
                    "facts": [],
                    "strength": 1.0 - (index / 1000),
                }
                for index in range(50)
            ]

        cards = _condensed_cards(
            records("metric"),
            records("game"),
            records("achievement"),
            records("pattern"),
        )

        self.assertEqual(40, len(cards))
        self.assertEqual(
            {"metric": 10, "game": 10, "achievement": 10, "pattern": 10},
            dict(Counter(card["type"] for card in cards)),
        )

    def test_every_section_signal_carries_provenance_and_coverage(self):
        signals = derive_signals(load_profile())
        for section_name in ("library", "playtime", "genres", "release_era", "achievements"):
            for key, signal in signals[section_name].items():
                with self.subTest(section=section_name, signal=key):
                    self.assertIn("value", signal)
                    self.assertIsInstance(signal.get("coverage"), dict)
                    self.assertEqual(signal.get("source"), "derived:normalized_profile")
                    self.assertIn(signal.get("confidence"), {"low", "medium", "high"})
        for candidate in signals["candidate_tensions"]:
            self.assertIn("coverage", candidate)
            self.assertIn("source", candidate)
            self.assertIn("confidence", candidate)

    def test_library_and_playtime_metrics_have_expected_values(self):
        signals = derive_signals(load_profile())
        library = signals["library"]
        self.assertEqual(library["owned_count"]["value"], 4)
        self.assertEqual(library["played_count"]["value"], 3)
        self.assertEqual(library["unplayed_count"]["value"], 1)
        self.assertEqual(library["meaningfully_played_count"]["value"], 2)
        self.assertEqual(library["total_playtime_minutes"]["value"], 1000)
        self.assertAlmostEqual(library["engagement_ratio"]["value"], 0.75)

        playtime = signals["playtime"]
        self.assertAlmostEqual(playtime["top1_share"]["value"], 0.6)
        self.assertAlmostEqual(playtime["top3_share"]["value"], 1.0)
        self.assertAlmostEqual(playtime["hhi"]["value"], 0.46)
        self.assertAlmostEqual(playtime["gini"]["value"], 0.5)
        expected_entropy = -sum(value * math.log(value) for value in (0.6, 0.3, 0.1))
        self.assertAlmostEqual(playtime["shannon_entropy"]["value"], expected_entropy)
        self.assertAlmostEqual(playtime["effective_games"]["value"], math.exp(expected_entropy))

    def test_genres_use_one_unit_per_multilabel_game_and_separate_attributes(self):
        signals = derive_signals(load_profile())
        genres = signals["genres"]
        played_titles = genres["played_title_distribution"]["value"]
        played = genres["playtime_distribution"]["value"]
        self.assertAlmostEqual(sum(played_titles.values()), 1.0)
        self.assertAlmostEqual(played_titles["Action"], 0.5)
        self.assertAlmostEqual(played_titles["Adventure"], 1 / 6)
        self.assertAlmostEqual(played_titles["RPG"], 1 / 6)
        self.assertAlmostEqual(played_titles["Strategy"], 1 / 6)
        self.assertAlmostEqual(played["Action"], 0.35)
        self.assertAlmostEqual(played["Adventure"], 0.05)
        self.assertAlmostEqual(played["RPG"], 0.30)
        self.assertAlmostEqual(played["Strategy"], 0.30)
        self.assertAlmostEqual(genres["indie_share"]["value"], 1 / 3)
        self.assertAlmostEqual(genres["free_to_play_share"]["value"], 1 / 3)
        self.assertAlmostEqual(genres["early_access_share"]["value"], 1 / 3)
        self.assertEqual(genres["played_title_distribution"]["coverage"]["titles"], 1.0)
        self.assertEqual(genres["played_title_distribution"]["coverage"]["playtime"], 1.0)

    def test_release_era_is_playtime_weighted_and_coverage_aware(self):
        release = derive_signals(load_profile())["release_era"]
        self.assertAlmostEqual(release["weighted_mean_release_year"]["value"], 2007.0)
        self.assertEqual(release["weighted_median_release_year"]["value"], 2000.0)
        self.assertEqual(release["era_span"]["value"], 20)
        self.assertAlmostEqual(release["old_game_share"]["value"], 0.7)
        self.assertAlmostEqual(release["recent_game_share"]["value"], 0.0)
        self.assertEqual(release["weighted_mean_release_year"]["coverage"]["titles"], 1.0)
        self.assertEqual(release["weighted_mean_release_year"]["coverage"]["playtime"], 1.0)

    def test_achievement_completion_rarity_surprise_and_inversion(self):
        achievements = derive_signals(load_profile())["achievements"]
        self.assertEqual(achievements["perfected_games"]["value"], 1)
        self.assertEqual(achievements["completion_80_plus_games"]["value"], 1)
        self.assertAlmostEqual(achievements["completion_mean"]["value"], 0.9)
        self.assertAlmostEqual(achievements["completion_variance"]["value"], 0.01)
        self.assertEqual(achievements["ultra_rare_count"]["value"], 1)
        self.assertEqual(achievements["rare_count"]["value"], 2)
        self.assertEqual(achievements["rarest_unlock"]["value"]["api_name"], "ACH_C")
        self.assertEqual(achievements["top_surprising_misses"]["value"][0]["api_name"], "ACH_D")
        inversion = achievements["inversion_candidates"]["value"][0]
        self.assertEqual(inversion["semantic_status"], "unchecked")
        self.assertAlmostEqual(inversion["gap_percentage_points"], 89.5)
        self.assertEqual(achievements["completion_mean"]["confidence"], "high")
        self.assertEqual(achievements["rarest_unlock"]["confidence"], "high")

    def test_timeline_span_comeback_and_burst_are_observable_only(self):
        achievements = derive_signals(load_profile())["achievements"]
        game_one = next(row for row in achievements["timelines"]["value"] if row["appid"] == "10")
        self.assertEqual(game_one["max_unlocks_in_24h"], 3)
        self.assertEqual(game_one["burst_count"], 1)
        self.assertEqual(game_one["comeback_count"], 1)
        longest = achievements["longest_observable_achievement_span"]["value"]
        self.assertEqual(longest["appid"], "20")
        self.assertIn("Observable achievement activity span", achievements["longest_observable_achievement_span"]["definition"])

    def test_achievement_rhythm_contrast_pairs_distinct_burst_and_long_return_games(self):
        profile = {
            "run_id": "rhythm-contrast",
            "player_alias": "Player",
            "generated_at": "2025-01-01T00:00:00Z",
            "games": [
                {
                    "appid": 10,
                    "name": "Burst Game",
                    "playtime_minutes": 600,
                    "metadata": {"genres": ["Action"], "release_year": 2024},
                    "achievements": {
                        "status": "ok",
                        "items": [
                            {
                                "api_name": "BURST_PROLOGUE",
                                "name": "Burst Prologue",
                                "achieved": True,
                                "unlock_time": 1514764800,
                                "global_percent": 30,
                            },
                            *[
                            {
                                "api_name": f"BURST_{index}",
                                "name": f"Burst {index}",
                                "achieved": True,
                                "unlock_time": 1704067200 + index * 3600,
                                "global_percent": index + 1,
                            }
                            for index in range(12)
                            ],
                        ],
                    },
                },
                {
                    "appid": 20,
                    "name": "Long Return Game",
                    "playtime_minutes": 600,
                    "metadata": {"genres": ["RPG"], "release_year": 2020},
                    "achievements": {
                        "status": "ok",
                        "items": [
                            {
                                "api_name": f"RETURN_{index}",
                                "name": f"Return {index}",
                                "achieved": True,
                                "unlock_time": timestamp,
                            }
                            for index, timestamp in enumerate(
                                (1609459200, 1640995200, 1704067200)
                            )
                        ],
                    },
                },
                *[
                    {
                        "appid": 100 + game_index,
                        "name": f"Other Burst {game_index}",
                        "playtime_minutes": 60,
                        "metadata": {"genres": ["Action"], "release_year": 2024},
                        "achievements": {
                            "status": "ok",
                            "items": [
                                {
                                    "api_name": f"OTHER_{game_index}_{unlock_index}",
                                    "name": f"Other {game_index} {unlock_index}",
                                    "achieved": True,
                                    "unlock_time": (
                                        1706745600
                                        + game_index * 86400
                                        + unlock_index * 3600
                                    ),
                                }
                                for unlock_index in range(11)
                            ],
                        },
                    }
                    for game_index in range(11)
                ],
            ],
        }

        signals = derive_signals(profile)
        contrasts = [
            candidate
            for candidate in signals["candidate_tensions"]
            if candidate["type"] == "achievement_rhythm_contrast"
        ]
        self.assertEqual(1, len(contrasts))
        contrast = contrasts[0]
        facts = {fact["name"]: fact["value"] for fact in contrast["facts"]}
        self.assertEqual("10", facts["burst_game_appid"])
        self.assertEqual(12, facts["burst_max_unlocks_in_24h"])
        self.assertEqual("20", facts["long_running_game_appid"])
        self.assertEqual(3, facts["long_activity_year_count"])
        self.assertGreaterEqual(facts["long_comeback_count"], 1)
        self.assertEqual("observable achievement activity", facts["scope"])

        evidence = build_evidence(profile, signals)
        record = next(
            row
            for row in evidence["patterns"]
            if row["type"] == "achievement_rhythm_contrast"
        )
        self.assertEqual(["game:10", "game:20"], record["related_ids"])
        self.assertIn(record["id"], {card["id"] for card in evidence["cards"]})

    def test_exact_90_day_and_24_hour_boundaries_are_inclusive(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        times = [start, start + timedelta(hours=24), start + timedelta(days=90)]
        profile = {
            "run_id": "boundaries",
            "generated_at": "2024-12-31T00:00:00Z",
            "games": [{
                "appid": 1,
                "name": "Boundary",
                "playtime_minutes": 1,
                "metadata": {"genres": ["Action"], "release_year": 2024},
                "achievements": {
                    "status": "ok",
                    "items": [
                        {"api_name": f"A{i}", "achieved": True, "unlock_time": time.timestamp(), "global_percent": 50}
                        for i, time in enumerate(times)
                    ],
                },
            }],
        }
        timeline = derive_signals(profile, {"burst_min_unlocks": 2})["achievements"]["timelines"]["value"][0]
        self.assertEqual(timeline["max_unlocks_in_24h"], 2)
        self.assertEqual(timeline["burst_count"], 1)
        self.assertEqual(timeline["comeback_count"], 0)  # the preceding unlock makes this gap 89 days

        profile["games"][0]["achievements"]["items"] = [
            {"api_name": "A", "achieved": True, "unlock_time": start.timestamp(), "global_percent": 50},
            {"api_name": "B", "achieved": True, "unlock_time": (start + timedelta(days=90)).timestamp(), "global_percent": 50},
        ]
        timeline = derive_signals(profile)["achievements"]["timelines"]["value"][0]
        self.assertEqual(timeline["comeback_count"], 1)

    def test_cross_signal_outputs_are_candidates_not_personality_claims(self):
        profile = {
            "run_id": "tensions",
            "generated_at": "2025-01-01T00:00:00Z",
            "games": [],
        }
        genre_names = ["Action", "Adventure", "RPG", "Strategy", "Simulation"]
        for index in range(20):
            played = index < 5
            profile["games"].append({
                "appid": index,
                "name": f"G{index}",
                "playtime_minutes": 800 if index == 0 else 50 if played else 0,
                "metadata": {"genres": ["Action" if played else genre_names[index % len(genre_names)]], "release_year": 2020},
                "achievements": {
                    "status": "ok" if index == 0 else "unsupported",
                    "items": [
                        {"api_name": "A", "achieved": False, "unlock_time": 0, "global_percent": 80},
                        {"api_name": "B", "achieved": False, "unlock_time": 0, "global_percent": 20},
                    ] if index == 0 else [],
                },
            })
        tensions = derive_signals(profile)["candidate_tensions"]
        types = {row["type"] for row in tensions}
        self.assertIn("library_engagement_gap", types)
        self.assertNotIn("played_title_genre_distribution_gap", types)
        self.assertIn("completion_playtime_depth_gap", types)
        for row in tensions:
            self.assertIn("coverage", row)
            self.assertIn("source", row)
            self.assertIn("confidence", row)
            self.assertIsInstance(row["facts"], list)

    def test_attention_breadth_and_completion_polarity_become_story_candidates(self):
        games = []
        for index in range(100):
            played = index < 40
            if index < 30:
                unlocked = 1
            elif index < 35:
                unlocked = 5
            else:
                unlocked = 9
            items = [
                {
                    "api_name": f"A{item}",
                    "achieved": played and item < unlocked,
                    "unlock_time": 0,
                    "global_percent": 50,
                }
                for item in range(10)
            ] if played else []
            games.append({
                "appid": index + 1,
                "name": f"Game {index + 1}",
                "playtime_minutes": 100 if played else 0,
                "metadata": {"genres": ["Action"], "release_year": 2020},
                "achievements": {
                    "status": "ok" if played else "unsupported",
                    "items": items,
                },
            })

        profile = {
            "run_id": "breadth-and-polarity",
            "generated_at": "2025-01-01T00:00:00Z",
            "games": games,
        }
        signals = derive_signals(profile)
        candidates = {row["type"]: row for row in signals["candidate_tensions"]}

        breadth = candidates["library_attention_breadth_contrast"]
        breadth_facts = {fact["name"]: fact["value"] for fact in breadth["facts"]}
        self.assertAlmostEqual(40.0, breadth_facts["effective_games"])
        self.assertAlmostEqual(0.25, breadth_facts["top10_share"])
        self.assertGreaterEqual(breadth_facts["gini"], 0.5)
        self.assertEqual("library-wide recorded playtime", breadth_facts["scope"])

        polarity = candidates["selective_completion_contrast"]
        polarity_facts = {fact["name"]: fact["value"] for fact in polarity["facts"]}
        self.assertEqual(30, polarity_facts["completion_below_20_games"])
        self.assertEqual(5, polarity_facts["completion_80_plus_games"])
        self.assertEqual(0, polarity_facts["perfected_games"])
        self.assertEqual("games with available player achievement data", polarity_facts["scope"])

        evidence = build_evidence(profile, signals)
        pattern_types = {row["type"] for row in evidence["patterns"]}
        self.assertIn("library_attention_breadth_contrast", pattern_types)
        self.assertIn("selective_completion_contrast", pattern_types)
        card_types = {row["type"] for row in evidence["cards"]}
        self.assertIn("library_attention_breadth_contrast", card_types)
        self.assertIn("selective_completion_contrast", card_types)

    def test_timestamped_achievement_activity_can_surface_an_era_composition_shift(self):
        games = []
        for index in range(12):
            year = 2012 if index < 6 else 2022
            genre = "Action" if index < 6 else "Adventure"
            unlock_time = datetime(year, 6, 1, tzinfo=timezone.utc).timestamp()
            games.append({
                "appid": index + 1,
                "name": f"Era Game {index + 1}",
                "playtime_minutes": 120,
                "metadata": {"genres": [genre], "release_year": year},
                "achievements": {
                    "status": "ok",
                    "items": [{
                        "api_name": "A",
                        "achieved": True,
                        "unlock_time": unlock_time,
                        "global_percent": 50,
                    }],
                },
            })

        signals = derive_signals({
            "run_id": "activity-era-shift",
            "generated_at": "2025-01-01T00:00:00Z",
            "games": games,
        })
        shift = next(
            row
            for row in signals["candidate_tensions"]
            if row["type"] == "achievement_activity_genre_shift"
        )
        facts = {fact["name"]: fact["value"] for fact in shift["facts"]}
        self.assertEqual("2010–2014", facts["earlier_era"])
        self.assertEqual("2020–2024", facts["later_era"])
        self.assertEqual("Action", facts["most_decreased_genre"])
        self.assertEqual("Adventure", facts["most_increased_genre"])
        self.assertEqual(6, facts["earlier_active_games"])
        self.assertEqual(6, facts["later_active_games"])
        self.assertEqual(
            "distinct games with timestamped achievement activity per five-year era",
            facts["scope"],
        )

    def test_same_series_groups_surface_three_played_titles_as_an_evidence_candidate(self):
        profile = {
            "run_id": "same-series-atlas",
            "generated_at": "2025-01-01T00:00:00Z",
            "games": [
                {
                    "appid": 100,
                    "name": "Nioh: Complete Edition",
                    "playtime_minutes": 1200,
                    "metadata": {"release_year": 2017},
                    "achievements": {"items": []},
                },
                {
                    "appid": 200,
                    "name": "Nioh 2 – The Complete Edition",
                    "playtime_minutes": 1500,
                    "metadata": {"release_year": 2021},
                    "achievements": {"items": []},
                },
                {
                    "appid": 300,
                    "name": "Nioh 3",
                    "playtime_minutes": 300,
                    "metadata": {"release_year": 2026},
                    "achievements": {"items": []},
                },
                {
                    "appid": 400,
                    "name": "Unrelated Archive",
                    "playtime_minutes": 900,
                    "metadata": {"release_year": 2020},
                    "achievements": {"items": []},
                },
            ],
        }

        signals = derive_signals(profile)
        group = next(
            item
            for item in signals["candidate_tensions"]
            if item["type"] == "same_series_group"
        )
        facts = {fact["name"]: fact["value"] for fact in group["facts"]}
        self.assertEqual("nioh", facts["series_key"])
        self.assertEqual(3, facts["game_count"])
        self.assertEqual(
            ["game:100", "game:200", "game:300"],
            group["related_ids"],
        )
        self.assertEqual(3, len(facts["game_rows"]))
        self.assertEqual("same_series_group", group["type"])

        evidence = build_evidence(profile, signals)
        series_record = next(
            item for item in evidence["patterns"] if item["type"] == "same_series_group"
        )
        self.assertIn("game:200", series_record["related_ids"])
        self.assertIn(
            "same_series_group",
            {item["type"] for item in evidence["cards"]},
        )

    def test_cross_game_patterns_surface_burst_and_divergence_candidates(self):
        profile = {
            "run_id": "cross-game-atlas",
            "generated_at": "2025-01-01T00:00:00Z",
            "games": [],
        }
        for appid, name, minutes, unlocked in (
            (1, "Quick Finish", 60, 8),
            (2, "Tiny Finish", 120, 7),
            (3, "Deep Partial", 1000, 2),
            (4, "Long Partial", 1500, 4),
        ):
            profile["games"].append({
                "appid": appid,
                "name": name,
                "playtime_minutes": minutes,
                "achievements": {
                    "status": "ok",
                    "items": [
                        {
                            "api_name": f"A{index}",
                            "achieved": index < unlocked,
                            "unlock_time": 0,
                            "global_percent": 50,
                        }
                        for index in range(10)
                    ],
                },
            })

        signals = derive_signals(profile)
        patterns = {
            row["pattern_key"]: row
            for row in signals["cross_game_patterns"]["value"]
        }
        self.assertIn("completion_attention_divergence", patterns)
        divergence = patterns["completion_attention_divergence"]
        self.assertEqual(4, divergence["game_count"])
        self.assertEqual(
            [
                "pattern:cross-game-atlas:completion_attention_divergence:1",
                "game:1",
            ],
            divergence["games"][0]["evidence_ids"],
        )

        evidence = build_evidence(profile, signals)
        record_ids = {record["id"] for record in evidence["patterns"]}
        self.assertIn(
            "pattern:cross-game-atlas:completion_attention_divergence:1",
            record_ids,
        )

    def test_empty_and_missing_data_return_defined_low_coverage_outputs(self):
        profile = {"run_id": "empty", "generated_at": "2025-01-01T00:00:00Z", "games": []}
        signals = derive_signals(profile)
        self.assertEqual(signals["library"]["owned_count"]["value"], 0)
        self.assertEqual(signals["playtime"]["effective_games"]["value"], 0.0)
        self.assertEqual(signals["genres"]["played_title_distribution"]["value"], {})
        self.assertIsNone(signals["release_era"]["weighted_mean_release_year"]["value"])
        self.assertIsNone(signals["achievements"]["completion_mean"]["value"])
        self.assertEqual(signals["candidate_tensions"], [])
        evidence = build_evidence(profile, signals)
        self.assertEqual(evidence["games"], [])
        self.assertEqual(evidence["achievements"], [])
        self.assertEqual(evidence["patterns"], [])
        self.assertLessEqual(evidence["card_count"], 40)

    def test_evidence_ids_facts_and_condensed_cards_are_stable(self):
        profile = load_profile()
        signals = derive_signals(profile)
        evidence = build_evidence(profile, signals)
        self.assertIn("metric:owned_count", {row["id"] for row in evidence["metrics"]})
        self.assertIn(
            "metric:meaningful_played_threshold", {row["id"] for row in evidence["cards"]}
        )
        self.assertIn("game:10", {row["id"] for row in evidence["games"]})
        self.assertNotIn("game:40", {row["id"] for row in evidence["games"]})
        self.assertIn("achievement:10:ACH_C", {row["id"] for row in evidence["achievements"]})
        self.assertIn("pattern:achievement_inversion:10", {row["id"] for row in evidence["patterns"]})
        self.assertIn("pattern:longest_achievement_span:20", {row["id"] for row in evidence["patterns"]})
        self.assertLessEqual(evidence["card_count"], 40)
        for group in ("metrics", "games", "achievements", "patterns", "cards"):
            for row in evidence[group]:
                self.assertIn("facts", row)
                self.assertIn("strength", row)
                self.assertIn("coverage", row)

        reordered = deepcopy(profile)
        reordered["games"].reverse()
        reordered_signals = derive_signals(reordered)
        self.assertEqual(signals, reordered_signals)
        self.assertEqual(evidence, build_evidence(reordered, reordered_signals))

    def test_new_narrative_tensions_and_signals(self):
        profile = {
            "run_id": "test-new-tensions",
            "player_alias": "Explorer",
            "generated_at": "2025-01-01T00:00:00Z",
            "games": [
                {
                    "appid": 101,
                    "name": "Hardcore Action 1",
                    "playtime_minutes": 3600,
                    "metadata": {"genres": ["Action", "Shooter"], "release_year": 2005},
                    "achievements": {
                        "items": [
                            {"api_name": "ACH_EASY_TUTORIAL", "name": "Tutorial", "achieved": False, "global_percent": 92.0},
                            {"api_name": "ACH_HARD_BOSS", "name": "Abyss King", "achieved": True, "global_percent": 1.5, "unlock_time": 1600000000},
                            {"api_name": "ACH_PROLOGUE", "name": "Start Game", "achieved": True, "global_percent": 2.0, "unlock_time": 1600000000},
                        ]
                    },
                },
                {
                    "appid": 102,
                    "name": "Hardcore Action 2",
                    "playtime_minutes": 7200,
                    "metadata": {"genres": ["Action", "Shooter"], "release_year": 2022},
                    "achievements": {
                        "items": [
                            {"api_name": f"ACH_{i}", "name": f"Ach {i}", "achieved": True, "global_percent": 3.0, "unlock_time": 1600000000}
                            for i in range(19)
                        ] + [
                            {"api_name": "ACH_GRIND", "name": "Grind 10000", "achieved": False, "global_percent": 0.5}
                        ]
                    },
                },
                {
                    "appid": 103,
                    "name": "Relaxing Puzzle 1",
                    "playtime_minutes": 1800,
                    "metadata": {"genres": ["Puzzle", "Casual"], "release_year": 2008},
                    "achievements": {
                        "items": [
                            {"api_name": "P_ACH_0", "name": "Puzzle 0", "achieved": True, "global_percent": 2.0, "unlock_time": 1600000000}
                        ] + [
                            {"api_name": f"P_ACH_{i}", "name": f"Puzzle {i}", "achieved": False, "global_percent": 2.0}
                            for i in range(1, 10)
                        ]
                    },
                },
                {
                    "appid": 104,
                    "name": "Relaxing Puzzle 2",
                    "playtime_minutes": 1200,
                    "metadata": {"genres": ["Puzzle", "Casual"], "release_year": 2024},
                    "achievements": {
                        "items": [
                            {"api_name": "P2_ACH_0", "name": "Puzzle2 0", "achieved": True, "global_percent": 2.0, "unlock_time": 1600000000}
                        ] + [
                            {"api_name": f"P2_ACH_{i}", "name": f"Puzzle2 {i}", "achieved": False, "global_percent": 2.0}
                            for i in range(1, 10)
                        ]
                    },
                },
            ],
        }
        signals = derive_signals(profile)
        ach = signals["achievements"]
        self.assertIsNotNone(ach["anti_mainstream_score"]["value"])
        self.assertGreater(ach["anti_mainstream_score"]["value"], 80.0)
        self.assertIsNotNone(ach["peak_daily_burst"]["value"])
        self.assertEqual(ach["peak_daily_burst"]["value"]["date"], "2020-09-13")

        tension_types = {t["type"] for t in signals["candidate_tensions"]}
        self.assertIn("anti_mainstream_divergence", tension_types)
        self.assertIn("peak_daily_burst", tension_types)
        self.assertIn("sequence_breaker_anomaly", tension_types)
        self.assertIn("near_complete_plateau", tension_types)
        self.assertIn("flow_friction_contrast", tension_types)
        self.assertIn("era_evolution_strata", tension_types)
        self.assertIn("genre_specialist_vs_tourist", tension_types)

        evidence = build_evidence(profile, signals)
        pattern_types = {record["type"] for record in evidence["patterns"]}
        self.assertIn("anti_mainstream_divergence", pattern_types)
        self.assertIn("peak_daily_burst", pattern_types)
        self.assertIn("sequence_breaker_anomaly", pattern_types)
        self.assertIn("near_complete_plateau", pattern_types)
        self.assertIn("flow_friction_contrast", pattern_types)
        self.assertIn("era_evolution_strata", pattern_types)
        self.assertIn("genre_specialist_vs_tourist", pattern_types)


if __name__ == "__main__":
    unittest.main()
