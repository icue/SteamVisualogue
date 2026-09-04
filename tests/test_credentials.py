from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import chdir

from tests import REPO_ROOT, SKILL_ROOT

from steam_visualogue.credentials import (  # noqa: E402
    CREDENTIAL_FILE_NAME,
    CredentialFileError,
    api_coordination_path,
    credential_path,
    load_steam_api_key,
    skill_root,
)
from steam_visualogue.cli import _default_cache_path, _resolve_collect_locale, build_parser  # noqa: E402
from steam_visualogue.io_utils import write_json
from steam_visualogue.locales import SETTINGS_FILE_NAME, SETTINGS_FORMAT


VALID_KEY = "0123456789ABCDEF0123456789ABCDEF"


class CredentialTests(unittest.TestCase):
    def test_skill_resources_and_workspace_private_state_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary):
            workspace = Path(temporary).resolve()
            self.assertEqual(SKILL_ROOT, skill_root())
            self.assertEqual(workspace / CREDENTIAL_FILE_NAME, credential_path())
            self.assertEqual(workspace / ".steam-visualogue-cache.sqlite", _default_cache_path())
            self.assertEqual(workspace / ".steam-visualogue-api-coordination.sqlite", api_coordination_path())
            (workspace / CREDENTIAL_FILE_NAME).write_text(f"STEAM_API_KEY={VALID_KEY}\n", encoding="utf-8")
            self.assertEqual(VALID_KEY, load_steam_api_key())
            write_json(workspace / SETTINGS_FILE_NAME, {
                "format": SETTINGS_FORMAT,
                "default_report_locale": "zh-CN",
            })
            args = build_parser().parse_args(["collect", "--identity", "76561198000000000", "--run-dir", "run"])
            self.assertEqual("zh-CN", _resolve_collect_locale(args))

    def test_reads_one_strict_assignment_with_comments_and_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / CREDENTIAL_FILE_NAME).write_text(
                f"\ufeff# Local only\n\nSTEAM_API_KEY = {VALID_KEY}\n",
                encoding="utf-8",
            )
            self.assertEqual(VALID_KEY, load_steam_api_key(root))

    def test_environment_variable_is_never_a_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"STEAM_API_KEY": VALID_KEY}):
                with self.assertRaisesRegex(CredentialFileError, "Missing local credential"):
                    load_steam_api_key(temporary)

    def test_cache_defaults_to_workspace_and_ignores_process_configuration(self) -> None:
        elsewhere = str(Path(tempfile.gettempdir()) / "should-not-be-used.sqlite")
        with patch.dict(os.environ, {"STEAM_VISUALOGUE_CACHE": elsewhere}):
            self.assertEqual(
                Path.cwd() / ".steam-visualogue-cache.sqlite",
                _default_cache_path(),
            )

    def test_every_cache_aware_command_uses_the_same_default(self) -> None:
        expected = str(Path.cwd() / ".steam-visualogue-cache.sqlite")
        commands = [
            ["collect", "--identity", "76561198000000000", "--run-dir", "run"],
            ["enrich", "--run-dir", "run"],
            ["palette", "--run-dir", "run"],
            ["assets", "--run-dir", "run"],
            ["reuse-editorial", "--run-dir", "run"],
            ["commit-reuse", "--run-dir", "run"],
            ["purge-user-cache", "--steamid", "76561198000000000"],
        ]
        parser = build_parser()
        for argv in commands:
            with self.subTest(command=argv[0]):
                self.assertEqual(expected, parser.parse_args(argv).cache)

    def test_gitignore_covers_cache_database_and_sqlite_sidecars(self) -> None:
        rules = {
            line.strip()
            for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(
            {
                "/.steam-visualogue-cache.sqlite",
                "/.steam-visualogue-cache.sqlite-journal",
                "/.steam-visualogue-cache.sqlite-shm",
                "/.steam-visualogue-cache.sqlite-wal",
            }.issubset(rules)
        )
        self.assertIn("/.steam-visualogue-settings.json", rules)
        self.assertIn("/.codex/config.toml", rules)
        self.assertIn("/output/", rules)

    def test_rejects_unknown_duplicate_quoted_and_malformed_values_without_echoing(self) -> None:
        invalid_documents = [
            f"OTHER={VALID_KEY}\n",
            f"STEAM_API_KEY={VALID_KEY}\nSTEAM_API_KEY={VALID_KEY}\n",
            f'STEAM_API_KEY="{VALID_KEY}"\n',
            "STEAM_API_KEY=not-a-steam-key\n",
            VALID_KEY,
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / CREDENTIAL_FILE_NAME
            for document in invalid_documents:
                with self.subTest(document=document):
                    source.write_text(document, encoding="utf-8")
                    with self.assertRaises(CredentialFileError) as caught:
                        load_steam_api_key(temporary)
                    self.assertNotIn(VALID_KEY, str(caught.exception))
                    self.assertNotIn("not-a-steam-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
