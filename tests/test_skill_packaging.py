from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import SKILL_ROOT
from tests.current_contract_fixture import build_metadata_fixture
from steam_visualogue.editorial_deck import deck_schema_fingerprint
from steam_visualogue.io_utils import write_json
from steam_visualogue.locales import ensure_run_config
from steam_visualogue.semantic_candidates import achievement_analysis_contract_fingerprint


class SkillPackagingTests(unittest.TestCase):
    def test_standalone_skill_resolves_resources_and_runs_from_another_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "installed skills" / "steam-visualogue"
            shutil.copytree(SKILL_ROOT, installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            workspace = root / "report workspace"
            workspace.mkdir()
            probe = subprocess.run(
                [sys.executable, "-B", "-c", "\n".join([
                    "import json, sys",
                    "from pathlib import Path",
                    "sys.path.insert(0, sys.argv[1])",
                    "from steam_visualogue.editorial_deck import deck_schema_fingerprint",
                    "from steam_visualogue.semantic_candidates import achievement_analysis_contract_fingerprint",
                    "from steam_visualogue.credentials import credential_path, api_coordination_path",
                    "from steam_visualogue.cli import _default_cache_path",
                    "print(json.dumps({",
                    "    'deck': deck_schema_fingerprint(),",
                    "    'analysis': achievement_analysis_contract_fingerprint(),",
                    "    'state_roots': [str(path.parent) for path in (credential_path(), api_coordination_path(), _default_cache_path())],",
                    "}))",
                ]), str(installed / "scripts")],
                cwd=workspace, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(0, probe.returncode, probe.stderr)
            resources = json.loads(probe.stdout)
            self.assertEqual(deck_schema_fingerprint(), resources["deck"])
            self.assertEqual(achievement_analysis_contract_fingerprint(), resources["analysis"])
            self.assertEqual([str(workspace)] * 3, resources["state_roots"])

            fixture = build_metadata_fixture(root / "fixture")
            run_dir = workspace / "output" / "packaging-smoke"
            ensure_run_config(run_dir, "en-US")
            for filename, key in (
                ("compiled-deck.json", "compiled"),
                ("publish-layout.json", "layout"),
                ("evidence.json", "evidence"),
            ):
                write_json(run_dir / filename, fixture[key])
            started = subprocess.run(
                [sys.executable, "-B", str(installed / "scripts" / "run.py"),
                 "quality-start", "--run-dir", "packaging-smoke", "--gate", "reader"],
                cwd=workspace, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(0, started.returncode, started.stderr + started.stdout)
            assignment = json.loads(started.stdout)["assignments"][0]
            self.assertTrue((run_dir / assignment["packet_path"]).is_file())
            command = shlex.split(assignment["submit_command"].replace("<run>", "packaging-smoke"))
            command[0] = sys.executable
            submitted = subprocess.run(
                command, cwd=workspace, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(2, submitted.returncode, submitted.stderr + submitted.stdout)
            self.assertEqual("rejected", json.loads(submitted.stdout)["status"])
            self.assertFalse((installed / "output").exists())
            self.assertEqual([], list(installed.glob(".steam-visualogue*")))


if __name__ == "__main__":
    unittest.main()
