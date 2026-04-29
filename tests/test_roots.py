"""Auto-discovery of indexed roots."""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root
from snapctx.roots import (
    discover_roots,
    find_subproject_dirs,
    has_project_marker,
    root_label,
    route_by_path,
    route_by_qname,
)


def _make_indexed(root: Path, name: str = "m.py") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text("def hello(): return 1\n")
    index_root(root)
    return root


def test_walk_up_finds_nearest_enclosing_index(tmp_path: Path) -> None:
    """An index at the project root should be discoverable from any nested dir."""
    repo = _make_indexed(tmp_path / "repo")
    deep = repo / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "leaf.py").write_text("x = 1\n")

    roots = discover_roots(deep)
    assert roots == [repo.resolve()]


def test_walk_up_returns_self_when_started_at_root(tmp_path: Path) -> None:
    repo = _make_indexed(tmp_path / "repo")
    roots = discover_roots(repo)
    assert roots == [repo.resolve()]


def test_walk_up_takes_precedence_over_walk_down(tmp_path: Path) -> None:
    """When a parent has its own index AND children are indexed, the parent wins.

    Multi-root mode is only triggered when the parent itself isn't indexed.
    """
    parent = _make_indexed(tmp_path / "parent")
    _make_indexed(parent / "child_a", "a.py")
    _make_indexed(parent / "child_b", "b.py")

    roots = discover_roots(parent)
    assert roots == [parent.resolve()]


def test_walk_down_finds_indexed_children(tmp_path: Path) -> None:
    """Parent has no index but children do — return them all."""
    parent = tmp_path / "monorepo"
    parent.mkdir()
    backend = _make_indexed(parent / "backend", "main.py")
    frontend = _make_indexed(parent / "frontend", "app.py")

    roots = discover_roots(parent)
    assert set(roots) == {backend.resolve(), frontend.resolve()}


def test_walk_down_skips_hidden_dirs(tmp_path: Path) -> None:
    parent = tmp_path / "monorepo"
    parent.mkdir()
    real = _make_indexed(parent / "backend", "main.py")
    # A hidden dir with a .snapctx that we should NOT pick up (looks like
    # accidental tooling state, not a sub-project).
    hidden = parent / ".cache"
    _make_indexed(hidden, "z.py")

    roots = discover_roots(parent)
    assert roots == [real.resolve()]


def test_no_index_returns_empty(tmp_path: Path) -> None:
    """A directory with no index, anywhere in its tree, gives an empty list."""
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "f.py").write_text("def x(): pass\n")
    assert discover_roots(bare) == []


def test_discover_from_file_uses_parent_dir(tmp_path: Path) -> None:
    repo = _make_indexed(tmp_path / "repo")
    f = repo / "file.py"
    f.write_text("x=1")
    roots = discover_roots(f)
    assert roots == [repo.resolve()]


def test_route_by_qname_finds_first_match(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    (a / "m.py").write_text("def alpha(): pass\n")
    index_root(a)

    b = tmp_path / "b"
    b.mkdir()
    (b / "m.py").write_text("def beta(): pass\n")
    index_root(b)

    assert route_by_qname("m:alpha", [a, b]) == a
    assert route_by_qname("m:beta", [a, b]) == b
    assert route_by_qname("m:nope", [a, b]) is None


def test_route_by_path_picks_longest_prefix(tmp_path: Path) -> None:
    parent = tmp_path / "monorepo"
    parent.mkdir()
    backend = parent / "backend"
    frontend = parent / "frontend"
    backend.mkdir()
    frontend.mkdir()

    f = backend / "deep" / "file.py"
    f.parent.mkdir(parents=True)
    f.write_text("x=1")

    assert route_by_path(f, [backend, frontend]) == backend
    assert route_by_path(f, [frontend, backend]) == backend


def test_route_by_path_returns_none_for_unowned_path(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    outside = tmp_path / "outside" / "x.py"
    outside.parent.mkdir()
    outside.write_text("x=1")

    assert route_by_path(outside, [a, b]) is None


def test_root_label_uses_relative_path_when_anchored(tmp_path: Path) -> None:
    parent = tmp_path / "monorepo"
    backend = parent / "backend"
    backend.mkdir(parents=True)
    assert root_label(backend.resolve(), parent.resolve()) == "backend"


def test_root_label_falls_back_to_basename_without_anchor(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    assert root_label(backend.resolve()) == "backend"


def test_has_project_marker_detects_pyproject(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    assert not has_project_marker(p)
    (p / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert has_project_marker(p)


def test_has_project_marker_detects_package_json(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    (p / "package.json").write_text("{}")
    assert has_project_marker(p)


def test_find_subproject_dirs_returns_only_marker_dirs(tmp_path: Path) -> None:
    """Children with project markers are real sub-projects; the rest aren't."""
    parent = tmp_path / "monorepo"
    parent.mkdir()
    backend = parent / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text("[project]\nname='b'\n")
    frontend = parent / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    docs = parent / "docs"
    docs.mkdir()
    (docs / "intro.md").write_text("# docs\n")  # no marker, not a sub-project
    hidden = parent / ".cache"
    hidden.mkdir()
    (hidden / "package.json").write_text("{}")  # hidden dirs skipped regardless

    found = find_subproject_dirs(parent)
    assert set(found) == {backend.resolve(), frontend.resolve()}


def test_find_subproject_dirs_handles_nonexistent_anchor(tmp_path: Path) -> None:
    assert find_subproject_dirs(tmp_path / "missing") == []
