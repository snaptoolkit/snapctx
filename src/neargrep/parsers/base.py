"""Parser protocol. Implementations live in sibling modules."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from neargrep.schema import ParseResult


class Parser(Protocol):
    """A language parser extracts symbols, calls, and imports from a source file."""

    language: str
    extensions: tuple[str, ...]

    def parse(self, path: Path, root: Path) -> ParseResult: ...
