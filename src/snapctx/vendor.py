"""Per-package on-demand indexing of installed third-party packages.

Each indexed package gets its own isolated SQLite database under
``<root>/.snapctx/vendor/<name>/index.db``. Two reasons for the per-
package isolation rather than merging into the repo's index:

1. **Vector neighborhood quality.** Cosine search over a single
   coherent corpus (just Django source) returns much sharper matches
   than over a mixed corpus (Django + the user's filter classes that
   share vocabulary). The merged form noticeably degrades retrieval.
2. **Cleaner qnames.** Inside the package's index we re-root the
   parser at the package directory itself, so qnames look like
   ``db.models.query:QuerySet`` — Django's actual module structure —
   not ``.venv.lib.python3.14.site-packages.django.db.models.query:QuerySet``.

Routing is explicit. The user prefixes a query with ``<pkg>: <rest>``
(e.g. ``"django: queryset filter chain"``) and snapctx routes to that
package's index. No prefix → repo only. There is no implicit auto-
detection from arbitrary query tokens — that was the cause of the
original cross-namespace noise.

Discovery scans:
- Python: ``<root>/{.venv,venv,env}/lib/python*/site-packages/<name>/``
- Node:   ``<root>/node_modules/<name>/`` (top-level; scoped @x/y out
  of scope for v1)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_VENV_DIRS = (".venv", "venv", "env")
_DIST_INFO_SUFFIXES = (".dist-info", ".egg-info", ".data")
_NODE_MODULES = "node_modules"


def discover_packages(root: Path) -> dict[str, Path]:
    """Return ``{name: absolute_path}`` for installed packages under ``root``."""
    found: dict[str, Path] = {}
    root = root.resolve()

    for venv_name in _VENV_DIRS:
        venv = root / venv_name
        if not venv.is_dir():
            continue
        for site in venv.glob("lib/python*/site-packages"):
            if not site.is_dir():
                continue
            for child in site.iterdir():
                name = child.name
                if not child.is_dir():
                    continue
                if name.startswith((".", "_")):
                    continue
                if any(name.endswith(suf) for suf in _DIST_INFO_SUFFIXES):
                    continue
                if name not in found:
                    found[name] = child.resolve()

    nm = root / _NODE_MODULES
    if nm.is_dir():
        for child in nm.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith((".", "@")):
                continue
            if name not in found:
                found[name] = child.resolve()

    return found


_IDENTIFIER = re.compile(r"^[A-Za-z_][\w\-]*$")


def parse_query_prefix(query: str, root: Path) -> tuple[str | None, str]:
    """Split ``"django: queryset filter"`` into ``("django", "queryset filter")``.

    The prefix is recognized only when:
    1. There's a single colon-separated head before the rest of the query,
    2. the head is a single identifier (letters, digits, underscore, hyphen),
       so we don't false-positive on dotted qnames like
       ``django.db.models:QuerySet``,
    3. the head matches a directory name discovered under ``root``'s
       installed packages — *or* a directory under ``.snapctx/vendor/``
       (already-indexed package, even if the source has since been
       deleted).

    Returns ``(None, original_query)`` when the prefix doesn't qualify so
    the caller falls back to repo-only routing.
    """
    if ":" not in query:
        return None, query
    head, rest = query.split(":", 1)
    head_clean = head.strip()
    if not _IDENTIFIER.match(head_clean):
        return None, query
    known = set(discover_packages(root)) | set(list_indexed_vendors(root))
    if head_clean not in known:
        # A name like ``foo:bar`` where ``foo`` isn't a package is
        # almost certainly a qname and should pass through unchanged.
        return None, query
    return head_clean, rest.strip()


def vendor_index_dir(root: Path, name: str) -> Path:
    """``<root>/.snapctx/vendor/<name>/`` — the per-package storage dir."""
    return (root / ".snapctx" / "vendor" / name).resolve()


def list_indexed_vendors(root: Path) -> list[str]:
    """Names of packages with an existing per-package index."""
    base = root / ".snapctx" / "vendor"
    if not base.is_dir():
        return []
    return sorted(
        d.name for d in base.iterdir()
        if d.is_dir() and (d / "index.db").exists()
    )


def is_vendor_indexed(root: Path, name: str) -> bool:
    return (vendor_index_dir(root, name) / "index.db").exists()


def ensure_vendor_indexed(
    root: Path, name: str, *, force: bool = False
) -> dict | None:
    """Build (or refresh) the per-package index for ``name`` under ``root``.

    Returns the indexer summary, or ``None`` when the package is unknown
    (not installed under any discovered venv / node_modules) and we have
    nothing to index. Existing indexes are skipped unless ``force=True``.
    """
    if not force and is_vendor_indexed(root, name):
        return None

    packages = discover_packages(root)
    pkg_path = packages.get(name)
    if pkg_path is None:
        sys.stderr.write(
            f"snapctx: package {name!r} not found under {root} "
            f"(checked .venv/, venv/, env/, node_modules/)\n"
        )
        return None

    sys.stderr.write(
        f"snapctx: indexing vendor package {name} at {pkg_path} (one-time)...\n"
    )
    from snapctx.api._indexer import index_vendor_package
    summary = index_vendor_package(root, name, pkg_path)
    sys.stderr.write(
        f"snapctx: vendor package {name} ready "
        f"({summary['files_updated']} files, {summary['symbols_indexed']} symbols).\n"
    )
    return summary


def forget_vendor(root: Path, name: str) -> bool:
    """Delete a package's per-package index directory. Returns True if removed."""
    import shutil

    d = vendor_index_dir(root, name)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True
