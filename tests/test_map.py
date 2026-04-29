"""Tests for ``map_repo`` — the repo-wide table-of-contents op."""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root, map_repo


def test_map_groups_by_directory_and_hoists_module_docstring(indexed_root: Path) -> None:
    """Each file lands under its directory, with module docstring at the file
    level (not as a separate ``module:`` symbol)."""
    out = map_repo(root=indexed_root)

    assert out["depth"] == 1
    assert out["file_count"] >= 1
    dirs = {d["dir"]: d for d in out["directories"]}
    assert "sample_pkg" in dirs

    auth_file = next(
        f for f in dirs["sample_pkg"]["files"]
        if f["file"].endswith("auth.py")
    )
    assert auth_file["summary"] == "Session management for the sample package."
    # module symbol got hoisted, not duplicated as a symbol entry
    assert all(s["kind"] != "module" for s in auth_file["symbols"])
    # top-level classes are present, with their docstring summary
    sm = next(s for s in auth_file["symbols"] if s["qname"].endswith(":SessionManager"))
    assert sm["kind"] == "class"
    assert sm["docstring"] == "Creates, refreshes, and invalidates user sessions."
    assert "children" not in sm  # depth=1 default


def test_map_depth_2_includes_direct_children(indexed_root: Path) -> None:
    """``depth=2`` adds class methods under their class — but not deeper."""
    out = map_repo(root=indexed_root, depth=2)
    auth_file = next(
        f for d in out["directories"] for f in d["files"]
        if f["file"].endswith("auth.py")
    )
    sm = next(s for s in auth_file["symbols"] if s["qname"].endswith(":SessionManager"))
    assert "children" in sm
    method_names = {c["qname"].rsplit(".", 1)[-1] for c in sm["children"]}
    assert {"login", "refresh", "logout"}.issubset(method_names)


def test_map_includes_decorators_for_navigator_disambiguation(indexed_root: Path) -> None:
    """Decorators are stored separately from the signature in the index, so
    map must surface them — they're the most identifying fact about many
    symbols (``@app.route``, ``@dataclass``, ``@pytest.fixture``)."""
    out = map_repo(root=indexed_root)
    auth_file = next(
        f for d in out["directories"] for f in d["files"]
        if f["file"].endswith("auth.py")
    )
    session = next(s for s in auth_file["symbols"] if s["qname"].endswith(":Session"))
    assert session["decorators"] == ["@dataclass"]


def test_map_prefix_filter_scopes_to_subtree(tmp_path: Path) -> None:
    """``prefix='middleware/'`` returns only files under that subdirectory."""
    repo = tmp_path / "repo"
    (repo / "middleware").mkdir(parents=True)
    (repo / "middleware" / "auth.py").write_text(
        '"""Auth middleware."""\ndef authenticate(): pass\n'
    )
    (repo / "app.py").write_text('"""App entry."""\ndef main(): pass\n')
    index_root(repo)

    out = map_repo(root=repo, prefix="middleware/")
    files = [f["file"] for d in out["directories"] for f in d["files"]]
    assert files == ["middleware/auth.py"]
    assert out["prefix"] == "middleware/"


def test_map_default_lean_mode_drops_signatures_and_lines(indexed_root: Path) -> None:
    """Lean mode (the default) keeps the orientation payload small by
    omitting per-symbol signatures and line ranges. Agents call
    ``outline <file>`` when they need those details for a specific file.
    """
    out = map_repo(root=indexed_root)
    assert out["mode"] == "lean"
    auth_file = next(
        f for d in out["directories"] for f in d["files"]
        if f["file"].endswith("auth.py")
    )
    for s in auth_file["symbols"]:
        assert "signature" not in s, s
        assert "lines" not in s, s
        assert "qname" in s
        assert "kind" in s


def test_map_full_mode_restores_signature_and_lines(indexed_root: Path) -> None:
    out = map_repo(root=indexed_root, mode="full")
    assert out["mode"] == "full"
    auth_file = next(
        f for d in out["directories"] for f in d["files"]
        if f["file"].endswith("auth.py")
    )
    sm = next(s for s in auth_file["symbols"] if s["qname"].endswith(":SessionManager"))
    assert "signature" in sm
    assert "lines" in sm
    assert "-" in sm["lines"]


def test_map_lean_mode_is_substantially_smaller(indexed_root: Path) -> None:
    """The whole point of lean mode: payload is meaningfully smaller."""
    import json

    lean = json.dumps(map_repo(root=indexed_root, mode="lean"))
    full = json.dumps(map_repo(root=indexed_root, mode="full"))
    assert len(lean) < len(full)
    # Conservative: at least 20% smaller on the small fixture; on real
    # repos the gap is much larger because TS signatures dominate.
    assert len(lean) < len(full) * 0.85, (len(lean), len(full))


def test_map_lean_mode_still_keeps_decorators(indexed_root: Path) -> None:
    """Decorators are the most identifying fact about routed/typed symbols
    (``@app.route``, ``@dataclass``) — lean mode keeps them because they're
    cheap and signature-replacing."""
    out = map_repo(root=indexed_root)
    auth_file = next(
        f for d in out["directories"] for f in d["files"]
        if f["file"].endswith("auth.py")
    )
    session = next(s for s in auth_file["symbols"] if s["qname"].endswith(":Session"))
    assert session["decorators"] == ["@dataclass"]
