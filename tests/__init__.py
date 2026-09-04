"""Test package bootstrap with one shared source import path."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / ".agents" / "skills" / "steam-visualogue"
_SCRIPTS = str(SKILL_ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
