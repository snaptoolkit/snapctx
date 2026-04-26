"""Cross-package call resolution: when a call inside one vendor package's
index lands on a name imported from another *also-indexed* package, the
graph stitches across the two indexes.

Honors the explicit-prefix rule from the routing redesign: cross-resolution
only peeks into packages the user has *already chosen* to index. Sibling
packages without an index stay unresolved.
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import context, expand, index_root
from snapctx.api._cross_package import CrossPackageResolver, _candidate_qnames
from snapctx.vendor import ensure_vendor_indexed


def _make_python_pkg(root: Path, name: str, files: dict[str, str]) -> Path:
    """Create a fake site-packages package with the given files."""
    site = root / ".venv" / "lib" / "python3.14" / "site-packages"
    pkg = site / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for relpath, body in files.items():
        target = pkg / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return pkg


# ---------- candidate qname enumeration ----------


def test_candidate_qnames_simple_function() -> None:
    """``from x.y import z`` then ``z(...)``: in_pkg_module=y, chain=[z]."""
    cands = _candidate_qnames("y", ["z"])
    assert "y:z" in cands


def test_candidate_qnames_submodule_via_from_import() -> None:
    """``from x import y`` then ``y.z(...)``: chain=[y, z], in_pkg_module=''."""
    cands = _candidate_qnames("", ["y", "z"])
    assert "y:z" in cands


def test_candidate_qnames_class_method() -> None:
    """``from x.y import C`` then ``C.method(...)``: chain=[C, method]."""
    cands = _candidate_qnames("y", ["C", "method"])
    # Either "y:C.method" (C is a class in y.py) or "y.C:method" — both
    # are tried so a class-with-method or a sub-sub-module both resolve.
    assert "y:C.method" in cands
    assert "y.C:method" in cands


# ---------- end-to-end cross-package resolution ----------


def test_expand_resolves_cross_package_callee(tmp_path: Path) -> None:
    """A method in package ``alpha`` calls a function imported from package
    ``beta``; both are indexed; ``expand`` should report the resolved
    cross-package neighbor with a ``package`` tag."""
    _make_python_pkg(tmp_path, "beta", {
        "core.py": "def do_work(x): return x + 1\n",
    })
    _make_python_pkg(tmp_path, "alpha", {
        "main.py": (
            "from beta.core import do_work\n"
            "\n"
            "class Worker:\n"
            "    def run(self):\n"
            "        return do_work(42)\n"
        ),
    })
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    index_root(tmp_path)
    ensure_vendor_indexed(tmp_path, "alpha")
    ensure_vendor_indexed(tmp_path, "beta")

    out = expand("main:Worker.run", direction="callees", root=tmp_path, scope="alpha")
    layer0 = out["layers"][0]
    cross_edges = [e for e in layer0 if e.get("package") == "beta"]
    assert len(cross_edges) == 1
    assert cross_edges[0]["qname"].endswith("core:do_work")


def test_expand_leaves_unresolved_when_other_package_not_indexed(
    tmp_path: Path,
) -> None:
    """Honors the prefix-explicit rule: don't fan out into packages the
    user hasn't asked for. ``beta`` is installed but not indexed → the
    cross-package call stays unresolved."""
    _make_python_pkg(tmp_path, "beta", {
        "core.py": "def do_work(x): return x + 1\n",
    })
    _make_python_pkg(tmp_path, "alpha", {
        "main.py": (
            "from beta.core import do_work\n"
            "def caller(): return do_work(1)\n"
        ),
    })
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    index_root(tmp_path)
    ensure_vendor_indexed(tmp_path, "alpha")
    # Note: beta is NOT indexed.

    out = expand("main:caller", direction="callees", root=tmp_path, scope="alpha")
    layer0 = out["layers"][0]
    # The do_work call should appear unresolved — no package tag, no row.
    do_work_edges = [
        e for e in layer0
        if "do_work" in e.get("qname", "") or e.get("resolved") is False
    ]
    assert any(e.get("resolved") is False for e in do_work_edges), do_work_edges


def test_context_includes_cross_package_callee(tmp_path: Path) -> None:
    """``context()`` should also surface cross-package edges via
    ``collect_neighbors``."""
    _make_python_pkg(tmp_path, "beta", {
        "core.py": "def do_work(x): return x + 1\n",
    })
    _make_python_pkg(tmp_path, "alpha", {
        "main.py": (
            "from beta.core import do_work\n"
            "def runner(): return do_work(7)\n"
        ),
    })
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    index_root(tmp_path)
    ensure_vendor_indexed(tmp_path, "alpha")
    ensure_vendor_indexed(tmp_path, "beta")

    res = context("main:runner", root=tmp_path, scope="alpha")
    seeds = res["seeds"]
    assert len(seeds) == 1
    callees = seeds[0].get("callees", [])
    cross = [c for c in callees if c.get("package") == "beta"]
    assert len(cross) == 1
    assert cross[0]["qname"].endswith("core:do_work")


def test_aliased_import_resolves(tmp_path: Path) -> None:
    """``from beta.core import do_work as dw`` then ``dw(...)``."""
    _make_python_pkg(tmp_path, "beta", {
        "core.py": "def do_work(x): return x + 1\n",
    })
    _make_python_pkg(tmp_path, "alpha", {
        "main.py": (
            "from beta.core import do_work as dw\n"
            "def caller(): return dw(1)\n"
        ),
    })
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    index_root(tmp_path)
    ensure_vendor_indexed(tmp_path, "alpha")
    ensure_vendor_indexed(tmp_path, "beta")

    out = expand("main:caller", direction="callees", root=tmp_path, scope="alpha")
    cross = [e for e in out["layers"][0] if e.get("package") == "beta"]
    assert len(cross) == 1
    assert cross[0]["qname"].endswith("core:do_work")


def test_dotted_module_import_resolves(tmp_path: Path) -> None:
    """``from beta import core`` then ``core.do_work(...)``."""
    _make_python_pkg(tmp_path, "beta", {
        "core.py": "def do_work(x): return x + 1\n",
    })
    _make_python_pkg(tmp_path, "alpha", {
        "main.py": (
            "from beta import core\n"
            "def caller(): return core.do_work(1)\n"
        ),
    })
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    index_root(tmp_path)
    ensure_vendor_indexed(tmp_path, "alpha")
    ensure_vendor_indexed(tmp_path, "beta")

    out = expand("main:caller", direction="callees", root=tmp_path, scope="alpha")
    cross = [e for e in out["layers"][0] if e.get("package") == "beta"]
    assert len(cross) == 1
    assert cross[0]["qname"].endswith("core:do_work")


def test_resolver_does_not_recurse_into_own_index(tmp_path: Path) -> None:
    """A package's own intra-package calls should resolve via the normal
    parser path, not via the cross-package resolver. Verify by checking
    that the resolver returns None for the current scope."""
    _make_python_pkg(tmp_path, "alpha", {
        "main.py": (
            "from alpha.helper import helper\n"
            "def caller(): return helper(1)\n"
        ),
        "helper.py": "def helper(x): return x\n",
    })
    (tmp_path / "app.py").write_text("def app(): return 1\n")
    index_root(tmp_path)
    ensure_vendor_indexed(tmp_path, "alpha")

    from snapctx.api._common import open_index
    idx = open_index(tmp_path, scope="alpha")
    try:
        resolver = CrossPackageResolver(tmp_path, current_scope="alpha")
        try:
            # Force a resolve attempt for a name imported from alpha itself.
            # Even if the parser missed it, the resolver should refuse to
            # peek into its own scope.
            file_path = str((tmp_path / ".venv/lib/python3.14/site-packages/alpha/main.py").resolve())
            result = resolver.resolve("helper", file_path, idx)
            assert result is None
        finally:
            resolver.close()
    finally:
        idx.close()
