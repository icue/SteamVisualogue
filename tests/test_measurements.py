from __future__ import annotations

import math
import unittest

from pathlib import Path


from steam_visualogue.measurements import (  # noqa: E402
    largest_remainder_allocation,
    measures_are_comparable,
    resolve_measure,
)


class MeasurementContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = {
            "game:10": {
                "id": "game:10",
                "facts": [
                    {"name": "playtime_minutes", "value": 125},
                    {"name": "achievement_completion", "value": 0.375},
                    {"name": "release_year", "value": 2020},
                    {"name": "release_date", "value": "2020-01-02"},
                ],
            }
        }

    def test_resolves_numeric_facts_without_accepting_author_supplied_values(self) -> None:
        hours = resolve_measure(
            {"evidence_id": "game:10", "fact": "playtime_minutes", "format": {"kind": "hours", "precision": 1}},
            self.catalog,
        )
        self.assertEqual(125, hours.raw_value)
        self.assertEqual("duration", hours.dimension)
        self.assertEqual("minutes", hours.canonical_unit)
        self.assertEqual("2.1", hours.display_value)

        percent = resolve_measure(
            {"evidence_id": "game:10", "fact": "achievement_completion", "format": {"kind": "percent", "precision": 1}},
            self.catalog,
        )
        self.assertEqual("37.5%", percent.display_value)

    def test_resolves_calendar_values_and_comparability(self) -> None:
        year = resolve_measure(
            {"evidence_id": "game:10", "fact": "release_year", "format": {"kind": "year"}},
            self.catalog,
        )
        date = resolve_measure(
            {"evidence_id": "game:10", "fact": "release_date", "format": {"kind": "date"}},
            self.catalog,
        )
        self.assertEqual("2020", year.display_value)
        self.assertEqual("2020-01-02", date.display_value)
        self.assertFalse(measures_are_comparable(year, date))

    def test_rejects_nonfinite_boolean_and_text_values(self) -> None:
        for value in (True, "125", math.inf, math.nan):
            with self.subTest(value=value):
                catalog = {"metric:x": {"id": "metric:x", "facts": [{"name": "count", "value": value}]}}
                with self.assertRaises(ValueError):
                    resolve_measure(
                        {"evidence_id": "metric:x", "fact": "count", "format": {"kind": "integer"}},
                        catalog,
                    )

    def test_largest_remainder_is_stable_and_sums_to_one_hundred(self) -> None:
        allocation, remainder = largest_remainder_allocation([1, 1, 1], 3)
        self.assertEqual([34, 33, 33], allocation)
        self.assertEqual(0, remainder)
        allocation, remainder = largest_remainder_allocation([2, 1], 5)
        self.assertEqual(100, sum(allocation) + remainder)
        self.assertEqual([40, 20], allocation)
        self.assertEqual(40, remainder)


if __name__ == "__main__":
    unittest.main()
