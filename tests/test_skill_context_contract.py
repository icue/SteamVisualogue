from __future__ import annotations

import unittest
import re

from tests import SKILL_ROOT


class SkillContextContractTests(unittest.TestCase):
    def test_skill_links_workflow_and_existing_references(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        linked_paths = re.findall(r"\]\((references/[^)]+\.md)\)", text)
        self.assertIn("references/workflow.md", linked_paths)
        for link in set(linked_paths):
            self.assertTrue((SKILL_ROOT / link).is_file(), link)

    def test_skill_does_not_duplicate_the_workflow(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("compile-deck -> assets", text)
        self.assertNotIn("quality-start(reader)", text)
        self.assertIn("references/workflow.md", text)

    def test_skill_names_current_entry_artifacts_and_gate_commands(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        for name in ("deck-plan.json", "compiled-deck.json", "publish-layout.json", "quality-state.json"):
            self.assertIn(name, text)
        for command in ("compile-deck", "quality-start", "quality-submit", "quality-finish", "quality-status", "finalize-quality"):
            self.assertIn(command, text)
        self.assertNotIn("severity_reason", text)
        self.assertNotIn("quality-state.json` under", text)

    def test_authoritative_references_do_not_reintroduce_retired_quality_terms(self) -> None:
        text = "\n".join(path.read_text(encoding="utf-8") for path in (SKILL_ROOT / "references").glob("*.md"))
        for term in ("minor", "major", "blocking", "severity_reason"):
            self.assertNotIn(term, text)


if __name__ == "__main__":
    unittest.main()
