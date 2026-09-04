from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests.current_contract_fixture import _visual_brief, atlas_plan_and_evidence, build_current_fixture
from steam_visualogue.contact_sheet import make_contact_sheet  # noqa: E402
from steam_visualogue.exports import build_output_manifest, export_story_markdown  # noqa: E402
from steam_visualogue.io_utils import read_json  # noqa: E402
from steam_visualogue.editorial_deck import EditorialDeckError, compile_editorial_deck  # noqa: E402
from steam_visualogue.fingerprint import compute_asset_manifest_fingerprint, compute_visual_brief_fingerprint  # noqa: E402
from steam_visualogue.planning import validate_schema_document  # noqa: E402
from steam_visualogue.publish_layout import PublishLayoutError, compose_publish_layout, font_family  # noqa: E402
from steam_visualogue.render import render_deck  # noqa: E402
from steam_visualogue.validate import validate_deck  # noqa: E402


class PublishRenderingTests(unittest.TestCase):
    def test_visual_brief_is_required_and_bound_to_compiled_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
            with self.assertRaises(PublishLayoutError) as error:
                compose_publish_layout(fixture["compiled"], fixture["art_direction"], fixture["manifest"])
            self.assertEqual("visual_brief_required", error.exception.code)

            stale_compiled = copy.deepcopy(fixture["visual_brief"])
            stale_compiled["compiled_deck_fingerprint"] = "sha256:" + "2" * 64
            stale_compiled["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(stale_compiled)
            with self.assertRaises(PublishLayoutError) as error:
                compose_publish_layout(fixture["compiled"], fixture["art_direction"], fixture["manifest"], stale_compiled)
            self.assertEqual("visual_brief_compiled_stale", error.exception.code)

            stale_assets = copy.deepcopy(fixture["visual_brief"])
            stale_assets["asset_manifest_fingerprint"] = "sha256:" + "3" * 64
            stale_assets["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(stale_assets)
            with self.assertRaises(PublishLayoutError) as error:
                compose_publish_layout(fixture["compiled"], fixture["art_direction"], fixture["manifest"], stale_assets)
            self.assertEqual("visual_brief_assets_stale", error.exception.code)

    def test_visual_brief_changes_layout_inputs_without_changing_compiled_deck(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
            changed_brief = copy.deepcopy(fixture["visual_brief"])
            changed_brief["library_palette"] = {"primary": "#ffffff"}
            changed_brief["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(changed_brief)
            changed_layout = compose_publish_layout(fixture["compiled"], fixture["art_direction"], fixture["manifest"], changed_brief)
        self.assertEqual(fixture["compiled"]["compiled_deck_fingerprint"], changed_layout["compiled_deck_fingerprint"])
        self.assertNotEqual(fixture["layout"]["layout_input_fingerprint"], changed_layout["layout_input_fingerprint"])

    def test_each_current_layout_input_changes_layout_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
            baseline = fixture["layout"]["layout_input_fingerprint"]

            changed_direction = copy.deepcopy(fixture["art_direction"])
            changed_direction["density"] = "open-with-anchored-lower-field"
            direction_layout = compose_publish_layout(fixture["compiled"], changed_direction, fixture["manifest"], fixture["visual_brief"])

            changed_brief = copy.deepcopy(fixture["visual_brief"])
            changed_brief["library_palette"] = {"primary": "#ffffff"}
            changed_brief["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(changed_brief)
            brief_layout = compose_publish_layout(fixture["compiled"], fixture["art_direction"], fixture["manifest"], changed_brief)

            changed_assets = copy.deepcopy(fixture["manifest"])
            changed_assets["assets"]["game:1:portrait"]["width"] = 601
            changed_asset_brief = copy.deepcopy(fixture["visual_brief"])
            changed_asset_brief["asset_manifest_fingerprint"] = compute_asset_manifest_fingerprint(changed_assets)
            for row in changed_asset_brief["candidate_assets"]:
                if row["asset_id"] == "game:1:portrait":
                    row["width"] = 601
                    row["aspect_ratio"] = round(601 / row["height"], 6)
            changed_asset_brief["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(changed_asset_brief)
            asset_layout = compose_publish_layout(fixture["compiled"], fixture["art_direction"], changed_assets, changed_asset_brief)

            changed_plan = copy.deepcopy(fixture["plan"])
            changed_plan["title"] = "A Broad Shelf, Selective Depth — revised"
            changed_compiled = compile_editorial_deck(changed_plan, {"findings": []}, fixture["evidence"], None)
            compiled_brief = _visual_brief(changed_compiled, fixture["manifest"])
            compiled_layout = compose_publish_layout(changed_compiled, fixture["art_direction"], fixture["manifest"], compiled_brief)

        self.assertEqual(5, len({
            direction_layout["layout_input_fingerprint"],
            brief_layout["layout_input_fingerprint"],
            asset_layout["layout_input_fingerprint"],
            compiled_layout["layout_input_fingerprint"],
            baseline,
        }))

    def test_layout_rejects_assets_outside_visual_brief_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
            brief = copy.deepcopy(fixture["visual_brief"])
            brief["candidate_assets"] = [row for row in brief["candidate_assets"] if row["asset_id"] != "game:2:portrait"]
            brief["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(brief)
            with self.assertRaises(PublishLayoutError) as error:
                compose_publish_layout(fixture["compiled"], fixture["art_direction"], fixture["manifest"], brief)
            self.assertEqual("asset_not_candidate", error.exception.code)

    def test_zh_cn_fails_without_a_cjk_font(self) -> None:
        with patch("steam_visualogue.publish_layout._font_candidates", return_value=[]):
            with self.assertRaises(PublishLayoutError) as error:
                font_family("zh-CN")
        self.assertEqual("cjk_font_missing", error.exception.code)

    def test_render_export_and_validation_share_the_current_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = build_current_fixture(root)
            output = root / "output"
            pages = render_deck(fixture["layout"], fixture["assets_dir"], output)
            first = validate_deck(fixture["layout"], output, check_output_manifest=False)
            self.assertTrue(first["ok"])
            contact = make_contact_sheet(pages, output / "contact-sheet.png", layout=fixture["layout"])
            reader_export = export_story_markdown(fixture["compiled"], output / "story.md")
            manifest = build_output_manifest(pages, contact_sheet=contact, layout=fixture["layout"], validation=first, destination=output / "manifest.json")
            self.assertTrue(Path(reader_export).is_file())
            self.assertTrue(Path(manifest).is_file())
            public_manifest = read_json(manifest)
            validate_schema_document("output manifest", "output-manifest.schema.json", public_manifest)
            self.assertEqual(len(public_manifest["pages"]), len(public_manifest["page_semantics"]))
            self.assertNotIn("reader_title", public_manifest)
            final = validate_deck(fixture["layout"], output)
            self.assertTrue(final["ok"], final)
            self.assertEqual([f"{index:02d}.png" for index in range(1, 16)], [Path(path).name for path in pages])
            self.assertEqual("comparison-bars", fixture["layout"]["pages"][3]["composition"])
            self.assertEqual("comparison-equal-cards", fixture["layout"]["pages"][4]["composition"])
            with Image.open(pages[0]) as rendered_page:
                self.assertEqual((1080, 1440), rendered_page.size)

    def test_perspective_cover_margins_preserve_page_background(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = build_current_fixture(root)
            pages = render_deck(fixture["layout"], fixture["assets_dir"], root / "output")

            with Image.open(pages[3]) as rendered_page:
                black_outside = 0
                for x, y, perspective in ((80, 600, "left_wall"), (780, 600, "right_wall")):
                    for local_y in range(440):
                        for local_x in range(220):
                            edge = 52.8 * local_x / 220 if perspective == "left_wall" else 52.8 * (1 - local_x / 220)
                            if not (edge <= local_y <= 440 - edge) and rendered_page.getpixel((x + local_x, y + local_y)) == (0, 0, 0):
                                black_outside += 1
                self.assertEqual(0, black_outside)

    def test_publish_surface_has_no_visible_machine_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
        for page in fixture["layout"]["pages"]:
            visible = " ".join(str(item.get("text", "")) for item in page["elements"] if item.get("type") == "text")
            self.assertNotIn("sha256", visible)
            self.assertNotIn("claim:", visible)
            self.assertNotIn("evidence", visible.lower())
            self.assertNotIn("role", visible.lower())
            self.assertEqual([], [item for item in page["elements"] if item.get("semantic_role") == "caption"])
            headline = page["reader_title"]
            self.assertLessEqual(visible.count(headline), 1)

    def test_layout_adapts_low_resolution_assets_and_unsafe_equal_comparisons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))

            low_resolution_assets = copy.deepcopy(fixture["manifest"])
            low_resolution_assets["assets"]["game:1:portrait"].update({"width": 101, "height": 101})
            low_resolution_brief = copy.deepcopy(fixture["visual_brief"])
            low_resolution_brief["asset_manifest_fingerprint"] = compute_asset_manifest_fingerprint(low_resolution_assets)
            low_resolution_brief["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(low_resolution_brief)
            low_resolution_layout = compose_publish_layout(fixture["compiled"], fixture["art_direction"], low_resolution_assets, low_resolution_brief)
            hero_image = next(
                item for item in low_resolution_layout["pages"][1]["elements"] if item.get("type") == "image"
            )
            self.assertLessEqual(float(hero_image["scale_factor"]), 1.5)
            self.assertEqual("contain", hero_image["treatment"])

            unsafe_equal_assets = copy.deepcopy(fixture["manifest"])
            for asset_id in ("game:4:header", "game:5:header"):
                unsafe_equal_assets["assets"][asset_id].update({"width": 2000, "height": 100})
            stacked_brief = copy.deepcopy(fixture["visual_brief"])
            stacked_brief["asset_manifest_fingerprint"] = compute_asset_manifest_fingerprint(unsafe_equal_assets)
            stacked_brief["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(stacked_brief)
            stacked_layout = compose_publish_layout(fixture["compiled"], fixture["art_direction"], unsafe_equal_assets, stacked_brief)
            cards = [
                item
                for item in stacked_layout["pages"][4]["elements"]
                if str(item.get("id", "")).startswith("card-")
            ]
            self.assertEqual(2, len(cards))
            self.assertEqual(cards[0]["x"], cards[1]["x"])
            self.assertGreater(cards[1]["y"], cards[0]["y"])

    def test_qualitative_comparison_rejects_portrait_subject_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
            fixture["compiled"]["pages"][4]["presentation"]["content"]["items"][0]["subject"]["asset_id"] = "game:4:portrait"
            with self.assertRaises(PublishLayoutError) as error:
                compose_publish_layout(fixture["compiled"], fixture["art_direction"], fixture["manifest"], fixture["visual_brief"])
            self.assertEqual("comparison_landscape_asset_required", error.exception.code)

    def test_atlas_requires_portrait_assets_and_uses_tall_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
            atlas_plan, atlas_evidence = atlas_plan_and_evidence()
            atlas_plan["pages"][1]["presentation"]["content"]["items"][0]["note"] = "internal note must not become visible"
            atlas_deck = compile_editorial_deck(atlas_plan, {"findings": []}, atlas_evidence, None)
            atlas_brief = _visual_brief(atlas_deck, fixture["manifest"])
            layout = compose_publish_layout(atlas_deck, fixture["art_direction"], fixture["manifest"], atlas_brief)
            atlas_page_layout = layout["pages"][1]
            images = [item for item in atlas_page_layout["elements"] if item.get("type") == "image"]
            cards = [item for item in atlas_page_layout["elements"] if str(item.get("id", "")).startswith("atlas-card-")]
            self.assertEqual(3, len(images))
            self.assertTrue(all((item["w"], item["h"]) == (210, 295) for item in images))
            self.assertTrue(all(item["h"] > item["w"] for item in images))
            self.assertTrue(all(card["h"] >= 60 for card in cards))
            self.assertNotIn("note", " ".join(str(item.get("text", "")) for item in atlas_page_layout["elements"]))

            invalid_atlas_plan = copy.deepcopy(atlas_plan)
            invalid_atlas_plan["pages"][1]["presentation"]["content"]["items"][0]["subject"]["asset_id"] = "game:1:header"
            with self.assertRaises(EditorialDeckError) as error:
                compile_editorial_deck(invalid_atlas_plan, {"findings": []}, atlas_evidence, None)
            self.assertEqual("atlas_portrait_asset_required", error.exception.code)

            landscape_assets = copy.deepcopy(fixture["manifest"])
            landscape_assets["assets"]["game:1:portrait"].update({"width": 900, "height": 600})
            landscape_brief = copy.deepcopy(atlas_brief)
            landscape_brief["asset_manifest_fingerprint"] = compute_asset_manifest_fingerprint(landscape_assets)
            landscape_brief["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(landscape_brief)
            with self.assertRaises(PublishLayoutError) as error:
                compose_publish_layout(atlas_deck, fixture["art_direction"], landscape_assets, landscape_brief)
            self.assertEqual("atlas_portrait_asset_required", error.exception.code)

    def test_default_final_output_is_1080_by_1440(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
            direction = copy.deepcopy(fixture["art_direction"])
            direction.pop("final_size")
            layout = compose_publish_layout(fixture["compiled"], direction, fixture["manifest"], fixture["visual_brief"])
            self.assertEqual([1080, 1440], layout["working_size"])
            self.assertEqual([1080, 1440], layout["final_size"])

    def test_single_game_page_adapts_cover_palette_and_accent_rule_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
            layout = fixture["layout"]
            # Verify accent-rule mark is completely absent from all pages
            for page in layout["pages"]:
                mark_ids = [item.get("id") for item in page["elements"] if item.get("type") == "mark"]
                self.assertNotIn("accent-rule", mark_ids)

            # Hero page with single game artwork (e.g. Page 2) should adapt its background
            hero_page = layout["pages"][1]  # 0-indexed page 2
            self.assertIn("background", hero_page)
            self.assertTrue(hero_page["background"].startswith("#"))

    def test_quantitative_comparison_and_anomaly_aspect_ratios(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_current_fixture(Path(temporary))
            layout = fixture["layout"]
            # Page 4 is quantitative-comparison in the current fixture
            comp_page = layout["pages"][3]
            comp_images = [item for item in comp_page["elements"] if item.get("type") == "image"]
            self.assertEqual(2, len(comp_images))
            for img in comp_images:
                self.assertEqual(220, img["w"])
                self.assertEqual(440, img["h"])
                self.assertAlmostEqual(220 / 440, img["w"] / img["h"], places=4)

            # Test achievement anomaly layout dimensions and portrait requirement
            anomaly_plan = copy.deepcopy(fixture["plan"])
            anomaly_evidence = copy.deepcopy(fixture["evidence"])
            anomaly_evidence["achievements"].append({"id": "achievement:5:FIRST_WIN", "type": "achievement", "facts": [{"name": "name", "value": "First Win"}]})
            anomaly_plan["pages"][11]["claim"]["evidence_ids"] = ["game:5", "achievement:5:FIRST_WIN"]
            anomaly_plan["pages"][11]["presentation"] = {
                "kind": "achievement-anomaly",
                "content": {
                    "item": {
                        "subject": {
                            "game_id": "game:5",
                            "asset_id": "game:5:portrait",
                        },
                        "achievement": {
                            "achievement_id": "achievement:5:FIRST_WIN",
                        },
                        "evidence_ids": ["game:5", "achievement:5:FIRST_WIN"],
                    }
                }
            }
            anomaly_compiled = compile_editorial_deck(anomaly_plan, {"findings": []}, anomaly_evidence, None)
            anomaly_brief = _visual_brief(anomaly_compiled, fixture["manifest"])
            anomaly_layout = compose_publish_layout(anomaly_compiled, fixture["art_direction"], fixture["manifest"], anomaly_brief)
            anomaly_page = anomaly_layout["pages"][11]
            anomaly_images = [item for item in anomaly_page["elements"] if item.get("type") == "image"]
            self.assertEqual(1, len(anomaly_images))
            self.assertEqual(320, anomaly_images[0]["w"])
            self.assertEqual(480, anomaly_images[0]["h"])
            self.assertAlmostEqual(2 / 3, anomaly_images[0]["w"] / anomaly_images[0]["h"], places=4)


if __name__ == "__main__":
    unittest.main()
