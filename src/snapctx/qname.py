"""Qualified-name formatting and identifier splitting.

Format (stable across languages; colon separates module from member path):
    <module_path>:<member_path>

Python:      myapp.auth.session:SessionManager.refresh
TypeScript:  myapp/auth/session:SessionManager.refresh   (module uses '/')
"""

from __future__ import annotations

import re
from pathlib import Path


def python_module_path(file: Path, root: Path) -> str:
    """Convert a .py path under `root` into a dotted module path.

    root=/repo, file=/repo/src/myapp/auth/session.py -> 'src.myapp.auth.session'
    __init__.py collapses to the package itself.
    """
    rel = file.resolve().relative_to(root.resolve())
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def typescript_module_path(file: Path, root: Path) -> str:
    """Convert a .ts/.tsx path under ``root`` into a slash-separated module path.

    Matches TS import semantics: ``import x from './foo/bar'`` points at
    file path, not a dotted package. ``index.ts`` / ``index.tsx`` collapse
    to the directory (like Python ``__init__``).

    root=/repo, file=/repo/src/auth/session.ts  -> 'src/auth/session'
    root=/repo, file=/repo/src/auth/index.tsx   -> 'src/auth'
    """
    rel = file.resolve().relative_to(root.resolve())
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "index":
        parts = parts[:-1]
    return "/".join(parts)


def make_qname(module: str, member_path: list[str]) -> str:
    """'myapp.auth', ['SessionManager', 'refresh'] -> 'myapp.auth:SessionManager.refresh'.
    An empty member_path means the module itself: 'myapp.auth:'.
    """
    return f"{module}:{'.'.join(member_path)}" if member_path else f"{module}:"


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def split_identifier(name: str) -> list[str]:
    """Split a qname/identifier into lower-case tokens for FTS indexing.

    'SessionManager.refresh_token' -> ['session', 'manager', 'refresh', 'token']
    """
    tokens: list[str] = []
    # split on non-word separators first (dots, colons, slashes)
    for chunk in re.split(r"[^\w]+", name):
        if not chunk:
            continue
        # snake_case
        for piece in chunk.split("_"):
            if not piece:
                continue
            # camelCase / PascalCase
            for sub in _CAMEL_BOUNDARY.split(piece):
                if sub:
                    tokens.append(sub.lower())
    return tokens


def identifier_parts(name: str) -> str:
    """Space-joined identifier tokens, suitable for an FTS5 column."""
    return " ".join(split_identifier(name))
