"""Load the Steam credential locally without exposing it through the CLI interface."""

from __future__ import annotations

import re
from pathlib import Path

from .paths import skill_root, workspace_root


CREDENTIAL_FILE_NAME = ".steam-visualogue.env"
API_COORDINATION_FILE_NAME = ".steam-visualogue-api-coordination.sqlite"
_API_KEY = re.compile(r"^[0-9A-Fa-f]{32}$")


class CredentialFileError(RuntimeError):
    """A safe outward error that never contains credential-file contents."""


def credential_path(root: str | Path | None = None) -> Path:
    base = workspace_root() if root is None else Path(root)
    return base / CREDENTIAL_FILE_NAME


def api_coordination_path() -> Path:
    """Return the shared request coordination database for this workspace."""

    return workspace_root() / API_COORDINATION_FILE_NAME


def load_steam_api_key(root: str | Path | None = None) -> str:
    """Read one strict STEAM_API_KEY assignment from the local credential file."""

    source = credential_path(root)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise CredentialFileError(
            f"Missing local credential file: {CREDENTIAL_FILE_NAME}"
        ) from None
    except (OSError, UnicodeError):
        raise CredentialFileError(
            f"Unable to read local credential file: {CREDENTIAL_FILE_NAME}"
        ) from None

    value: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CredentialFileError(
                f"Invalid local credential file: {CREDENTIAL_FILE_NAME}"
            )
        name, candidate = (part.strip() for part in line.split("=", 1))
        if name != "STEAM_API_KEY" or value is not None:
            raise CredentialFileError(
                f"Invalid local credential file: {CREDENTIAL_FILE_NAME}"
            )
        value = candidate

    if value is None or not _API_KEY.fullmatch(value):
        raise CredentialFileError(
            f"Invalid local credential file: {CREDENTIAL_FILE_NAME}"
        )
    return value


__all__ = [
    "CREDENTIAL_FILE_NAME",
    "API_COORDINATION_FILE_NAME",
    "CredentialFileError",
    "api_coordination_path",
    "credential_path",
    "load_steam_api_key",
    "skill_root",
]
