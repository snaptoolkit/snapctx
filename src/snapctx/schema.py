"""Language-agnostic record types emitted by parsers and stored in the index."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SymbolKind = Literal[
    "function", "method", "class", "interface", "type", "component", "module", "constant"
]


@dataclass(slots=True)
class Symbol:
    qname: str
    kind: SymbolKind
    language: str
    signature: str
    docstring: str | None
    file: str
    line_start: int
    line_end: int
    parent_qname: str | None
    decorators: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)     # class-only; bare names as written
    source_sha: str = ""


@dataclass(slots=True)
class Call:
    caller_qname: str
    callee_name: str            # as it appears at the call site (e.g. "foo", "x.bar", "self.baz")
    callee_qname: str | None    # resolved qname, None if unresolved
    file: str
    line: int


@dataclass(slots=True)
class Import:
    file: str
    module: str                 # dotted module path ("os.path")
    name: str | None            # imported binding, None for `import X`
    alias: str | None           # `as X`
    line: int


@dataclass(slots=True)
class ParseResult:
    symbols: list[Symbol]
    calls: list[Call]
    imports: list[Import]
    language: str
