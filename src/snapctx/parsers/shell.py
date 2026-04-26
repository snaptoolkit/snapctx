"""Shell-script parser (.sh, .bash).

Extracts:

- **module** — the script as a whole, with its leading comment block as
  the docstring (the closest thing shell has to one).
- **function** — every function definition. Both POSIX (``name() { … }``)
  and ksh-style (``function name { … }`` or ``function name() { … }``).
  Body extent found via brace-depth tracking that respects single-/
  double-quoted strings and ``#`` end-of-line comments.
- **import** — ``source <file>`` and ``. <file>`` directives. Module
  path is recorded relative to the script (``./lib/util.sh`` →
  ``lib/util``) so cross-script lookups work the same way as Python's
  dotted imports.
- **call** — invocations of *intra-file* functions. We can only
  resolve calls to functions also defined in this script (the parser
  doesn't see what other sourced files contain). Calls to external
  binaries (``aws``, ``docker``, ``git``) are intentionally not emitted —
  they aren't symbols in any indexable sense.

Known limitations:

- Heredocs (``<<EOF … EOF``) aren't tracked when matching braces; a
  heredoc body containing an unbalanced ``{`` would confuse function-
  end detection. Rare enough in practice that we don't pay tree-sitter's
  weight to fix it for v1.
- Functions defined inside other functions (nested) get the same
  parent_qname as top-level ones; shell's lexical scoping is loose
  enough that this rarely matters for retrieval.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from snapctx.qname import make_qname, typescript_module_path
from snapctx.schema import Call, Import, ParseResult, Symbol


# Function definition forms. We deliberately stop *before* the body-
# opening ``{`` so the post-match ``find("{", end)`` can locate it
# unambiguously. Two alternatives:
#   POSIX:   ``[function ]name() …``    — name in group 1
#   KSH:     ``function name …``         — name in group 2
_FUNC_RE = re.compile(
    r"""
    ^[ \t]*
    (?:
        (?:function[ \t]+)?              # optional 'function' keyword
        ([A-Za-z_][A-Za-z0-9_\-]*)       # 1: POSIX name
        [ \t]*\(\)                       # parens
      |
        function[ \t]+
        ([A-Za-z_][A-Za-z0-9_\-]*)       # 2: ksh name
        (?![ \t]*\()                     # not followed by parens (handled above)
    )
    """,
    re.MULTILINE | re.VERBOSE,
)

_SOURCE_RE = re.compile(
    r"""
    ^[ \t]*
    (?:source|\.)[ \t]+              # ``source`` or ``.``
    ['"]?                            # optional quote
    ([^\s'";]+)                      # group 1: path
    """,
    re.MULTILINE | re.VERBOSE,
)


class ShellParser:
    """Parser entry point. Implements ``parsers.base.Parser`` protocol."""

    language = "shell"
    extensions = (".sh", ".bash")

    def parse(self, path: Path, root: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        module = typescript_module_path(path, root)  # slash-separated, like .ts
        file_str = str(path.resolve())

        symbols: list[Symbol] = []
        calls: list[Call] = []
        imports: list[Import] = []

        # Module-level symbol — the whole script. Leading comment block
        # (after an optional shebang) is the closest thing to a docstring.
        line_count = source.count("\n") + (0 if source.endswith("\n") else 1)
        module_qname = make_qname(module, [])
        symbols.append(Symbol(
            qname=module_qname,
            kind="module",
            language=self.language,
            signature=f"shell script {module}",
            docstring=_leading_comment_block(source),
            file=file_str,
            line_start=1,
            line_end=max(1, line_count),
            parent_qname=None,
            source_sha=_sha(source),
        ))

        # Function definitions — find each header, then locate the
        # matching closing brace.
        functions = list(_iter_functions(source))
        defined_names = {name for name, _, _ in functions}
        for name, header_line, header_end_pos in functions:
            body_start_pos = source.find("{", header_end_pos)
            if body_start_pos < 0:
                continue
            end_pos = _match_brace(source, body_start_pos)
            if end_pos < 0:
                continue
            line_start = source.count("\n", 0, header_end_pos) + 1
            line_end = source.count("\n", 0, end_pos) + 1
            body = source[body_start_pos + 1 : end_pos]

            qname = make_qname(module, [name])
            symbols.append(Symbol(
                qname=qname,
                kind="function",
                language=self.language,
                signature=f"{name}()",
                docstring=_leading_comment_block_before(source, header_line),
                file=file_str,
                line_start=line_start,
                line_end=line_end,
                parent_qname=module_qname,
                source_sha=_sha(body),
            ))

            # Intra-file function calls: scan the body for tokens that
            # match a defined function name at command position (start of
            # a statement). Bash command position = start of a logical
            # line, after ``;``, ``&&``, ``||``, ``|``, or ``$( ``.
            for call_name, call_line_offset in _find_command_tokens(
                body, defined_names - {name}
            ):
                calls.append(Call(
                    caller_qname=qname,
                    callee_name=call_name,
                    callee_qname=make_qname(module, [call_name]),
                    file=file_str,
                    line=line_start + call_line_offset,
                ))

        # source / dot imports.
        for m in _SOURCE_RE.finditer(source):
            raw = m.group(1)
            line = source.count("\n", 0, m.start()) + 1
            imports.append(Import(
                file=file_str,
                module=_normalize_source_path(raw),
                name=None,
                alias=None,
                line=line,
            ))

        return ParseResult(
            symbols=symbols, calls=calls, imports=imports, language=self.language,
        )


# ---------- helpers ----------


def _iter_functions(source: str):
    """Yield (name, header_line_no, header_end_pos) for each function header."""
    for m in _FUNC_RE.finditer(source):
        name = m.group(1) or m.group(2)
        if not name:
            continue
        line_no = source.count("\n", 0, m.start()) + 1
        yield name, line_no, m.end()


def _match_brace(source: str, open_pos: int) -> int:
    """Return index of the ``}`` that closes the ``{`` at ``open_pos``.

    Tracks single- and double-quoted strings (with ``\\`` escape inside
    double quotes) and ``#`` end-of-line comments so braces inside those
    don't confuse depth counting. Returns -1 if no match found.
    """
    depth = 0
    i = open_pos
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
            i += 1
        elif ch == "'":
            j = source.find("'", i + 1)
            if j < 0:
                return -1
            i = j + 1
        elif ch == '"':
            i += 1
            while i < n:
                if source[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if source[i] == '"':
                    i += 1
                    break
                i += 1
        elif ch == "#":
            # Comment to end of line; only counts as a comment when at
            # token start (after whitespace, ``;``, ``\n``, etc.).
            if i == 0 or source[i - 1] in " \t\n;|&(":
                j = source.find("\n", i)
                i = n if j < 0 else j + 1
            else:
                i += 1
        else:
            i += 1
    return -1


_COMMAND_BREAKERS = "\n;&|()`"


def _find_command_tokens(body: str, names: set[str]):
    """Yield (name, line_offset) for each call to a name in ``names`` at
    command position within ``body``. Line offsets are 0-based relative
    to the start of the body.
    """
    if not names:
        return
    n = len(body)
    i = 0
    at_command_start = True
    while i < n:
        ch = body[i]
        if ch == "\n":
            at_command_start = True
            i += 1
            continue
        if ch in _COMMAND_BREAKERS:
            at_command_start = True
            i += 1
            continue
        if ch in " \t":
            i += 1
            continue
        if ch == "#":
            j = body.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "'":
            j = body.find("'", i + 1)
            i = n if j < 0 else j + 1
            at_command_start = False
            continue
        if ch == '"':
            i += 1
            while i < n:
                if body[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if body[i] == '"':
                    i += 1
                    break
                i += 1
            at_command_start = False
            continue
        # Identifier?
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (body[j].isalnum() or body[j] in "_-"):
                j += 1
            tok = body[i:j]
            if at_command_start and tok in names:
                line_offset = body.count("\n", 0, i)
                yield tok, line_offset
            at_command_start = False
            i = j
            continue
        at_command_start = False
        i += 1


def _normalize_source_path(raw: str) -> str:
    """Normalize a sourced-file path into a slash-separated module form
    matching ``typescript_module_path`` output (extension stripped).
    """
    p = raw.lstrip("./")
    for ext in (".sh", ".bash"):
        if p.endswith(ext):
            p = p[: -len(ext)]
            break
    return p


def _leading_comment_block(source: str) -> str | None:
    """Return the leading comment block as a docstring, ignoring shebangs."""
    lines = source.splitlines()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("#!"):
            continue
        if s.startswith("#"):
            out.append(s.lstrip("#").lstrip())
            continue
        if not s:
            if out:
                break
            continue
        break
    if not out:
        return None
    return "\n".join(out).strip() or None


def _leading_comment_block_before(source: str, header_line: int) -> str | None:
    """Return any contiguous ``# …`` lines immediately above the header line."""
    lines = source.splitlines()
    if header_line < 2:
        return None
    out: list[str] = []
    for i in range(header_line - 2, -1, -1):
        s = lines[i].strip()
        if s.startswith("#"):
            out.append(s.lstrip("#").lstrip())
            continue
        if not s:
            break
        break
    if not out:
        return None
    return "\n".join(reversed(out)).strip() or None


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()
