from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from steam_visualogue.cli import (
    _assets,
    _build_visual_brief,
    _render,
    build_parser,
    normalize_run_dir,
)
from steam_visualogue.locales import ensure_run_config


class CliBoundaryTests(unittest.TestCase):
    def test_normalize_run_dir_anchors_relative_runs_inside_output(self) -> None:
        self.assertEqual("output", normalize_run_dir("output"))
        self.assertEqual("output/my-run", normalize_run_dir("my-run"))
        self.assertEqual("output/my-run", normalize_run_dir("output/my-run"))
        self.assertEqual("output/sub/run", normalize_run_dir("output/sub/run"))
        self.assertEqual("output/run-zh-01", normalize_run_dir("run-zh-01"))
        absolute = Path(tempfile.gettempdir()).resolve().as_posix()
        self.assertEqual(str(Path(absolute)), normalize_run_dir(absolute))

    def test_parser_normalizes_run_dir_argument(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["compile-deck", "--run-dir", "zh-deck-01"])
        self.assertEqual("output/zh-deck-01", args.run_dir)
        args_with_prefix = parser.parse_args(["compile-deck", "--run-dir", "output/zh-deck-01"])
        self.assertEqual("output/zh-deck-01", args_with_prefix.run_dir)
    def test_assets_requires_reader_gate_before_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            ensure_run_config(root, "en-US")
            args = SimpleNamespace(
                run_dir=str(root),
                cache=None,
                assets_dir=None,
                select=None,
                prune_generated=False,
                force_artwork=False,
                force_palette=False,
            )
            with self.assertRaisesRegex(ValueError, "reader quality gate"):
                _assets(args)

    def test_visual_brief_and_render_require_reader_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            ensure_run_config(root, "en-US")
            with self.assertRaisesRegex(ValueError, "reader quality gate"):
                _build_visual_brief(SimpleNamespace(run_dir=str(root)))
            with self.assertRaisesRegex(ValueError, "reader quality gate"):
                _render(SimpleNamespace(run_dir=str(root), assets_dir=None))


if __name__ == "__main__":
    unittest.main()
