"""Map file extension → parser instance."""

from __future__ import annotations

from snapctx.parsers.base import Parser
from snapctx.parsers.python import PythonParser
from snapctx.parsers.shell import ShellParser
from snapctx.parsers.typescript import TypeScriptParser

_PARSERS: list[Parser] = [PythonParser(), TypeScriptParser(), ShellParser()]

_BY_EXT: dict[str, Parser] = {
    ext: p for p in _PARSERS for ext in p.extensions
}


def parser_for(suffix: str) -> Parser | None:
    return _BY_EXT.get(suffix)


def supported_extensions() -> tuple[str, ...]:
    return tuple(_BY_EXT.keys())


def extensions_for_languages(languages: frozenset[str] | None) -> tuple[str, ...]:
    """Return the union of file extensions handled by the named languages.

    ``languages=None`` means "every registered parser" — same result as
    ``supported_extensions()``. Used by the walker to honor the
    ``[walker].languages`` config knob.
    """
    if languages is None:
        return supported_extensions()
    out: list[str] = []
    for p in _PARSERS:
        if p.language in languages:
            out.extend(p.extensions)
    return tuple(out)


def known_languages() -> tuple[str, ...]:
    return tuple(p.language for p in _PARSERS)
