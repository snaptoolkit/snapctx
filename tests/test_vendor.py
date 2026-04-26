"""Per-package on-demand vendor indexing.

Each indexed package gets its own SQLite at
``<root>/.snapctx/vendor/<name>/index.db``. Routing is by query prefix
(``"<pkg>: ..."``) or explicit ``--pkg`` flag — no auto-detection on
arbitrary tokens.
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root, search_code
from snapctx.api._indexer import index_vendor_package
from snapctx.index import db_path_for
from snapctx.vendor import (
    discover_packages,
    ensure_vendor_indexed,
    forget_vendor,
    is_vendor_indexed,
    list_indexed_vendors,
    parse_query_prefix,
    vendor_index_dir,
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


# ---------- discovery ----------


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
    """v1: scoped @ packages are intentionally out of scope."""
    (tmp_path / "node_modules" / "@types").mkdir(parents=True)
    _make_node_pkg(tmp_path, "react")
    found = discover_packages(tmp_path)
    assert "@types" not in found
    assert "react" in found


# ---------- prefix parsing ----------


def test_prefix_routes_when_head_matches_installed_package(tmp_path: Path) -> None:
    _make_python_pkg(tmp_path, "django")
    scope, rest = parse_query_prefix("django: queryset filter chain", tmp_path)
    assert scope == "django"
    assert rest == "queryset filter chain"


def test_prefix_does_not_route_when_head_is_unknown(tmp_path: Path) -> None:
    """``foo:bar`` looks like a qname when ``foo`` isn't a real package."""
    scope, rest = parse_query_prefix("module.path:Symbol", tmp_path)
    assert scope is None
    assert rest == "module.path:Symbol"


def test_prefix_does_not_route_when_head_is_dotted(tmp_path: Path) -> None:
    """Dotted heads are qnames, not vendor prefixes."""
    _make_python_pkg(tmp_path, "django")
    scope, rest = parse_query_prefix("django.db.models:QuerySet", tmp_path)
    assert scope is None
    assert rest == "django.db.models:QuerySet"


def test_prefix_routes_to_already_indexed_package_even_if_uninstalled(
    tmp_path: Path,
) -> None:
    """If a package was indexed before but its source dir was deleted, the
    prefix should still route to its preserved index."""
    pkg = _make_python_pkg(tmp_path, "ghost")
    index_root(tmp_path)
    ensure_vendor_indexed(tmp_path, "ghost")
    # Wipe the source dir.
    import shutil
    shutil.rmtree(pkg)

    scope, rest = parse_query_prefix("ghost: something", tmp_path)
    assert scope == "ghost"
    assert rest == "something"


# ---------- per-package index lifecycle ----------


def test_index_vendor_package_writes_to_isolated_db(tmp_path: Path) -> None:
    pkg = _make_python_pkg(tmp_path, "fakepkg", body="def fake_helper(): return 42\n")
    index_root(tmp_path)
    summary = index_vendor_package(tmp_path, "fakepkg", pkg)
    assert summary["package"] == "fakepkg"
    assert summary["files_updated"] >= 2  # __init__.py + core.py

    repo_db = db_path_for(tmp_path)
    pkg_db = db_path_for(tmp_path, scope="fakepkg")
    assert repo_db.exists()
    assert pkg_db.exists()
    assert repo_db != pkg_db
    # The package's symbols sit in the package's DB only — repo search
    # must not see them.
    repo_hits = search_code("fake_helper", k=5, root=tmp_path)
    assert all("fake_helper" not in r["qname"] for r in repo_hits["results"])


def test_search_with_scope_finds_package_symbols(tmp_path: Path) -> None:
    _make_python_pkg(tmp_path, "fakepkg", body="def fake_helper(): return 42\n")
    index_root(tmp_path)
    ensure_vendor_indexed(tmp_path, "fakepkg")

    res = search_code("fake_helper", k=3, root=tmp_path, scope="fakepkg")
    qnames = [r["qname"] for r in res["results"]]
    assert any("fake_helper" in q for q in qnames)


def test_vendor_qnames_are_rooted_at_package_not_repo(tmp_path: Path) -> None:
    """Per-package re-rooting: qnames inside a vendor index look like the
    package's module structure, not ``.venv.lib.pythonX.Y...`` prefixes."""
    _make_python_pkg(tmp_path, "fakepkg", body="def fake_helper(): return 42\n")
    index_root(tmp_path)
    ensure_vendor_indexed(tmp_path, "fakepkg")

    res = search_code("fake_helper", k=3, root=tmp_path, scope="fakepkg")
    qnames = [r["qname"] for r in res["results"]]
    # Should be like "core:fake_helper", not "<long.venv.path>:fake_helper".
    assert any(q.startswith("core:") for q in qnames), qnames


def test_ensure_vendor_indexed_is_idempotent(tmp_path: Path) -> None:
    _make_python_pkg(tmp_path, "fakepkg")
    index_root(tmp_path)

    first = ensure_vendor_indexed(tmp_path, "fakepkg")
    assert first is not None
    assert first["files_updated"] >= 2

    # Already indexed — second call returns None without rebuilding.
    second = ensure_vendor_indexed(tmp_path, "fakepkg")
    assert second is None


def test_ensure_vendor_indexed_unknown_package_returns_none(tmp_path: Path) -> None:
    index_root(tmp_path)
    result = ensure_vendor_indexed(tmp_path, "not_installed")
    assert result is None
    assert not is_vendor_indexed(tmp_path, "not_installed")


def test_list_indexed_vendors_reflects_directory(tmp_path: Path) -> None:
    _make_python_pkg(tmp_path, "alpha")
    _make_python_pkg(tmp_path, "beta")
    index_root(tmp_path)
    assert list_indexed_vendors(tmp_path) == []

    ensure_vendor_indexed(tmp_path, "alpha")
    assert list_indexed_vendors(tmp_path) == ["alpha"]

    ensure_vendor_indexed(tmp_path, "beta")
    assert list_indexed_vendors(tmp_path) == ["alpha", "beta"]


def test_forget_vendor_removes_only_that_packages_index(tmp_path: Path) -> None:
    _make_python_pkg(tmp_path, "alpha")
    _make_python_pkg(tmp_path, "beta")
    index_root(tmp_path)
    ensure_vendor_indexed(tmp_path, "alpha")
    ensure_vendor_indexed(tmp_path, "beta")

    assert forget_vendor(tmp_path, "alpha") is True
    assert not is_vendor_indexed(tmp_path, "alpha")
    assert is_vendor_indexed(tmp_path, "beta")
    # Forgetting again is a no-op.
    assert forget_vendor(tmp_path, "alpha") is False


def test_repo_index_unaffected_by_vendor_indexing(tmp_path: Path) -> None:
    """Indexing a vendor package must not write to the repo's DB."""
    (tmp_path / "app.py").write_text("def app_only(): return 1\n")
    _make_python_pkg(tmp_path, "fakepkg")
    index_root(tmp_path)

    repo_files_before = _count_files(tmp_path, scope=None)
    ensure_vendor_indexed(tmp_path, "fakepkg")
    repo_files_after = _count_files(tmp_path, scope=None)
    assert repo_files_before == repo_files_after


def _count_files(root: Path, scope: str | None) -> int:
    import sqlite3
    db = db_path_for(root, scope=scope)
    if not db.exists():
        return 0
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    finally:
        conn.close()


def test_vendor_index_dir_layout(tmp_path: Path) -> None:
    """``.snapctx/vendor/<name>/index.db`` is the canonical path."""
    expected = tmp_path / ".snapctx" / "vendor" / "django" / "index.db"
    assert db_path_for(tmp_path, scope="django") == expected.resolve()
    assert vendor_index_dir(tmp_path, "django") == expected.parent.resolve()
