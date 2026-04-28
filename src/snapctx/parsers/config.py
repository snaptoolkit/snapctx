"""Config-file parsers — TOML, JSON, YAML, .env.

Each top-level key (and, for TOML, table header) becomes an indexed
symbol so an agent can find ``DATABASE_URL`` via
``snapctx_search "database url"`` instead of grepping every config in
the repo. The body extent for each key is the line of the key itself
plus continuation lines until the next key at the same indentation.

We deliberately don't try to be a *complete* TOML/YAML/JSON parser —
the indexer just needs key names and line ranges. For TOML we use
stdlib ``tomllib`` to walk the structure; for the rest we use simple
line scanners that handle the common forms (top-level keys,
``[table]`` headers, ``KEY=value`` pairs). Edge cases — multiline
strings, complex YAML anchors, JSON nested in JSON — fall through as
file-level module symbols only, which is still strictly more useful
than the previous "not indexed at all" behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

from snapctx.qname import make_qname
from snapctx.schema import ParseResult, Symbol


# ---------- shared helpers ----------


def _module_path(path: Path, root: Path) -> str:
    """Slash-separated path including extension (``app/.env``, ``cfg/db.toml``)."""
    rel = path.resolve().relative_to(root.resolve())
    return "/".join(rel.parts)


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _module_symbol(
    *,
    qname: str,
    language: str,
    file_str: str,
    signature: str,
    docstring: str | None,
    line_count: int,
    sha: str,
) -> Symbol:
    return Symbol(
        qname=qname,
        kind="module",
        language=language,
        signature=signature,
        docstring=docstring,
        file=file_str,
        line_start=1,
        line_end=max(1, line_count),
        parent_qname=None,
        source_sha=sha,
    )


def _key_symbol(
    *,
    qname: str,
    language: str,
    file_str: str,
    signature: str,
    name: str,
    line_start: int,
    line_end: int,
    parent_qname: str,
) -> Symbol:
    return Symbol(
        qname=qname,
        kind="constant",
        language=language,
        signature=signature,
        docstring=name,
        file=file_str,
        line_start=line_start,
        line_end=line_end,
        parent_qname=parent_qname,
        source_sha="",
    )


# ---------- TOML ----------


class TomlParser:
    language = "toml"
    extensions = (".toml",)

    def parse(self, path: Path, root: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        module = _module_path(path, root)
        file_str = str(path.resolve())
        lines = source.splitlines()
        line_count = max(1, len(lines))

        symbols: list[Symbol] = []
        module_qname = make_qname(module, [])
        symbols.append(_module_symbol(
            qname=module_qname, language=self.language, file_str=file_str,
            signature=f"toml {module}", docstring=_leading_comments(lines),
            line_count=line_count, sha=_sha(source),
        ))

        try:
            tomllib.loads(source)
        except tomllib.TOMLDecodeError:
            return ParseResult(symbols=symbols, calls=[], imports=[], language=self.language)

        # Walk lines: track current table header, emit a symbol for each
        # table header AND each key inside.
        table_re = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*(?:#.*)?$")
        key_re = re.compile(r"^\s*([A-Za-z_][\w\-\.]*)\s*=")

        current_table: list[str] = []
        current_table_qname = module_qname
        current_table_start: int | None = None

        # Pre-compute table line ranges by scanning for headers.
        table_starts: list[tuple[int, list[str]]] = []
        for i, line in enumerate(lines, start=1):
            m = table_re.match(line)
            if m:
                table_starts.append((i, [p.strip() for p in m.group(1).split(".")]))

        def _table_end(idx: int) -> int:
            if idx + 1 < len(table_starts):
                return table_starts[idx + 1][0] - 1
            return line_count

        for idx, (start_line, parts) in enumerate(table_starts):
            end_line = _table_end(idx)
            qname = make_qname(module, parts)
            symbols.append(Symbol(
                qname=qname,
                kind="class",
                language=self.language,
                signature=f"[{'.'.join(parts)}]",
                docstring=".".join(parts),
                file=file_str,
                line_start=start_line,
                line_end=end_line,
                parent_qname=module_qname,
                source_sha="",
            ))

        # Keys: assign each key line to the most recent table header.
        for i, line in enumerate(lines, start=1):
            m = key_re.match(line)
            if not m:
                continue
            key = m.group(1)
            # Find the enclosing table by line.
            parents: list[str] = []
            parent_qname = module_qname
            for start_line, parts in table_starts:
                if start_line <= i:
                    parents = parts
                    parent_qname = make_qname(module, parts)
                else:
                    break
            qname = make_qname(module, parents + [key])
            symbols.append(_key_symbol(
                qname=qname, language=self.language, file_str=file_str,
                signature=line.strip(), name=key,
                line_start=i, line_end=i, parent_qname=parent_qname,
            ))

        return ParseResult(symbols=symbols, calls=[], imports=[], language=self.language)


# ---------- JSON ----------


class JsonParser:
    language = "json"
    extensions = (".json",)

    def parse(self, path: Path, root: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        module = _module_path(path, root)
        file_str = str(path.resolve())
        lines = source.splitlines()
        line_count = max(1, len(lines))

        symbols: list[Symbol] = []
        module_qname = make_qname(module, [])
        symbols.append(_module_symbol(
            qname=module_qname, language=self.language, file_str=file_str,
            signature=f"json {module}", docstring=None,
            line_count=line_count, sha=_sha(source),
        ))

        try:
            data = json.loads(source)
        except json.JSONDecodeError:
            return ParseResult(symbols=symbols, calls=[], imports=[], language=self.language)
        if not isinstance(data, dict):
            return ParseResult(symbols=symbols, calls=[], imports=[], language=self.language)

        # Top-level keys only — line numbers via a regex scan.
        key_re = re.compile(r'^\s*"([^"\\]+)"\s*:')
        # Track brace depth so we only emit top-level keys.
        depth = 0
        for i, line in enumerate(lines, start=1):
            for ch in line:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth < 0:
                        depth = 0
            stripped = line.lstrip()
            if not stripped or stripped.startswith("//"):
                continue
            # We want depth==1 at the START of the line (i.e. directly inside
            # the outer object). Recompute the at-line-start depth by undoing
            # this line's contribution.
            line_open = sum(1 for ch in line if ch == "{")
            line_close = sum(1 for ch in line if ch == "}")
            at_start_depth = depth - line_open + line_close
            if at_start_depth != 1:
                continue
            m = key_re.match(line)
            if not m:
                continue
            key = m.group(1)
            qname = make_qname(module, [key])
            symbols.append(_key_symbol(
                qname=qname, language=self.language, file_str=file_str,
                signature=line.strip().rstrip(","), name=key,
                line_start=i, line_end=i, parent_qname=module_qname,
            ))

        return ParseResult(symbols=symbols, calls=[], imports=[], language=self.language)


# ---------- YAML ----------


class YamlParser:
    """Lightweight YAML scanner — top-level keys only, no value parsing.

    Avoids a pyyaml dependency. We only need (key_name, line_no) for
    indexing; value structure is irrelevant to the search use case.
    """

    language = "yaml"
    extensions = (".yaml", ".yml")

    def parse(self, path: Path, root: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        module = _module_path(path, root)
        file_str = str(path.resolve())
        lines = source.splitlines()
        line_count = max(1, len(lines))

        symbols: list[Symbol] = []
        module_qname = make_qname(module, [])
        symbols.append(_module_symbol(
            qname=module_qname, language=self.language, file_str=file_str,
            signature=f"yaml {module}", docstring=_leading_comments(lines),
            line_count=line_count, sha=_sha(source),
        ))

        key_re = re.compile(r"^([A-Za-z_][\w\-]*)\s*:(?:\s|$)")
        for i, line in enumerate(lines, start=1):
            if line.startswith("#") or not line.strip():
                continue
            # Top-level key: zero leading whitespace.
            if line[:1] in (" ", "\t", "-"):
                continue
            m = key_re.match(line)
            if not m:
                continue
            key = m.group(1)
            qname = make_qname(module, [key])
            symbols.append(_key_symbol(
                qname=qname, language=self.language, file_str=file_str,
                signature=line.rstrip(), name=key,
                line_start=i, line_end=i, parent_qname=module_qname,
            ))

        return ParseResult(symbols=symbols, calls=[], imports=[], language=self.language)


# ---------- .env ----------


class EnvParser:
    """Parser for ``.env``-style files.

    Treats every top-level ``KEY=value`` line as a constant symbol.
    No interpolation, quoting, or comments-in-values handling — just
    enough to make ``DATABASE_URL``, ``DEBUG``, etc. searchable.
    """

    language = "env"
    extensions = (".env",)

    def parse(self, path: Path, root: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        module = _module_path(path, root)
        file_str = str(path.resolve())
        lines = source.splitlines()
        line_count = max(1, len(lines))

        symbols: list[Symbol] = []
        module_qname = make_qname(module, [])
        symbols.append(_module_symbol(
            qname=module_qname, language=self.language, file_str=file_str,
            signature=f"env {module}", docstring=_leading_comments(lines),
            line_count=line_count, sha=_sha(source),
        ))

        key_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Z0-9_a-z]*)\s*=")
        for i, line in enumerate(lines, start=1):
            if line.lstrip().startswith("#") or not line.strip():
                continue
            m = key_re.match(line)
            if not m:
                continue
            key = m.group(1)
            qname = make_qname(module, [key])
            symbols.append(_key_symbol(
                qname=qname, language=self.language, file_str=file_str,
                signature=line.rstrip(), name=key,
                line_start=i, line_end=i, parent_qname=module_qname,
            ))

        return ParseResult(symbols=symbols, calls=[], imports=[], language=self.language)


# ---------- shared ----------


def _leading_comments(lines: list[str]) -> str | None:
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("#"):
            out.append(s.lstrip("#").strip())
            continue
        if not s:
            if out:
                break
            continue
        break
    if not out:
        return None
    return " ".join(out)
