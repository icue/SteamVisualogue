"""Locate bundled skill resources and caller-owned runtime data."""

from pathlib import Path


def skill_root() -> Path:
    """Return the installed skill directory, independent of the caller's cwd."""

    return Path(__file__).resolve().parents[2]


def workspace_root() -> Path:
    """Use the command's working directory for credentials, caches, and output."""

    return Path.cwd().resolve()


REFERENCES_ROOT = skill_root() / "references"
SCHEMA_ROOT = REFERENCES_ROOT / "schemas"
