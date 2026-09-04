from __future__ import annotations

import copy
import unittest

from tests.current_contract_fixture import atlas_plan_and_evidence, constellation_plan, current_plan_and_evidence
from steam_visualogue.editorial_deck import EditorialDeckError, compile_editorial_deck  # noqa: E402
from steam_visualogue.planning import validate_schema_document  # noqa: E402


class EditorialDeckTests(unittest.TestCase):
    def test_both_modes_compile_in_english_and_chinese(self) -> None:
        for locale in ("en-US", "zh-CN"):
            plan, evidence = current_plan_and_evidence(locale)
            for candidate in (plan, constellation_plan(locale)):
                compiled = compile_editorial_deck(candidate, {"findings": []}, evidence, None)
                self.assertEqual(locale, compiled["locale"])
                self.assertTrue(compiled["reader_audit"]["passed"])

    def test_thesis_plan_compiles_to_the_current_contract(self) -> None:
        plan, evidence = current_plan_and_evidence()
        compiled = compile_editorial_deck(plan, {"findings": []}, evidence, None)
        validate_schema_document("compiled-deck", "compiled-deck.schema.json", compiled)
        self.assertEqual("steam-visualogue-compiled-deck", compiled["format"])
        self.assertEqual(15, len(compiled["pages"]))
        self.assertEqual("renderer", compiled["visible_identity_owner"]["4"])
        self.assertTrue(compiled["reader_audit"]["passed"])
        self.assertTrue(all("role" not in page["reader_copy"] for page in compiled["pages"]))

    def test_caption_is_opt_in_and_must_add_information(self) -> None:
        plan, evidence = current_plan_and_evidence()

        invalid = copy.deepcopy(plan)
        invalid["pages"][2]["reader_copy"]["caption"] = "The marks show the completion bands."
        with self.assertRaises(EditorialDeckError) as context:
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)
        self.assertEqual("caption_opt_in_required", context.exception.code)

        invalid = copy.deepcopy(plan)
        invalid["pages"][2]["reader_copy"].update({
            "caption": "The marks show the completion bands.",
            "caption_required": True,
        })
        with self.assertRaises(EditorialDeckError) as context:
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)
        self.assertEqual("caption_reason_missing", context.exception.code)

        invalid = copy.deepcopy(plan)
        invalid["pages"][2]["reader_copy"].update({
            "caption_required": True,
            "caption_reason": "需要说明图片",
        })
        with self.assertRaises(EditorialDeckError) as context:
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)
        self.assertEqual("caption_required_without_caption", context.exception.code)

        invalid = copy.deepcopy(plan)
        invalid["pages"][2]["reader_copy"].update({
            "caption": "The marks show the completion bands.",
            "caption_required": True,
            "caption_reason": "需要说明图片",
        })
        with self.assertRaises(EditorialDeckError) as context:
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)
        self.assertEqual("caption_reason_generic", context.exception.code)

        invalid = copy.deepcopy(plan)
        invalid["pages"][2]["reader_copy"].update({
            "caption": invalid["pages"][2]["reader_copy"]["headline"],
            "caption_required": True,
            "caption_reason": "The visual encoding is not self-explanatory without this interpretation.",
        })
        with self.assertRaises(EditorialDeckError) as context:
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)
        self.assertEqual("caption_no_information_gain", context.exception.code)

        valid = copy.deepcopy(plan)
        valid["pages"][2]["reader_copy"].update({
            "caption": "The mark density encodes completion bands that are not labeled elsewhere.",
            "caption_required": True,
            "caption_reason": "The image uses an unlabeled density encoding, so readers need its meaning to interpret the visual.",
        })
        compiled = compile_editorial_deck(valid, {"findings": []}, evidence, None)
        validate_schema_document("compiled-deck", "compiled-deck.schema.json", compiled)
        reader_copy = compiled["pages"][2]["reader_copy"]
        self.assertEqual("The mark density encodes completion bands that are not labeled elsewhere.", reader_copy["caption"])
        self.assertNotIn("caption_required", reader_copy)
        self.assertNotIn("caption_reason", reader_copy)

    def test_constellation_mode_requires_and_tracks_clusters(self) -> None:
        plan, evidence = current_plan_and_evidence()
        compiled = compile_editorial_deck(constellation_plan(), {"findings": []}, evidence, None)
        self.assertEqual("constellation-led", compiled["mode"])
        self.assertEqual("cluster:time", compiled["pages"][1]["claim"]["cluster_id"])

        invalid = constellation_plan()
        del invalid["pages"][8]["claim"]["cluster_id"]
        with self.assertRaisesRegex(EditorialDeckError, "cluster"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

        invalid = constellation_plan()
        for page in invalid["pages"][7:-1]:
            page["claim"]["cluster_id"] = "cluster:time"
        with self.assertRaisesRegex(EditorialDeckError, "cluster"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

        invalid = constellation_plan()
        invalid["pages"][7]["claim"]["develops"] = []
        with self.assertRaisesRegex(EditorialDeckError, "transition"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

    def test_page_claim_must_develop_the_thesis(self) -> None:
        plan, evidence = current_plan_and_evidence()
        invalid = copy.deepcopy(plan)
        invalid["pages"][2]["claim"]["develops"] = []
        with self.assertRaises(EditorialDeckError) as context:
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)
        self.assertEqual("claim_not_connected", context.exception.code)

    def test_comparison_contract_and_reader_identity_are_enforced(self) -> None:
        plan, evidence = current_plan_and_evidence()
        invalid = copy.deepcopy(plan)
        del invalid["pages"][3]["presentation"]["content"]["shared_question"]
        with self.assertRaisesRegex(EditorialDeckError, "comparison"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

        invalid = copy.deepcopy(plan)
        invalid["pages"][4]["presentation"]["content"]["items"][0]["statement"] = "Game 4 returns to the same rhythm."
        with self.assertRaisesRegex(EditorialDeckError, "qualitative"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

        invalid = copy.deepcopy(plan)
        invalid["pages"][4]["presentation"]["content"]["items"][0]["subject"]["asset_id"] = "game:4:portrait"
        with self.assertRaises(EditorialDeckError) as context:
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)
        self.assertEqual("comparison_landscape_asset_required", context.exception.code)

        invalid = copy.deepcopy(plan)
        invalid["pages"][3]["reader_copy"]["headline"] = "The taller bar wins."
        with self.assertRaisesRegex(EditorialDeckError, "headline"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

    def test_bilingual_reader_audit_rejects_catalog_and_backstage_copy(self) -> None:
        plan, evidence = current_plan_and_evidence()
        invalid = copy.deepcopy(plan)
        invalid["pages"][1]["reader_copy"]["headline"] = "Game 1 is listed under RPG."
        with self.assertRaisesRegex(EditorialDeckError, "headline"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

        chinese, evidence = current_plan_and_evidence("zh-CN")
        invalid = copy.deepcopy(chinese)
        invalid["pages"][5]["reader_copy"]["support"] = "这页来自候选挑战中。"
        with self.assertRaisesRegex(EditorialDeckError, "backstage"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

        invalid = copy.deepcopy(chinese)
        invalid["pages"][0]["reader_copy"]["headline"] = "这里没有单一中心。"
        invalid["pages"][-1]["reader_copy"]["headline"] = "整个书架没有单一中心。"
        with self.assertRaisesRegex(EditorialDeckError, "opening and closing"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

    def test_closing_must_synthesize_two_prior_claims(self) -> None:
        plan, evidence = current_plan_and_evidence()
        invalid = copy.deepcopy(plan)
        invalid["pages"][-1]["claim"]["develops"] = ["claim:14"]
        with self.assertRaises(EditorialDeckError) as context:
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)
        self.assertEqual("closing_not_synthesis", context.exception.code)

    def test_atlas_semantics_fail_at_the_compiler_boundary(self) -> None:
        plan, evidence = atlas_plan_and_evidence()

        invalid = copy.deepcopy(plan)
        invalid["pages"][1]["presentation"]["content"]["items"] = invalid["pages"][1]["presentation"]["content"]["items"][:2]
        with self.assertRaisesRegex(EditorialDeckError, "exactly three or four"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

        invalid = copy.deepcopy(plan)
        invalid["pages"][1]["presentation"]["content"]["items"][0]["statement"] = ""
        with self.assertRaisesRegex(EditorialDeckError, "non-empty statement"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

        invalid = copy.deepcopy(plan)
        invalid["pages"][1]["presentation"]["content"]["items"][0]["evidence_ids"] = ["game:4"]
        with self.assertRaisesRegex(EditorialDeckError, "outside the page closure"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

        invalid = copy.deepcopy(plan)
        invalid["pages"][1]["presentation"]["content"]["items"][0]["subject"]["game_id"] = "game:2"
        invalid["pages"][1]["presentation"]["content"]["items"][0]["subject"]["asset_id"] = "game:2:portrait"
        with self.assertRaisesRegex(EditorialDeckError, "unique game subjects"):
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

        invalid = copy.deepcopy(plan)
        invalid["pages"][1]["presentation"]["content"]["items"][0]["measure"]["fact"] = "achievement_completion"
        with self.assertRaisesRegex(EditorialDeckError, "dimension and canonical unit"):
            invalid["pages"][1]["presentation"]["content"]["items"][0]["measure"]["format"] = {"kind": "percent", "precision": 1}
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)

    def test_achievement_anomaly_requires_portrait_asset(self) -> None:
        plan, evidence = current_plan_and_evidence()
        evidence = copy.deepcopy(evidence)
        evidence["achievements"].append({"id": "achievement:1:FIRST_WIN", "type": "achievement", "facts": [{"name": "name", "value": "First Win"}]})
        invalid = copy.deepcopy(plan)
        invalid["pages"][11]["presentation"] = {
            "kind": "achievement-anomaly",
            "content": {
                "item": {
                    "subject": {
                        "game_id": "game:1",
                        "asset_id": "game:1:header",
                    },
                    "achievement": {
                        "achievement_id": "achievement:1:FIRST_WIN",
                    },
                    "evidence_ids": ["game:1"],
                }
            }
        }
        invalid["pages"][11]["claim"]["evidence_ids"] = ["game:1"]
        with self.assertRaises(EditorialDeckError) as context:
            compile_editorial_deck(invalid, {"findings": []}, evidence, None)
        self.assertEqual("anomaly_portrait_asset_required", context.exception.code)


if __name__ == "__main__":
    unittest.main()
