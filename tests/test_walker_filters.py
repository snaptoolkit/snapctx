"""Walker filters: build-output dirs and multi-level gitignore inheritance."""

from __future__ import annotations

from pathlib import Path

from snapctx.walker import (
    _ignored_by_stack,
    iter_source_files,
    iter_text_files,
    load_gitignore_stack,
)


# ---------- ALWAYS_SKIP framework build dirs ----------


def test_next_js_build_dir_is_skipped(tmp_path: Path) -> None:
    """A ``.next/`` build directory must not pollute the index even when it
    has thousands of generated TS files. This is the #1 friction point for
    Next.js / SvelteKit / Nuxt projects in multi-root mode where the
    parent's gitignore wouldn't otherwise apply."""
    repo = tmp_path / "frontend"
    (repo / ".next" / "server" / "pages").mkdir(parents=True)
    (repo / ".next" / "server" / "pages" / "index.js").write_text(
        "/* generated */\nfunction page() {}\n"
    )
    (repo / "app").mkdir()
    (repo / "app" / "page.tsx").write_text("export default function Home() {}\n")

    files = {p.relative_to(repo).as_posix() for p in iter_source_files(repo)}
    assert "app/page.tsx" in files
    assert not any(f.startswith(".next/") for f in files), files


def test_svelte_kit_and_nuxt_dirs_are_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "site"
    repo.mkdir()
    for build_dir in (".svelte-kit", ".nuxt", ".astro", ".turbo", ".parcel-cache", "out", ".expo"):
        d = repo / build_dir
        d.mkdir()
        (d / "noise.ts").write_text("export const x = 1\n")
    (repo / "src" / "real.ts").parent.mkdir(parents=True)
    (repo / "src" / "real.ts").write_text("export const real = true\n")

    files = {p.relative_to(repo).as_posix() for p in iter_source_files(repo)}
    assert "src/real.ts" in files
    for build_dir in (".svelte-kit", ".nuxt", ".astro", ".turbo", ".parcel-cache", "out", ".expo"):
        assert not any(f.startswith(f"{build_dir}/") for f in files), (build_dir, files)


def test_coverage_outputs_are_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "coverage" / "lcov-report").mkdir(parents=True)
    (repo / "coverage" / "lcov-report" / "fake.js").write_text("// generated\n")
    (repo / "htmlcov").mkdir()
    (repo / "htmlcov" / "index.html").write_text("<html>generated</html>\n")
    (repo / "src.py").write_text("def f(): return 1\n")

    files = {p.relative_to(repo).as_posix() for p in iter_source_files(repo)}
    assert "src.py" in files
    assert all("coverage" not in f and "htmlcov" not in f for f in files), files


# ---------- multi-level gitignore inheritance ----------


def test_subproject_scan_inherits_parent_gitignore(tmp_path: Path) -> None:
    """When the walker scans a sub-project, parent .gitignore rules apply.

    Concretely: monorepo at ``/r`` with .gitignore listing
    ``staticfiles/``. ``snapctx index r/backend`` scans ``r/backend``,
    which has its own ``staticfiles/`` dir. Without parent-gitignore
    walk-up, that dir is unfiltered. With it, the sub-project scan
    honors the parent rule.
    """
    repo = tmp_path / "monorepo"
    (repo / ".git").mkdir(parents=True)  # mark repo root
    (repo / ".gitignore").write_text("staticfiles/\n")

    backend = repo / "backend"
    backend.mkdir()
    (backend / "real.py").write_text("def real(): return 1\n")
    (backend / "staticfiles").mkdir()
    (backend / "staticfiles" / "noise.py").write_text("def noise(): return 0\n")

    files = {p.relative_to(backend).as_posix() for p in iter_source_files(backend)}
    assert "real.py" in files
    assert "staticfiles/noise.py" not in files


def test_load_gitignore_stack_stops_at_git_repo_root(tmp_path: Path) -> None:
    """Parent rules above the .git/ marker must not leak in — different repo."""
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".gitignore").write_text("# this should NOT apply\n")
    repo = outer / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("dist/\n")
    sub = repo / "pkg"
    sub.mkdir()

    stack = load_gitignore_stack(sub)
    anchors = [a for a, _ in stack]
    assert repo.resolve() in anchors
    # The outer dir's .gitignore is *above* the repo root and must be
    # excluded — leaking it would let unrelated rules from a parent
    # workspace into our scan.
    assert outer.resolve() not in anchors


def test_load_gitignore_stack_handles_no_gitignore_files(tmp_path: Path) -> None:
    repo = tmp_path / "bare"
    repo.mkdir()
    assert load_gitignore_stack(repo) == []


def test_ignored_by_stack_matches_relative_to_each_anchor(tmp_path: Path) -> None:
    """Each anchor's spec must match against the path relative to that anchor —
    a parent rule ``foo/`` must match ``parent/foo/x.py`` but not ``foo`` at
    the parent dir's parent."""
    parent = tmp_path / "p"
    parent.mkdir()
    import pathspec
    parent_spec = pathspec.PathSpec.from_lines("gitignore", ["secret/"])
    stack = [(parent, parent_spec)]

    inside = parent / "secret" / "leak.py"
    outside = parent / "ok" / "fine.py"
    inside.parent.mkdir(parents=True)
    outside.parent.mkdir(parents=True)
    inside.write_text("x = 1\n")
    outside.write_text("y = 1\n")

    assert _ignored_by_stack(inside, stack) is True
    assert _ignored_by_stack(outside, stack) is False


def test_iter_text_files_also_inherits_parent_gitignore(tmp_path: Path) -> None:
    """``snapctx grep`` walks via iter_text_files and must honor the same
    inheritance — otherwise grep returns matches from generated subtrees
    that the user already told git to ignore."""
    repo = tmp_path / "monorepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("build/\n")
    sub = repo / "frontend"
    sub.mkdir()
    (sub / "real.md").write_text("# Real\n")
    (sub / "build").mkdir()
    (sub / "build" / "noise.md").write_text("# Generated\n")

    files = {p.relative_to(sub).as_posix() for p in iter_text_files(sub)}
    assert "real.md" in files
    assert "build/noise.md" not in files
