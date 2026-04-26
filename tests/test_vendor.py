"""On-demand vendor-package indexing: discovery, query matching, and
the cache-and-protect contract that lets the package survive subsequent
``index_root`` refreshes.
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root, search_code
from snapctx.api._indexer import index_subtree
from snapctx.index import Index, db_path_for
from snapctx.vendor import (
    discover_packages,
    ensure_packages_for_query,
    match_packages,
)


def _make_python_pkg(root: Path, name: str, body: str = "def hello(): return 1\n") -> Path:
    site = root / ".venv" / "lib" / "python3.14" / "site-packages"
    pkg = site / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(body)
    return pkg


def _make_node_pkg(root: Path, name: str) -> Path:
    pkg = root / "node_modules" / name
    pkg.mkdir(parents=True)
    (pkg / "index.ts").write_text("export const x = 1;\n")
    return pkg


def test_discover_finds_python_packages_in_venv(tmp_path: Path) -> None:
    _make_python_pkg(tmp_path, "fakepkg")
    found = discover_packages(tmp_path)
    assert "fakepkg" in found
    assert found["fakepkg"].name == "fakepkg"


def test_discover_skips_dist_info_and_dotfiles(tmp_path: Path) -> None:
    site = tmp_path / ".venv" / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (site / "fakepkg-1.0.dist-info").mkdir()
    (site / "fakepkg-1.0.egg-info").mkdir()
    (site / ".hidden").mkdir()
    (site / "_internal").mkdir()
    (site / "real").mkdir()
    found = discover_packages(tmp_path)
    assert set(found) == {"real"}


def test_discover_finds_node_modules(tmp_path: Path) -> None:
    _make_node_pkg(tmp_path, "react")
    found = discover_packages(tmp_path)
    assert "react" in found


def test_discover_skips_scoped_node_packages(tmp_path: Path) -> None:
    """v1: scoped packages are intentionally out of scope."""
    (tmp_path / "node_modules" / "@types").mkdir(parents=True)
    _make_node_pkg(tmp_path, "react")
    found = discover_packages(tmp_path)
    assert "@types" not in found
    assert "react" in found


def test_match_packages_is_token_exact_not_substring(tmp_path: Path) -> None:
    """``model`` should NOT match ``modeling`` — substring matches would
    constantly false-positive on common English words."""
    pkgs = {"model": Path("/dev/null"), "django": Path("/dev/null")}
    assert match_packages("how does modeling work", pkgs) == []
    assert match_packages("django model definition", pkgs) == [
        ("model", Path("/dev/null")),
        ("django", Path("/dev/null")),
    ] or match_packages("django model definition", pkgs) == [
        ("django", Path("/dev/null")),
        ("model", Path("/dev/null")),
    ]


def test_match_packages_case_insensitive(tmp_path: Path) -> None:
    pkgs = {"django": Path("/dev/null")}
    assert match_packages("Django QuerySet", pkgs) == [("django", Path("/dev/null"))]


def test_index_subtree_ingests_only_the_subtree(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    pkg = _make_python_pkg(tmp_path, "fakepkg", body="def fake_helper(): return 42\n")
    # Index the project (without vendor toggle, so fakepkg is skipped).
    index_root(tmp_path)
    # On-demand subtree pass.
    summary = index_subtree(tmp_path, pkg)
    assert summary["files_updated"] >= 1
    # fake_helper should now be queryable.
    res = search_code("fake_helper", k=3, root=tmp_path)
    qnames = [r["qname"] for r in res["results"]]
    assert any("fake_helper" in q for q in qnames)


def test_ensure_packages_marks_indexed_and_skips_second_time(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    _make_python_pkg(tmp_path, "fakepkg")
    index_root(tmp_path)

    first = ensure_packages_for_query(tmp_path, "fakepkg please", enable_auto=True)
    assert len(first) == 1
    assert first[0]["package"] == "fakepkg"

    # Marker present.
    idx = Index(db_path_for(tmp_path))
    try:
        assert idx.is_vendor_indexed("fakepkg")
    finally:
        idx.close()

    # Second call: package already indexed, no work done.
    second = ensure_packages_for_query(tmp_path, "fakepkg again", enable_auto=True)
    assert second == []


def test_index_root_preserves_indexed_vendor_packages(tmp_path: Path) -> None:
    """Regression: without the vendor-prefix exemption in the staleness
    diff, the next regular ``index_root`` would forget every django file
    we just ingested on demand."""
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    pkg = _make_python_pkg(tmp_path, "fakepkg")
    index_root(tmp_path)
    ensure_packages_for_query(tmp_path, "fakepkg", enable_auto=True)

    # fakepkg's files are now in the DB.
    idx = Index(db_path_for(tmp_path))
    try:
        before = idx.conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE path LIKE ? || '%'",
            (str(pkg),),
        ).fetchone()["n"]
    finally:
        idx.close()
    assert before >= 2  # __init__.py + core.py

    # Re-run the regular index pass. fakepkg's files aren't in the walker's
    # view (they're in .venv/, gitignored / vendor-skip), so the staleness
    # diff would normally forget them. The vendor-prefix guard prevents that.
    summary = index_root(tmp_path)
    assert summary["files_removed"] == 0

    idx = Index(db_path_for(tmp_path))
    try:
        after = idx.conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE path LIKE ? || '%'",
            (str(pkg),),
        ).fetchone()["n"]
    finally:
        idx.close()
    assert after == before


def test_forget_vendor_package_drops_files_and_marker(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    _make_python_pkg(tmp_path, "fakepkg")
    index_root(tmp_path)
    ensure_packages_for_query(tmp_path, "fakepkg", enable_auto=True)

    idx = Index(db_path_for(tmp_path))
    try:
        removed = idx.forget_vendor_package("fakepkg")
        assert removed >= 2
        assert not idx.is_vendor_indexed("fakepkg")
        # Project's own files untouched.
        n_app = idx.conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE path LIKE ?",
            (f"{tmp_path}/app.py",),
        ).fetchone()["n"]
        assert n_app == 1
    finally:
        idx.close()


def test_explicit_pkg_overrides_query_token_absence(tmp_path: Path) -> None:
    """``--pkg`` works even when the package name doesn't appear in the query."""
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    _make_python_pkg(tmp_path, "fakepkg")
    index_root(tmp_path)

    summaries = ensure_packages_for_query(
        tmp_path, "totally unrelated query",
        explicit=["fakepkg"], enable_auto=False,
    )
    assert len(summaries) == 1
    assert summaries[0]["package"] == "fakepkg"
