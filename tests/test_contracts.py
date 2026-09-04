from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests import SKILL_ROOT

from tests.current_contract_fixture import build_current_fixture, build_metadata_fixture
from steam_visualogue.cli import build_parser  # noqa: E402
from steam_visualogue.planning import validate_schema_document  # noqa: E402


class CurrentContractTests(unittest.TestCase):
    def test_metadata_fixture_does_not_materialize_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_metadata_fixture(Path(temporary))
            self.assertEqual([], list(fixture["assets_dir"].glob("*.png")))
        validate_schema_document("compiled-deck", "compiled-deck.schema.json", fixture["compiled"])

    def test_current_deck_and_publish_schemas_are_strictly_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
        validate_schema_document("deck-plan", "deck-plan.schema.json", fixture["plan"])
        validate_schema_document("compiled-deck", "compiled-deck.schema.json", fixture["compiled"])
        validate_schema_document("publish-layout", "publish-layout.schema.json", fixture["layout"])
        self.assertNotIn("visual_brief_fingerprint", fixture["compiled"])
        self.assertTrue(fixture["compiled"]["catalog_version"])
        self.assertTrue(fixture["compiled"]["label_fingerprint"].startswith("sha256:"))
        self.assertTrue(all(set(("card_content", "visible_text_count", "visible_image_count", "lower_anchor")) <= set(page["layout_metrics"]) for page in fixture["layout"]["pages"]))

    def test_cli_exposes_current_quality_commands(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["quality-start", "--run-dir", "run", "--gate", "reader"])
        self.assertEqual("quality-start", args.command)
        self.assertEqual("reader", args.gate)
        args = parser.parse_args(["compile-deck", "--run-dir", "run"])
        self.assertEqual("compile-deck", args.command)

    def test_current_schema_set_is_present(self) -> None:
        schema_dir = SKILL_ROOT / "references" / "schemas"
        for name in (
            "deck-plan.schema.json",
            "compiled-deck.schema.json",
            "quality-packet.schema.json",
            "quality-result.schema.json",
            "quality-state.schema.json",
            "publish-layout.schema.json",
            "output-manifest.schema.json",
        ):
            self.assertTrue((schema_dir / name).exists(), name)

    def test_deck_plan_requires_the_current_stable_format(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
        invalid = dict(fixture["plan"])
        invalid.pop("format")
        with self.assertRaises(ValueError):
            validate_schema_document("deck-plan", "deck-plan.schema.json", invalid)


if __name__ == "__main__":
    unittest.main()
