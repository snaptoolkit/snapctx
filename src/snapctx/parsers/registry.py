"""Map file extension → parser instance."""

from __future__ import annotations

from snapctx.parsers.base import Parser
from snapctx.parsers.config import EnvParser, JsonParser, TomlParser, YamlParser
from snapctx.parsers.markdown import MarkdownParser
from snapctx.parsers.python import PythonParser
from snapctx.parsers.shell import ShellParser
from snapctx.parsers.typescript import TypeScriptParser

_PARSERS: list[Parser] = [
    PythonParser(),
    TypeScriptParser(),
    ShellParser(),
    MarkdownParser(),
    TomlParser(),
    YamlParser(),
    JsonParser(),
    EnvParser(),
]

_BY_EXT: dict[str, Parser] = {
    ext: p for p in _PARSERS for ext in p.extensions
}


def parser_for(suffix: str) -> Parser | None:
    return _BY_EXT.get(suffix)


def parser_for_path(path) -> Parser | None:
    """Pick a parser for a Path, handling dotfile-as-extension cases.

    ``Path(".env").suffix`` is empty, so a plain suffix lookup misses
    dotfiles whose extension *is* their entire name. Try the suffix
    first, then fall back to ``"." + name`` (so ``.env`` matches the
    ``EnvParser``'s registered ``.env`` extension).
    """
    p = _BY_EXT.get(path.suffix)
    if p is not None:
        return p
    return _BY_EXT.get(f".{path.name}" if not path.name.startswith(".") else path.name)


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
