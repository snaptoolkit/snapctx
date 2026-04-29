"""Walk a repo root, respecting .gitignore, and stream files to a parser."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pathspec

from snapctx.config import WalkerConfig
from snapctx.parsers.registry import (
    extensions_for_languages,
    parser_for,
    parser_for_path,
    supported_extensions,
)

ALWAYS_SKIP = {
    # VCS metadata.
    ".git", ".hg", ".svn",
    # Python tooling caches.
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # Generic build outputs.
    "dist", "build", ".tox",
    # snapctx's own state.
    ".snapctx",
    # JS/TS framework build outputs. These are gitignored in well-formed
    # projects but a sub-project scan in multi-root mode may not see the
    # parent's .gitignore, so an explicit name list catches them
    # regardless. Concretely: ``.next`` (Next.js), ``.svelte-kit``
    # (SvelteKit), ``.nuxt`` (Nuxt), ``.astro`` (Astro), ``.turbo``
    # (Turborepo cache), ``out`` (Next.js export / generic export),
    # ``.parcel-cache`` (Parcel), ``.expo`` (Expo / React Native).
    ".next", ".svelte-kit", ".nuxt", ".astro", ".turbo",
    "out", ".parcel-cache", ".expo",
    # Test/coverage outputs.
    "coverage", "htmlcov",
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


_MAX_GITIGNORE_WALK_UP = 20


def load_gitignore_stack(root: Path) -> list[tuple[Path, pathspec.PathSpec]]:
    """Collect ``.gitignore`` rules from ``root`` upward to the project root.

    Returns a list of ``(anchor_dir, spec)`` tuples — one per ``.gitignore``
    file found, ordered closest-to-furthest (root's own first). Each rule
    must be matched against the path **relative to its anchor dir** because
    gitignore patterns are anchored to the file's location.

    Walking up matters in multi-root mode: when ``snapctx index`` scans a
    sub-project (``./backend``) whose parent (``./``) has a ``.gitignore``
    listing ``backend/staticfiles/`` or ``.next/``, the sub-project scan
    must still honor that — otherwise huge generated subtrees that the
    user has *already* told git to ignore end up in the index.

    Stops at the directory containing ``.git/`` (the repo root) or at the
    filesystem root. Hard cap of 20 levels as a safety net.
    """
    out: list[tuple[Path, pathspec.PathSpec]] = []
    current = root.resolve()
    for _ in range(_MAX_GITIGNORE_WALK_UP):
        gi = current / ".gitignore"
        if gi.exists():
            try:
                spec = pathspec.PathSpec.from_lines(
                    "gitignore", gi.read_text().splitlines()
                )
                out.append((current, spec))
            except OSError:
                pass
        # The repo root is wherever ``.git`` lives — gitignore rules in
        # ancestors of the repo root don't apply to this repo.
        if (current / ".git").exists():
            break
        if current.parent == current:
            break
        current = current.parent
    return out


def _ignored_by_stack(
    abs_path: Path, stack: list[tuple[Path, pathspec.PathSpec]]
) -> bool:
    """``True`` if any gitignore in the stack ignores ``abs_path``."""
    for anchor, spec in stack:
        try:
            rel = abs_path.relative_to(anchor)
        except ValueError:
            continue
        if spec.match_file(str(rel)):
            return True
    return False


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

    gitignore_stack = (
        load_gitignore_stack(root) if cfg.respect_gitignore else []
    )
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
        suffix = path.suffix or (path.name if path.name.startswith(".") else "")
        if suffix not in enabled_exts_set:
            continue
        if parser_for_path(path) is None:
            continue
        if skip_suffixes and any(path.name.endswith(suf) for suf in skip_suffixes):
            continue
        # gitignore is checked AFTER ``extra_include`` so the user can
        # whitelist a subtree of an otherwise-ignored vendor dir. The
        # stack walks parent ``.gitignore`` files too so a sub-project
        # scan honors rules declared at the monorepo root.
        if gitignore_stack and _ignored_by_stack(path, gitignore_stack):
            if extra_include is None or not extra_include.match_file(rel_str):
                continue
        try:
            if path.stat().st_size > cfg.max_file_size:
                continue
        except OSError:
            continue
        yield path


def iter_text_files(
    root: Path, config: WalkerConfig | None = None
) -> Iterator[Path]:
    """Yield every text-like file under ``root`` (parser-supported or not).

    Same gitignore / vendor-dir / vendor-bundle / size-cap rules as
    ``iter_source_files``, but does NOT filter by parser-registered
    extension. Binary files are skipped via a cheap null-byte sniff over
    the first 4 KB. Used by ``snapctx grep`` to scan markdown, configs,
    docs, and any other text the user may want to search beyond what the
    indexer parses.
    """
    cfg = config or WalkerConfig()
    root = root.resolve()

    skip_dirs = skip_dirs_for(cfg)
    skip_suffixes = (
        _VENDOR_BUNDLE_SUFFIXES + cfg.extra_skip_suffixes
        if cfg.skip_vendor_bundles
        else cfg.extra_skip_suffixes
    )

    gitignore_stack = (
        load_gitignore_stack(root) if cfg.respect_gitignore else []
    )
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
        if skip_suffixes and any(path.name.endswith(suf) for suf in skip_suffixes):
            continue
        if gitignore_stack and _ignored_by_stack(path, gitignore_stack):
            if extra_include is None or not extra_include.match_file(rel_str):
                continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > cfg.max_file_size or size == 0:
            continue
        if _looks_binary(path):
            continue
        yield path


def _looks_binary(path: Path, sniff_bytes: int = 4096) -> bool:
    """Cheap binary detector: any NUL byte in the first ``sniff_bytes``."""
    try:
        with path.open("rb") as f:
            chunk = f.read(sniff_bytes)
    except OSError:
        return True
    return b"\x00" in chunk
