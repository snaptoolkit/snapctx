"""Walk a repo root, respecting .gitignore, and stream files to a parser."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pathspec

from neargrep.parsers.registry import parser_for, supported_extensions

ALWAYS_SKIP = {
    ".git", ".hg", ".svn",
    ".venv", "venv", "env",
    "node_modules", "bower_components", "vendor",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".tox",
    ".neargrep",
}

# Filename suffixes that indicate bundled/minified third-party code. Parsing
# these produces noise (single-letter symbol names, thousands of collisions)
# and they are never what a user is searching for.
_SKIP_SUFFIXES = (
    ".min.js", ".min.mjs", ".min.cjs", ".bundle.js", ".min.css",
    ".map",                                   # source maps
    ".worker.js",                             # web-worker bundles
    "-bundle.js", "-bundle.mjs",              # e.g. swagger-ui-bundle.js
    ".standalone.js", ".lib.js",              # e.g. redoc.standalone.js
)

# Max size (bytes) for a source file we'll parse. Any .js/.ts/.tsx over this
# is almost certainly a vendored bundle (swagger, jquery, redoc, …) — real
# hand-written code stays well under 250 KB even for the largest single files.
_MAX_SOURCE_BYTES = 250 * 1024


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
        name = path.name
        if any(name.endswith(suf) for suf in _SKIP_SUFFIXES):
            continue
        try:
            if path.stat().st_size > _MAX_SOURCE_BYTES:
                continue
        except OSError:
            continue
        yield path
