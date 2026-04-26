"""Walk a repo root, respecting .gitignore, and stream files to a parser."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pathspec

from snapctx.config import WalkerConfig
from snapctx.parsers.registry import (
    extensions_for_languages,
    parser_for,
    supported_extensions,
)

ALWAYS_SKIP = {
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".tox",
    ".snapctx",
}

# Third-party-dependency directories. Skipped by default because they are
# not the user's code and a single ``node_modules`` tree can dwarf the
# repo's real source by 100×. Toggleable via
# ``[walker].skip_vendor_packages = false`` for the rare case where you
# genuinely need to search inside deps (e.g. debugging an upstream bug).
VENDOR_PACKAGE_DIRS = {
    ".venv", "venv", "env",
    "node_modules", "bower_components", "vendor",
}

# Filename suffixes that indicate bundled/minified third-party code. Parsing
# these produces noise (single-letter symbol names, thousands of collisions)
# and they are never what a user is searching for. Joined with the user's
# ``extra_skip_suffixes`` from snapctx.toml.
_VENDOR_BUNDLE_SUFFIXES = (
    ".min.js", ".min.mjs", ".min.cjs", ".bundle.js", ".min.css",
    ".map",                                   # source maps
    ".worker.js",                             # web-worker bundles
    "-bundle.js", "-bundle.mjs",              # e.g. swagger-ui-bundle.js
    ".standalone.js", ".lib.js",              # e.g. redoc.standalone.js
)


def skip_dirs_for(cfg: WalkerConfig) -> set[str]:
    """Return the set of directory names to skip given a walker config.

    Centralizes the union of ``ALWAYS_SKIP``, vendor-package dirs (when
    enabled), and the user's ``extra_skip_dirs`` so the walker and the
    watcher (which both need to know "is this path under a skipped dir?")
    stay in sync as the rules evolve.
    """
    skip = ALWAYS_SKIP | set(cfg.extra_skip_dirs)
    if cfg.skip_vendor_packages:
        skip |= VENDOR_PACKAGE_DIRS
    return skip


def load_gitignore(root: Path) -> pathspec.PathSpec:
    """Return a PathSpec built from the root's .gitignore (if any).

    Missing file → empty spec (nothing ignored).
    """
    gi = root / ".gitignore"
    if not gi.exists():
        return pathspec.PathSpec.from_lines("gitignore", [])
    return pathspec.PathSpec.from_lines("gitignore", gi.read_text().splitlines())


def iter_source_files(
    root: Path, config: WalkerConfig | None = None
) -> Iterator[Path]:
    """Yield source files under ``root`` that a parser can handle.

    ``config`` overrides the built-in defaults (skip dirs, vendor-bundle
    filter, size cap, language enable list, gitignore behavior). When
    ``None`` (the common case), behavior is unchanged from before the
    config system existed.
    """
    cfg = config or WalkerConfig()
    root = root.resolve()

    skip_dirs = skip_dirs_for(cfg)
    skip_suffixes = (
        _VENDOR_BUNDLE_SUFFIXES + cfg.extra_skip_suffixes
        if cfg.skip_vendor_bundles
        else cfg.extra_skip_suffixes
    )
    enabled_exts = (
        extensions_for_languages(cfg.languages)
        if cfg.languages is not None
        else supported_extensions()
    )
    enabled_exts_set = set(enabled_exts)

    gitignore = load_gitignore(root) if cfg.respect_gitignore else None
    extra_include = (
        pathspec.PathSpec.from_lines("gitignore", cfg.extra_include)
        if cfg.extra_include
        else None
    )
    extra_exclude = (
        pathspec.PathSpec.from_lines("gitignore", cfg.extra_exclude)
        if cfg.extra_exclude
        else None
    )

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        rel_str = str(rel)

        if any(part in skip_dirs for part in rel.parts):
            continue
        if extra_exclude is not None and extra_exclude.match_file(rel_str):
            continue
        if path.suffix not in enabled_exts_set:
            continue
        if parser_for(path.suffix) is None:
            continue
        if skip_suffixes and any(path.name.endswith(suf) for suf in skip_suffixes):
            continue
        # gitignore is checked AFTER ``extra_include`` so the user can
        # whitelist a subtree of an otherwise-ignored vendor dir.
        if gitignore is not None and gitignore.match_file(rel_str):
            if extra_include is None or not extra_include.match_file(rel_str):
                continue
        try:
            if path.stat().st_size > cfg.max_file_size:
                continue
        except OSError:
            continue
        yield path
