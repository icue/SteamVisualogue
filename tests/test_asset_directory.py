from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests.current_contract_fixture import build_render_fixture
from steam_visualogue.agent_packets import build_packet_set
from steam_visualogue.agent_results import accept_agent_result
from steam_visualogue.asset_paths import remember_assets_dir
from steam_visualogue.cache_db import CacheDB
from steam_visualogue.cli import main
from steam_visualogue.context_budget import sha256_path_hex
from steam_visualogue.io_utils import read_json, write_json
from steam_visualogue.locales import ensure_run_config
from steam_visualogue.quality_gate import start_quality_gate, submit_quality_result, finish_quality_gate
from steam_visualogue.visual_signals import build_library_palette_signals


class AssetDirectoryTests(unittest.TestCase):
    def _command(self, *args: str) -> dict:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            status = main(list(args))
        self.assertEqual(0, status, output.getvalue())
        return json.loads(output.getvalue().splitlines()[-1])

    def test_custom_assets_are_used_by_packets_merge_render_and_validate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary):
            run = Path("output/custom-run")
            ensure_run_config(run, "en-US")
            fixture = build_render_fixture(Path("external library"))
            custom_assets = fixture["assets_dir"]
            cache = CacheDB(":memory:")
            try:
                visual = build_library_palette_signals({
                    "run_id": "custom-run", "games": [],
                    "evidence_fingerprint": fixture["evidence"]["evidence_fingerprint"],
                }, cache)
            finally:
                cache.close()
            for name, document in (
                ("profile.json", {"run_id": "custom-run", "games": []}),
                ("deck-plan.json", fixture["plan"]),
                ("compiled-deck.json", fixture["compiled"]),
                ("evidence.json", fixture["evidence"]),
                ("art-direction.json", fixture["art_direction"]),
                ("visual-signals.json", visual),
            ):
                write_json(run / name, document)
            reader = start_quality_gate(run, "reader")
            assignment = reader["assignments"][0]
            result_path = run / assignment["result_path"]
            result = read_json(result_path)
            for page in result["pages"]:
                for verdict in page.values():
                    if isinstance(verdict, dict):
                        verdict["reason"] = f"Page {page['page']} matches the assigned fixture evidence and reader copy."
            for verdict in result["deck_verdict"].values():
                verdict["reason"] = "The fixture deck matches its assigned evidence and intended progression."
            write_json(result_path, result)
            self.assertEqual("accepted", submit_quality_result(run, reader["attempt_id"], assignment["packet_id"])["status"])
            self.assertEqual("passed", finish_quality_gate(run, reader["attempt_id"])["status"])

            # The downloader is replaced with an existing local image fixture;
            # the actual CLI, packet, merge, render, and validation paths run.
            with patch("steam_visualogue.assets.materialize_selected_assets", return_value=fixture["manifest"]) as materialize:
                self._command("assets", "--run-dir", str(run), "--assets-dir", str(custom_assets))
                self.assertEqual(custom_assets.resolve(), Path(materialize.call_args.args[2]).resolve())
            self.assertFalse((run / "assets" / "manifest.json").exists())

            packets = self._command("packetize", "--run-dir", str(run), "--stage", "artwork-inspection")
            manifest = read_json(packets["packet_set"])
            self.assertGreater(len(manifest["packets"]), 0)
            for row in manifest["packets"]:
                packet = read_json(run / row["path"])
                for image in packet["images"]:
                    self.assertFalse(Path(image["path"]).is_absolute())
                    self.assertTrue((run / image["path"]).is_file())
                result_path = run / ".agent-work" / "results" / f"{row['packet_id']}.json"
                write_json(result_path, {
                    "format": "steam-visualogue-agent-result", "stage": "artwork-inspection",
                    "packet_id": row["packet_id"],
                    "images": [{
                        "asset_id": image["asset_id"], "crop_safe": True, "small_size_legible": True,
                        "tone": "muted", "dominant_geometry": "rectangular color field",
                        "recommended_roles": ["Hero Game"], "rejection_reason": None,
                    } for image in packet["images"]],
                })
                self._command("accept-agent-result", "--run-dir", str(run), "--packet-set", packets["packet_set"],
                              "--packet-id", row["packet_id"], "--result", str(result_path))
            self._command("merge-agent-results", "--run-dir", str(run), "--stage", "artwork-inspection")
            self._command("build-visual-brief", "--run-dir", str(run))
            self.assertGreater(len(read_json(run / "visual-brief.json")["accepted_inspections"]), 0)
            self._command("render", "--run-dir", str(run))
            self._command("validate", "--run-dir", str(run))
            self.assertTrue(read_json(run / "validation.json")["ok"])

    def test_switching_asset_directories_invalidates_existing_artwork_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            ensure_run_config(run, "en-US")
            fixture = build_render_fixture(root / "first")
            write_json(run / "compiled-deck.json", fixture["compiled"])
            write_json(run / "evidence.json", fixture["evidence"])
            remember_assets_dir(run, fixture["assets_dir"])
            started = build_packet_set(run, "artwork-inspection", select=["game:1:portrait"])
            manifest = read_json(started["packet_set"])
            packet_id = manifest["packets"][0]["packet_id"]
            packet = read_json(run / manifest["packets"][0]["path"])

            replacement = root / "second"
            replacement.mkdir()
            source = replacement / "portrait.png"
            Image.new("RGB", (600, 900), "red").save(source)
            write_json(replacement / "manifest.json", {"assets": {"game:1:portrait": {
                "status": "ready", "path": "portrait.png", "width": 600, "height": 900,
                "source": "steam", "sha256": sha256_path_hex(source),
            }}})
            remember_assets_dir(run, replacement)
            with self.assertRaisesRegex(ValueError, "fingerprints are stale"):
                accept_agent_result(run, started["packet_set"], packet_id,
                                    ".agent-work/results/unused.json")

            refreshed = build_packet_set(run, "artwork-inspection", select=["game:1:portrait"])
            fresh_manifest = read_json(refreshed["packet_set"])
            fresh_packet = read_json(run / fresh_manifest["packets"][0]["path"])
            self.assertNotEqual(packet["images"][0]["path"], fresh_packet["images"][0]["path"])
            self.assertEqual(source.read_bytes(), (run / fresh_packet["images"][0]["path"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
