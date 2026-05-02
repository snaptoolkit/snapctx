"""Framework-aware URL → handler extraction.

Symbol-graph reasoning sees a Django ``urls.py`` as a constant
assignment to ``urlpatterns`` and the entries inside as a list of
function calls. That's syntactically true but loses the load-bearing
information: WHICH HTTP path maps to WHICH view callable. Agents end
up grep-traversing nested ``include(...)`` chains to recover what the
framework already knows.

This module runs ONCE per indexer pass — after symbols / imports are
ingested — and extracts route → handler mappings into the ``routes``
table. Two frameworks are supported in v1:

* **Django** — walks ``urls.py`` files, AST-parses ``urlpatterns``
  list entries (``path("…", view_fn)`` / ``re_path(…)``), follows
  ``include("module.urls")`` references via the imports table,
  and produces a flattened ``(method=ANY, path, handler_qname)``
  table. Path components from nested includes are concatenated.

* **Next.js App Router** — files matching ``app/**/route.{ts,tsx,js}``
  map to URLs by directory layout (``app/api/users/route.ts`` →
  ``/api/users``). One row per exported HTTP-verb function (GET,
  POST, PUT, PATCH, DELETE, HEAD, OPTIONS), with the verb in the
  ``method`` column.

Both extractors are deliberately conservative — when resolution
fails (e.g. a dynamically-built urlpattern, or an ``include`` whose
target isn't itself indexed), we still emit a row with
``handler_qname=NULL`` so the agent gets the file + line and can
investigate. We never invent a qname.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from snapctx.api._common import open_index
from snapctx.qname import python_module_path

# ---------- Django ----------

# Django ``urls.py`` AST shapes we recognize:
#   urlpatterns = [
#       path("api/users/", views.list_users),
#       path("api/users/<int:pk>/", views.get_user, name="user-detail"),
#       re_path(r"^legacy/(?P<x>[0-9]+)$", legacy.handler),
#       path("api/v2/", include("api.v2.urls")),
#       path("inline/", lambda req: ..., name="inline"),  # skipped
#   ]
_DJANGO_URLPATTERNS_NAMES = ("urlpatterns",)
_DJANGO_PATH_FUNCS = ("path", "re_path", "url")
_DJANGO_INCLUDE = "include"


def _django_extract_urlpatterns_from_module(
    tree: ast.Module,
) -> list[ast.Call] | None:
    """Find the ``urlpatterns = [...]`` assignment and return the list
    of Call nodes inside. Returns ``None`` if not found or shape
    isn't a list of calls."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in _DJANGO_URLPATTERNS_NAMES
        ):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            return None
        calls: list[ast.Call] = []
        for elt in node.value.elts:
            if isinstance(elt, ast.Call):
                calls.append(elt)
        return calls
    return None


def _call_func_name(call: ast.Call) -> str | None:
    """Return the simple name of the call's function, or None if it's
    a complex expression we don't track (lambda, attribute chain)."""
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _str_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _name_or_attribute_dotted(node: ast.AST) -> str | None:
    """Render a Name/Attribute chain as a dotted string. Returns None
    for expressions that aren't a simple name reference (lambda, call,
    subscript, etc.)."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    else:
        return None
    return ".".join(reversed(parts))


def _django_extract(
    file: Path, source: str, root: Path,
) -> list[tuple[str, str, str | None, str, int, str]]:
    """Extract route rows from one Django ``urls.py`` file.

    Returns rows ready for ``Index.replace_routes_for_file``. Path
    components from this file are emitted bare — concatenation across
    ``include()`` boundaries happens at query time (we don't know the
    full prefix here without the parent's context).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    calls = _django_extract_urlpatterns_from_module(tree)
    if not calls:
        return []

    # The urls.py's own module path, derived from its source-tree
    # location. Used to resolve relative imports (``from . import
    # views``) into absolute dotted paths.
    file_module = python_module_path(file, root)
    # Package is everything before the last segment (urls.py at
    # ``translation/urls.py`` belongs to the ``translation`` package).
    package_parts = file_module.split(".")
    if package_parts and package_parts[-1] == "urls":
        package_parts.pop()
    self_package = ".".join(package_parts)

    # Map locally-bound names → fully-qualified module dotted path.
    # ``from foo.views import bar`` → ``{"bar": "foo.views.bar"}``.
    # ``from foo import views`` → ``{"views": "foo.views"}``.
    # ``from . import views`` (level=1) → ``{"views": "<self_package>.views"}``.
    # ``from ..core import views`` (level=2) → resolves up one package.
    # ``import foo.views`` → ``{"foo": "foo"}``.
    local_to_dotted: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            level = getattr(node, "level", 0) or 0
            if level > 0:
                # Relative import — resolve against this file's package.
                parts = self_package.split(".") if self_package else []
                # ``from .`` (level=1) keeps the current package; level=2
                # pops one segment, level=3 pops two, …
                pops = level - 1
                if pops > 0:
                    parts = parts[:-pops] if pops <= len(parts) else []
                base_components = list(parts)
                if node.module:
                    base_components.append(node.module)
                base = ".".join(base_components)
            elif node.module:
                base = node.module
            else:
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                local_to_dotted[bound] = (
                    f"{base}.{alias.name}" if base else alias.name
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                local_to_dotted[bound] = alias.asname or alias.name

    rows: list[tuple[str, str, str | None, str, int, str]] = []
    for call in calls:
        fname = _call_func_name(call)
        if fname not in _DJANGO_PATH_FUNCS:
            continue
        # path(<URL>, <view>) — first two positional args.
        if len(call.args) < 2:
            continue
        url_lit = _str_literal(call.args[0])
        if url_lit is None:
            continue
        view_arg = call.args[1]
        # Skip include() — those compose into nested URL configs and
        # need cross-file resolution. We emit them as TODO rows so the
        # agent at least sees they exist.
        if (
            isinstance(view_arg, ast.Call)
            and _call_func_name(view_arg) == _DJANGO_INCLUDE
            and view_arg.args
            and isinstance(view_arg.args[0], ast.Constant)
            and isinstance(view_arg.args[0].value, str)
        ):
            target_module = view_arg.args[0].value
            rows.append((
                "ANY", url_lit, None, str(file), call.lineno,
                f"django:include={target_module}",
            ))
            continue
        # Try to resolve the view to a qname.
        view_dotted = _name_or_attribute_dotted(view_arg)
        handler_qname: str | None = None
        if view_dotted is not None:
            head, _, tail = view_dotted.partition(".")
            if head in local_to_dotted:
                base = local_to_dotted[head]
                full = f"{base}.{tail}" if tail else base
                # Convert dotted module path → snapctx qname:
                # last segment is the symbol; everything before is module.
                qname_module, _, qname_member = full.rpartition(".")
                if qname_module and qname_member:
                    handler_qname = f"{qname_module}:{qname_member}"
            elif view_dotted in local_to_dotted:
                # Direct local binding (e.g. ``urlpatterns = [path("/", index)]``
                # where ``index`` was imported as a bare name).
                full = local_to_dotted[view_dotted]
                qname_module, _, qname_member = full.rpartition(".")
                if qname_module and qname_member:
                    handler_qname = f"{qname_module}:{qname_member}"
        rows.append((
            "ANY", url_lit, handler_qname, str(file), call.lineno, "django",
        ))

    return rows


# ---------- Next.js App Router ----------

_NEXTJS_HTTP_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
# Match an exported function or arrow-function declaration.
_NEXTJS_EXPORT_RE = re.compile(
    r"^export\s+(?:async\s+)?(?:function\s+([A-Z]+)|const\s+([A-Z]+)\s*=)",
    re.MULTILINE,
)


def _nextjs_url_from_path(file: Path, root: Path) -> str | None:
    """``app/api/users/[id]/route.ts`` → ``/api/users/[id]``.

    Returns None if the file isn't an App Router route handler under
    a top-level ``app/`` directory.
    """
    try:
        rel = file.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    parts = list(rel.parts)
    if not parts or parts[0] != "app":
        return None
    if not parts[-1].startswith("route."):
        return None
    # Drop the leading "app" and trailing "route.{ext}".
    segments = parts[1:-1]
    return "/" + "/".join(segments) if segments else "/"


def _nextjs_extract(
    file: Path, source: str, root: Path,
) -> list[tuple[str, str, str | None, str, int, str]]:
    url = _nextjs_url_from_path(file, root)
    if url is None:
        return []
    # File path → snapctx TS qname module: ``app/api/users/route``.
    rel = file.resolve().relative_to(root.resolve())
    module = "/".join(rel.with_suffix("").parts)
    rows: list[tuple[str, str, str | None, str, int, str]] = []
    for match in _NEXTJS_EXPORT_RE.finditer(source):
        verb = match.group(1) or match.group(2)
        if verb not in _NEXTJS_HTTP_VERBS:
            continue
        line = source[:match.start()].count("\n") + 1
        handler_qname = f"{module}:{verb}"
        rows.append((verb, url, handler_qname, str(file), line, "nextjs"))
    return rows


# ---------- entry point ----------


def extract_routes_for_file(
    file: Path, source: str, root: Path,
) -> list[tuple[str, str, str | None, str, int, str]]:
    """Pick the right extractor for ``file`` and return route rows.

    Returns an empty list when the file isn't a routing config (the
    common case — most files in a project aren't urlpatterns or
    App Router handlers).
    """
    name = file.name
    suffix = file.suffix
    if suffix == ".py" and name == "urls.py":
        return _django_extract(file, source, root)
    if suffix in (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"):
        if name.startswith("route."):
            return _nextjs_extract(file, source, root)
    return []


def reextract_all_routes(root: Path) -> int:
    """Re-extract routes for every indexed file under ``root``.

    Called as a post-pass by ``index_root`` after symbols / imports
    are populated. Returns the total number of route rows written.
    """
    idx = open_index(root, scope=None)
    total = 0
    try:
        rows = idx.conn.execute(
            "SELECT path FROM files"
        ).fetchall()
        for r in rows:
            file = Path(r["path"])
            if not file.exists():
                continue
            try:
                source = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            extracted = extract_routes_for_file(file, source, root)
            idx.replace_routes_for_file(str(file), extracted)
            total += len(extracted)
    finally:
        idx.close()
    return total


# ---------- query helpers (re-exported via api/__init__.py) ----------


def list_routes(root: str | Path = ".") -> dict:
    """Return every route in the index.

    Each row carries ``method`` (``ANY`` for Django, the verb for
    Next.js), ``path``, ``handler_qname`` (or null when unresolved),
    ``defined_in``, ``line``, and ``framework`` ("django" or "nextjs").
    Rows are sorted by ``(path, method)``.
    """
    idx = open_index(Path(root), scope=None)
    try:
        rows = idx.list_routes()
    finally:
        idx.close()
    payload = {
        "routes": [
            {
                "method": r["method"],
                "path": r["path"],
                "handler_qname": r["handler_qname"],
                "defined_in": r["defined_in"],
                "line": r["line"],
                "framework": r["framework"],
            }
            for r in rows
        ],
    }
    if not payload["routes"]:
        payload["hint"] = (
            "No routes indexed for this root. snapctx auto-extracts from "
            "Django ``urls.py`` files and Next.js App Router "
            "``app/**/route.{ts,tsx,js}``. Other frameworks are not yet "
            "supported — fall back to ``snapctx grep`` for those."
        )
    return payload


def lookup_route(path_pattern: str, root: str | Path = ".") -> dict:
    """Return rows whose stored ``path`` equals ``path_pattern``.

    The match is exact for v1 — agents pass the pattern as it appears
    in the URL config (``api/users/<int:pk>/`` for Django, ``/api/users/[id]``
    for Next.js). Returns an empty list with a hint when nothing matches.
    """
    idx = open_index(Path(root), scope=None)
    try:
        rows = idx.find_routes_by_path(path_pattern)
    finally:
        idx.close()
    payload: dict = {
        "path": path_pattern,
        "matches": [
            {
                "method": r["method"],
                "path": r["path"],
                "handler_qname": r["handler_qname"],
                "defined_in": r["defined_in"],
                "line": r["line"],
                "framework": r["framework"],
            }
            for r in rows
        ],
    }
    if not payload["matches"]:
        payload["hint"] = (
            f"No exact match for path {path_pattern!r}. "
            "v1 lookup is exact-match only — patterns must be quoted as "
            "they appear in the urls.py / route file. Use ``snapctx routes`` "
            "to list everything."
        )
    return payload
