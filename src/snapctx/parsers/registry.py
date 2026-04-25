"""Map file extension → parser instance."""

from __future__ import annotations

from snapctx.parsers.base import Parser
from snapctx.parsers.python import PythonParser
from snapctx.parsers.typescript import TypeScriptParser

_PARSERS: list[Parser] = [PythonParser(), TypeScriptParser()]

_BY_EXT: dict[str, Parser] = {
    ext: p for p in _PARSERS for ext in p.extensions
}


def parser_for(suffix: str) -> Parser | None:
    return _BY_EXT.get(suffix)


def supported_extensions() -> tuple[str, ...]:
    return tuple(_BY_EXT.keys())
