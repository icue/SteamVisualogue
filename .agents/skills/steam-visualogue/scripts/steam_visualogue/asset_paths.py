"""Keep each run's asset directory consistent across pipeline stages."""

import os
import shutil
from pathlib import Path

from .context_budget import sha256_path
from .io_utils import read_json, write_json


def _directory_file(run_dir: str | Path) -> Path:
    return Path(run_dir) / ".agent-work" / "asset-directory.json"


def resolve_assets_dir(run_dir: str | Path, assets_dir: str | Path | None = None) -> Path:
    """Resolve an explicit cwd-relative override, a saved choice, or run/assets."""

    if assets_dir is not None:
        return Path(assets_dir).resolve()
    root = Path(run_dir).resolve()
    settings = _directory_file(root)
    if not settings.is_file():
        return root / "assets"
    document = read_json(settings)
    if not isinstance(document, dict) or not isinstance(document.get("directory"), str) or not document["directory"].strip():
        raise ValueError("run asset directory is invalid")
    return (root / document["directory"]).resolve()


def remember_assets_dir(run_dir: str | Path, assets_dir: str | Path) -> None:
    """Save a successful selection locally, retaining relative paths when possible."""

    root = Path(run_dir).resolve()
    selected = Path(assets_dir).resolve()
    settings = _directory_file(root)
    if selected == root / "assets" and not settings.exists():
        return
    try:
        directory = Path(os.path.relpath(selected, root)).as_posix()
    except ValueError:  # Windows directories on different drives.
        directory = selected.as_posix()
    document = {"directory": directory}
    if not settings.is_file() or read_json(settings) != document:
        write_json(settings, document)


def artwork_reference(run_dir: str | Path, source: Path) -> str:
    """Expose readable run-relative images without leaking external source paths."""

    root = Path(run_dir).resolve()
    source = source.resolve()
    if source.is_relative_to(root):
        return source.relative_to(root).as_posix()
    digest = sha256_path(source)
    target = root / ".agent-work" / "artwork" / (digest.removeprefix("sha256:") + source.suffix.lower())
    if not target.resolve().is_relative_to(root):
        raise ValueError("artwork handoff path escapes its run directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or sha256_path(target) != digest:
        shutil.copyfile(source, target)
    return target.relative_to(root).as_posix()
