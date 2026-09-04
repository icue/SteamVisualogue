from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from steam_visualogue.cli import _stage_progress, main  # noqa: E402
from steam_visualogue.io_utils import write_json  # noqa: E402
from steam_visualogue.locales import ensure_run_config  # noqa: E402


class ProgressOutputTests(unittest.TestCase):
    def test_cli_stdout_is_progress_json_lines_followed_by_one_result(self) -> None:
        profile = json.loads(
            (ROOT / "tests" / "fixtures" / "analytics_profile.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            ensure_run_config(run_dir, "en-US")
            write_json(run_dir / "profile.json", profile)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["derive", "--run-dir", str(run_dir)])

        self.assertEqual(0, exit_code)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(row.get("event") == "progress" for row in rows[:-1]))
        self.assertEqual("ok", rows[-1].get("status"))
        self.assertNotIn("event", rows[-1])
        self.assertEqual([0, 1, 2, 3], [row["current"] for row in rows[:-1]])
        self.assertTrue(all(0 <= row["percent"] <= 100 for row in rows[:-1]))

    def test_large_progress_sequences_are_throttled_but_keep_boundaries(self) -> None:
        output = io.StringIO()
        callback = _stage_progress("enrich")
        with redirect_stdout(output):
            for current in range(101):
                callback("Enriching played-game data", current, 100)

        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(21, len(rows))
        self.assertEqual(0, rows[0]["current"])
        self.assertEqual(100, rows[-1]["current"])
        self.assertEqual(100, rows[-1]["percent"])


if __name__ == "__main__":
    unittest.main()
