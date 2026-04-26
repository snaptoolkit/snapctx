"""Cross-package call resolution at query time.

When a call inside a vendor package's index has ``callee_qname=NULL``
(unresolved at parse time), the symbol may live in a *different* vendor
package whose index has also been built. This resolver:

1. Looks at the imports of the calling file.
2. Matches the callee name against import names / aliases / module paths.
3. Identifies which package the symbol is supposed to come from (first
   component of the imported module path).
4. If that package has an index at
   ``<repo_root>/.snapctx/vendor/<pkg>/index.db``, opens it and searches
   for the resolved qname.

Honors the explicit-prefix rule: we *only* peek into packages that the
user has already chosen to index — never spontaneously fan out into
something that wasn't requested.

Resolved hits carry a ``package`` tag so the consumer can distinguish
in-package edges from cross-package edges in expand / context output.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from snapctx.index import Index, db_path_for

# Names that are never worth cross-resolving: ``self.X`` is intra-class
# (handled by promote_self_calls); ``super.X`` is dynamic; bare builtins
# like ``isinstance`` aren't installed packages.
_NOISE_PREFIXES = ("self.", "this.", "super.", "cls.")


class CrossPackageResolver:
    """Per-operation cache of opened sibling indexes + import tables.

    Lifetime is one expand / context call. An ``expand`` that traverses
    50 cross-package edges shouldn't reopen the same DB 50 times, and
    shouldn't re-query imports for the same file 50 times.
    """

    def __init__(self, repo_root: Path, current_scope: str | None) -> None:
        self.repo_root = repo_root
        self.current_scope = current_scope
        self._open: dict[str, Index | None] = {}
        self._imports: dict[str, list[sqlite3.Row]] = {}

    def close(self) -> None:
        for idx in self._open.values():
            if idx is not None:
                idx.close()
        self._open.clear()

    def __enter__(self) -> CrossPackageResolver:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _open_pkg(self, name: str) -> Index | None:
        if name in self._open:
            return self._open[name]
        if name == self.current_scope:
            self._open[name] = None  # don't traverse into our own index
            return None
        db = db_path_for(self.repo_root, scope=name)
        if not db.exists():
            self._open[name] = None
            return None
        idx = Index(db)
        self._open[name] = idx
        return idx

    def _imports_for(self, caller_idx: Index, file: str) -> list[sqlite3.Row]:
        rows = self._imports.get(file)
        if rows is None:
            rows = caller_idx.imports_for_file(file)
            self._imports[file] = rows
        return rows

    def resolve(
        self, callee_name: str, caller_file: str, caller_idx: Index
    ) -> dict | None:
        """Try to resolve ``callee_name`` into a sibling vendor package.

        Returns ``{"package": str, "row": sqlite3.Row}`` on a hit; ``None``
        otherwise. The row is from the resolved package's index — same
        schema as the home index's ``symbols`` table.
        """
        if not callee_name:
            return None
        if any(callee_name.startswith(p) for p in _NOISE_PREFIXES):
            return None

        parts = callee_name.split(".")
        head = parts[0]
        rest = parts[1:]

        for imp in self._imports_for(caller_idx, caller_file):
            module = imp["module"]
            name = imp["name"]
            alias = imp["alias"]
            if not module:
                continue

            # Three import shapes drive different chain construction.
            if name is not None:
                # ``from <module> import <name> [as <alias>]``
                bound = alias or name
                if bound != head:
                    continue
                target_pkg = module.split(".")[0]
                in_pkg_module = (
                    "" if module == target_pkg
                    else module[len(target_pkg) + 1:]
                )
                # The bound name itself is the imported symbol; ``rest``
                # is whatever method / attribute chain follows.
                full_chain = [name, *rest]
            else:
                # ``import <module> [as <alias>]``
                bound = alias or module.split(".")[0]
                if bound != head:
                    continue
                target_pkg = module.split(".")[0]
                in_pkg_module = (
                    "" if module == target_pkg
                    else module[len(target_pkg) + 1:]
                )
                if alias is None:
                    # ``import asgiref.sync`` then ``asgiref.sync.X(...)`` —
                    # the rest re-traverses the module path that
                    # ``in_pkg_module`` already represents. Skip those parts.
                    skip = (
                        len(in_pkg_module.split(".")) if in_pkg_module else 0
                    )
                    full_chain = rest[skip:]
                else:
                    # Alias replaces the full module reference; rest starts
                    # immediately after the alias.
                    full_chain = list(rest)

            pkg_idx = self._open_pkg(target_pkg)
            if pkg_idx is None:
                continue

            for cand in _candidate_qnames(in_pkg_module, full_chain):
                row = pkg_idx.get_symbol(cand)
                if row is not None:
                    return {"package": target_pkg, "row": row}

        return None


def _candidate_qnames(in_pkg_module: str, chain: list[str]) -> list[str]:
    """Generate qname shapes to try for a (module, dotted-name-chain) target.

    The qname grammar is ``<module>:<member.path>``; given a chain of N
    parts, we don't statically know which prefix is part of the module
    path vs the member path. Enumerate progressively: at split index
    ``i``, parts ``[:i]`` extend the module and parts ``[i:]`` form the
    member. The caller checks each candidate against the package's
    symbols table.
    """
    if not chain:
        # Imported the package or module itself with no further access.
        return [f"{in_pkg_module}:"] if in_pkg_module else [":"]
    out: list[str] = []
    for i in range(len(chain)):
        mod_extra = ".".join(chain[:i])
        member = ".".join(chain[i:])
        if mod_extra:
            mod = f"{in_pkg_module}.{mod_extra}" if in_pkg_module else mod_extra
        else:
            mod = in_pkg_module
        out.append(f"{mod}:{member}")
    return out
