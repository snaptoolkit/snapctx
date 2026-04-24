"""Walk a repo root, respecting .gitignore, and stream files to a parser."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pathspec

from neargrep.parsers.registry import parser_for, supported_extensions

ALWAYS_SKIP = {
    ".git", ".hg", ".svn",
    ".venv", "venv", "env",
    "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".tox",
    ".neargrep",
}


def load_gitignore(root: Path) -> pathspec.PathSpec:
    """Return a PathSpec built from the root's .gitignore (if any).

    Missing file → empty spec (nothing ignored).
    """
    gi = root / ".gitignore"
    if not gi.exists():
        return pathspec.PathSpec.from_lines("gitwildmatch", [])
    return pathspec.PathSpec.from_lines("gitwildmatch", gi.read_text().splitlines())


def iter_source_files(root: Path) -> Iterator[Path]:
    """Yield source files under `root` that a parser can handle."""
    root = root.resolve()
    spec = load_gitignore(root)
    exts = supported_extensions()

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in ALWAYS_SKIP for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root)
        if spec.match_file(str(rel)):
            continue
        if path.suffix not in exts:
            continue
        if parser_for(path.suffix) is None:
            continue
        yield path
