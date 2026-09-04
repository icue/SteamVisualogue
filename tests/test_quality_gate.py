from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests.current_contract_fixture import build_metadata_fixture, build_render_fixture
from steam_visualogue.io_utils import read_json, write_json  # noqa: E402
from steam_visualogue.locales import ensure_run_config  # noqa: E402
from steam_visualogue.quality_gate import (  # noqa: E402
    QualityGateError,
    finalize_quality,
    finish_quality_gate,
    get_quality_status,
    quality_state_is_current,
    start_quality_gate,
    submit_quality_result,
)
from steam_visualogue.context_budget import READER_QUALITY_PACKET_MAX_UTF8_BYTES, RESULT_MAX_UTF8_BYTES  # noqa: E402
from steam_visualogue.contact_sheet import make_contact_sheet  # noqa: E402
from steam_visualogue.exports import build_output_manifest  # noqa: E402
from steam_visualogue.render import render_deck  # noqa: E402
from steam_visualogue.validate import validate_deck  # noqa: E402


class QualityGateTests(unittest.TestCase):
    @staticmethod
    def _concrete_result(result: dict) -> dict:
        for page in result["pages"]:
            number = page["page"]
            for field, verdict in page.items():
                if field == "page" or not isinstance(verdict, dict):
                    continue
                if verdict.get("status") == "not-applicable":
                    verdict["reason"] = f"Page {number} has no comparison, so {field} is not applicable."
                else:
                    verdict["reason"] = f"Page {number} {field} matches the assigned compiled-deck evidence and visible output."
        for field, verdict in result["deck_verdict"].items():
            verdict["reason"] = f"The complete deck {field} check matches the current assigned packet."
        return result

    def _run(self, *, page_count: int = 15) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "run"
        fixture = build_metadata_fixture(Path(temporary.name) / "fixture", page_count=page_count)
        ensure_run_config(root, "en-US")
        write_json(root / "compiled-deck.json", fixture["compiled"])
        write_json(root / "publish-layout.json", fixture["layout"])
        write_json(root / "evidence.json", fixture["evidence"])
        return root

    def _render_current(self, root: Path, *, page_count: int = 15) -> None:
        fixture = build_render_fixture(root / "render-fixture", page_count=page_count)
        write_json(root / "assets-manifest.json", fixture["manifest"])
        for path in ("compiled-deck.json", "publish-layout.json", "evidence.json"):
            source = {"compiled-deck.json": fixture["compiled"], "publish-layout.json": fixture["layout"], "evidence.json": fixture["evidence"]}[path]
            write_json(root / path, source)
        pages = render_deck(fixture["layout"], fixture["assets_dir"], root / "output")
        validation = validate_deck(fixture["layout"], root / "output", check_output_manifest=False)
        self.assertTrue(validation["ok"], validation)
        write_json(root / "validation.json", validation)
        contact = make_contact_sheet(pages, root / "output" / "contact-sheet.png", layout=fixture["layout"])
        quality_contact = root / ".agent-work" / "quality" / "contact-sheet.png"
        quality_contact.parent.mkdir(parents=True, exist_ok=True)
        make_contact_sheet(pages, quality_contact, layout=fixture["layout"])
        build_output_manifest(pages, contact_sheet=contact, layout=fixture["layout"], validation=validation, destination=root / "output" / "manifest.json")

    def test_each_gate_has_one_current_bounded_attempt(self) -> None:
        root = self._run()
        reader = start_quality_gate(root, "reader")
        self.assertEqual("reader-01", reader["attempt_id"])
        packet = read_json(root / reader["assignments"][0]["packet_path"])
        self.assertIn("caption_policy", packet["rubric"])
        self.assertIn("caption_no_information_gain", packet["rubric"]["must_fix_categories"])
        self.assertEqual([list(range(1, 16))], [packet["required_page_ids"]])
        result_packet = read_json(root / ".agent-work" / "quality" / "attempts" / "reader-01" / "results" / "reader-deck.json")
        result = self._concrete_result(copy.deepcopy(result_packet))
        write_json(root / ".agent-work" / "quality" / "attempts" / "reader-01" / "results" / "reader-deck.json", result)
        self.assertEqual("accepted", submit_quality_result(root, "reader-01", "reader-deck")["status"])
        self.assertEqual("passed", finish_quality_gate(root, "reader-01")["status"])
        status = get_quality_status(root)
        self.assertEqual("passed", status["gates"]["reader"]["status"])
        self.assertTrue(status["gates"]["reader"]["current"])

    def test_failed_verdict_without_a_finding_is_a_protocol_error(self) -> None:
        root = self._run()
        start_quality_gate(root, "reader")
        result_path = root / ".agent-work" / "quality" / "attempts" / "reader-01" / "results" / "reader-deck.json"
        result = self._concrete_result(read_json(result_path))
        result["pages"][0]["headline_gain"] = {"status": "fail", "reason": "The headline repeats the visible mark."}
        write_json(result_path, result)
        submitted = submit_quality_result(root, "reader-01", "reader-deck")
        self.assertEqual("rejected", submitted["status"])
        self.assertTrue(any(issue["code"] == "verdict_finding_mismatch" for issue in submitted["validation_report"]["issues"]))

    def test_default_template_reason_is_not_a_quality_review(self) -> None:
        root = self._run()
        start_quality_gate(root, "reader")
        submitted = submit_quality_result(root, "reader-01", "reader-deck")
        self.assertEqual("rejected", submitted["status"])
        self.assertTrue(any(issue["code"] == "verdict_invalid" for issue in submitted["validation_report"]["issues"]))

    def test_malformed_result_can_be_repaired_in_the_same_attempt(self) -> None:
        root = self._run()
        started = start_quality_gate(root, "reader")
        result_path = root / started["assignments"][0]["result_path"]
        self.assertEqual("rejected", submit_quality_result(root, started["attempt_id"], "reader-deck")["status"])
        repaired = self._concrete_result(read_json(result_path))
        write_json(result_path, repaired)
        self.assertEqual("accepted", submit_quality_result(root, started["attempt_id"], "reader-deck")["status"])
        self.assertEqual("passed", finish_quality_gate(root, started["attempt_id"])["status"])
        self.assertEqual(started["attempt_id"], start_quality_gate(root, "reader")["attempt_id"])

    def test_result_budget_boundary_is_hard_and_checked_before_protocol(self) -> None:
        root = self._run()
        started = start_quality_gate(root, "reader")
        result_path = root / started["assignments"][0]["result_path"]
        result = self._concrete_result(read_json(result_path))
        result["recommended_changes"] = [""]
        write_json(result_path, result)
        base_bytes = result_path.stat().st_size
        result["recommended_changes"] = ["x" * (RESULT_MAX_UTF8_BYTES - base_bytes)]
        write_json(result_path, result)
        self.assertEqual(RESULT_MAX_UTF8_BYTES, result_path.stat().st_size)
        self.assertEqual("accepted", submit_quality_result(root, started["attempt_id"], "reader-deck")["status"])

        over_root = self._run()
        over_started = start_quality_gate(over_root, "reader")
        over_path = over_root / over_started["assignments"][0]["result_path"]
        over_result = self._concrete_result(read_json(over_path))
        over_result["recommended_changes"] = ["x" * RESULT_MAX_UTF8_BYTES]
        write_json(over_path, over_result)
        rejected = submit_quality_result(over_root, over_started["attempt_id"], "reader-deck")
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("result_budget_exceeded", rejected["validation_report"]["issues"][0]["code"])

    def test_quality_finish_enforces_the_merged_result_budget(self) -> None:
        root = self._run()
        self._render_current(root)
        started = start_quality_gate(root, "visual")
        for assignment in started["assignments"]:
            result_path = root / assignment["result_path"]
            result = self._concrete_result(read_json(result_path))
            result["recommended_changes"] = ["x" * 10_000]
            write_json(result_path, result)
            self.assertEqual("accepted", submit_quality_result(root, started["attempt_id"], assignment["packet_id"])["status"])
        with self.assertRaises(QualityGateError) as error:
            finish_quality_gate(root, started["attempt_id"])
        self.assertEqual("quality_merge_budget_exceeded", error.exception.code)

    def test_quality_finish_requires_every_assignment(self) -> None:
        root = self._run()
        self._render_current(root)
        started = start_quality_gate(root, "visual")
        self.assertEqual(3, len(started["assignments"]))
        with self.assertRaises(QualityGateError) as error:
            finish_quality_gate(root, started["attempt_id"])
        self.assertEqual("coverage_incomplete", error.exception.code)

    def test_changed_fingerprint_starts_cycle_two_and_must_fix_stops_it(self) -> None:
        root = self._run()
        first = start_quality_gate(root, "reader")
        result_path = root / first["assignments"][0]["result_path"]
        result = self._concrete_result(read_json(result_path))
        result["pages"][0]["headline_gain"] = {"status": "fail", "reason": "The headline repeats the visible comparison."}
        result["findings"] = [{
            "category": "headline_gain",
            "severity": "must-fix",
            "locations": [{"page": 1, "field": "headline_gain"}],
            "explanation": "The headline repeats the visible comparison without adding a reader-facing relation.",
            "recommendation": "Rewrite the headline to state the new relation or consequence.",
        }]
        write_json(result_path, result)
        self.assertEqual("accepted", submit_quality_result(root, first["attempt_id"], "reader-deck")["status"])
        self.assertEqual("revision-required", finish_quality_gate(root, first["attempt_id"])["status"])
        with self.assertRaises(QualityGateError) as error:
            start_quality_gate(root, "reader")
        self.assertEqual("input_unchanged", error.exception.code)

        compiled = read_json(root / "compiled-deck.json")
        compiled["pages"][0]["reader_question"] += " Updated"
        write_json(root / "compiled-deck.json", compiled)
        second = start_quality_gate(root, "reader")
        self.assertEqual("reader-02", second["attempt_id"])
        second_path = root / second["assignments"][0]["result_path"]
        second_result = self._concrete_result(read_json(second_path))
        second_result["pages"][0]["headline_gain"] = {"status": "fail", "reason": "The headline still repeats the visible comparison."}
        second_result["findings"] = result["findings"]
        write_json(second_path, second_result)
        self.assertEqual("accepted", submit_quality_result(root, second["attempt_id"], "reader-deck")["status"])
        self.assertEqual("stopped", finish_quality_gate(root, second["attempt_id"])["status"])
        with self.assertRaises(QualityGateError) as exhausted:
            start_quality_gate(root, "reader")
        self.assertEqual("quality_budget_exhausted", exhausted.exception.code)

    def test_reader_visual_and_factual_sharding_covers_both_supported_deck_sizes(self) -> None:
        for page_count, expected_counts in ((12, [6, 6]), (15, [6, 6, 3]), (18, [6, 6, 6])):
            with self.subTest(page_count=page_count):
                root = self._run(page_count=page_count)
                reader = start_quality_gate(root, "reader")
                self.assertEqual(1, len(reader["assignments"]))
                reader_packet = read_json(root / reader["assignments"][0]["packet_path"])
                self.assertEqual(list(range(1, page_count + 1)), reader_packet["required_page_ids"])
                self.assertLessEqual(
                    len(json.dumps(reader_packet, ensure_ascii=False).encode("utf-8")),
                    READER_QUALITY_PACKET_MAX_UTF8_BYTES,
                )
                self._render_current(root, page_count=page_count)
                for gate in ("visual", "factual"):
                    started = start_quality_gate(root, gate)
                    self.assertEqual(expected_counts, [len(read_json(root / assignment["packet_path"])["required_page_ids"]) for assignment in started["assignments"]])
                    self.assertEqual([f"{gate}-{index:02d}" for index in range(1, len(expected_counts) + 1)], [assignment["packet_id"] for assignment in started["assignments"]])
                    assigned = [page for assignment in started["assignments"] for page in read_json(root / assignment["packet_path"])["required_page_ids"]]
                    self.assertEqual(list(range(1, page_count + 1)), assigned)

    def test_all_three_gates_finalize_only_from_current_render_and_evidence(self) -> None:
        root = self._run()
        self._render_current(root)
        for gate in ("reader", "visual", "factual"):
            started = start_quality_gate(root, gate)
            self.assertEqual(1 if gate == "reader" else 3, len(started["assignments"]))
            packets = [read_json(root / assignment["packet_path"]) for assignment in started["assignments"]]
            if gate == "visual":
                self.assertEqual(["visual-01", "visual-02", "visual-03"], [packet["packet_id"] for packet in packets])
                self.assertEqual([6, 6, 3], [len(packet["required_page_ids"]) for packet in packets])
                self.assertTrue(all(len(packet["images"]) <= 8 for packet in packets))
            if gate == "factual":
                self.assertEqual(["factual-01", "factual-02", "factual-03"], [packet["packet_id"] for packet in packets])
                for packet in packets:
                    self.assertEqual(set(packet["allowed_evidence_ids"]), set(packet["evidence_closure"]))
                    self.assertTrue(all("presentation_content" in page for page in packet["pages"]))
                    referenced = set()
                    for page in packet["pages"]:
                        referenced.update(page.get("evidence_ids", []))
                    self.assertEqual(referenced, set(packet["allowed_evidence_ids"]))
            for assignment in started["assignments"]:
                packet_id = assignment["packet_id"]
                result_path = root / assignment["result_path"]
                result = self._concrete_result(read_json(result_path))
                write_json(result_path, result)
                self.assertEqual("accepted", submit_quality_result(root, started["attempt_id"], packet_id)["status"])
            self.assertEqual("passed", finish_quality_gate(root, started["attempt_id"])["status"])
        finalized = finalize_quality(root)
        self.assertEqual("passed", finalized["status"])
        self.assertTrue((root / "quality-state.json").is_file())
        self.assertFalse((root / ".agent-work" / "quality" / "quality-state.json").exists())
        self.assertTrue(quality_state_is_current(root))


if __name__ == "__main__":
    unittest.main()
