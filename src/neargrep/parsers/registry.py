"""Map file extension → parser instance."""

from __future__ import annotations

from neargrep.parsers.base import Parser
from neargrep.parsers.python import PythonParser

_PARSERS: list[Parser] = [PythonParser()]

_BY_EXT: dict[str, Parser] = {
    ext: p for p in _PARSERS for ext in p.extensions
}


def parser_for(suffix: str) -> Parser | None:
    return _BY_EXT.get(suffix)


def supported_extensions() -> tuple[str, ...]:
    return tuple(_BY_EXT.keys())
