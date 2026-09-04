"""Current contract helpers for the Steam Visualogue pipeline.

The planning boundary deliberately knows only the current deck artifact.  It
does not parse historical runs or provide compatibility for retired contracts.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .paths import SCHEMA_ROOT


def _path_text(path: Sequence[Any]) -> str:
    return "/" + "/".join(str(part) for part in path)


def validate_schema_document(document_name: str, schema_name: str, document: Any) -> None:
    """Validate one current JSON document and expose a stable error location."""

    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("jsonschema is required for contract validation") from exc
    try:
        schema = json.loads((SCHEMA_ROOT / schema_name).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{document_name}: schema '{schema_name}' is unavailable") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{document_name}: schema '{schema_name}' is invalid: {exc}") from exc
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda item: (tuple(str(part) for part in item.absolute_path), str(item.message)),
    )
    if errors:
        error = errors[0]
        where = _path_text(tuple(error.absolute_path)) or "/"
        raise ValueError(f"{document_name} invalid at {where}: {error.message}")


__all__ = [
    "SCHEMA_ROOT",
    "validate_schema_document",
]
