"""Tests for the TypeScript / TSX parser."""

from __future__ import annotations

from pathlib import Path

from neargrep.parsers.typescript import TypeScriptParser


def _parse(tmp_path: Path, filename: str, source: str):
    f = tmp_path / filename
    f.write_text(source)
    return TypeScriptParser().parse(f, tmp_path)


def test_module_docstring_from_leading_jsdoc(tmp_path: Path) -> None:
    """A /** … */ block at the top of a file should produce a module symbol."""
    r = _parse(
        tmp_path,
        "runner.ts",
        "/**\n"
        " * Task runner for streaming script output.\n"
        " *\n"
        " * Publishes deltas on a Redis pub/sub channel.\n"
        " */\n"
        "export function run() {}\n",
    )
    mod = next(s for s in r.symbols if s.kind == "module")
    assert mod.qname == "runner:"
    assert "Task runner for streaming script output." in mod.docstring
    assert "Redis pub/sub channel" in mod.docstring


def test_no_module_symbol_for_plain_block_comment(tmp_path: Path) -> None:
    """A plain /* … */ (not /** … */) should NOT produce a module symbol —
    those are usually license headers, not documentation."""
    r = _parse(
        tmp_path,
        "licensed.ts",
        "/* Copyright 2024. All rights reserved. */\n"
        "export function run() {}\n",
    )
    assert not any(s.kind == "module" for s in r.symbols)


def test_no_module_symbol_without_leading_comment(tmp_path: Path) -> None:
    r = _parse(tmp_path, "bare.ts", "export function run() {}\n")
    assert not any(s.kind == "module" for s in r.symbols)


def test_function_declaration_is_captured(tmp_path: Path) -> None:
    r = _parse(
        tmp_path,
        "a.ts",
        "export function login(user: string, password: string): Promise<boolean> {\n"
        "  return Promise.resolve(true);\n"
        "}\n",
    )
    fn = next(s for s in r.symbols if s.qname == "a:login")
    assert fn.kind == "function"
    assert "login(user: string, password: string): Promise<boolean>" in fn.signature


def test_arrow_const_is_function(tmp_path: Path) -> None:
    r = _parse(
        tmp_path,
        "m.ts",
        "export const double = (x: number): number => x * 2;\n",
    )
    fn = next(s for s in r.symbols if s.qname == "m:double")
    assert fn.kind == "function"
    assert "const double" in fn.signature


def test_jsx_arrow_is_component(tmp_path: Path) -> None:
    r = _parse(
        tmp_path,
        "c.tsx",
        "export const Card = ({title}: {title: string}) => <div>{title}</div>;\n",
    )
    sym = next(s for s in r.symbols if s.qname == "c:Card")
    assert sym.kind == "component"


def test_class_methods_and_bases(tmp_path: Path) -> None:
    r = _parse(
        tmp_path,
        "s.ts",
        "import { Base } from './base';\n"
        "export class Session extends Base {\n"
        "  refresh(): void { return; }\n"
        "}\n",
    )
    cls = next(s for s in r.symbols if s.qname == "s:Session")
    assert cls.kind == "class"
    assert "Base" in cls.bases
    method = next(s for s in r.symbols if s.qname == "s:Session.refresh")
    assert method.kind == "method"


def test_interface_and_type_alias(tmp_path: Path) -> None:
    r = _parse(
        tmp_path,
        "t.ts",
        "export interface User { id: string; name: string; }\n"
        "export type Role = 'admin' | 'user';\n",
    )
    kinds = {s.qname: s.kind for s in r.symbols}
    assert kinds["t:User"] == "interface"
    assert kinds["t:Role"] == "type"


def test_module_level_constant(tmp_path: Path) -> None:
    r = _parse(
        tmp_path,
        "k.ts",
        'export const DEFAULT_MODEL = "claude-opus-4-5";\n',
    )
    c = next(s for s in r.symbols if s.qname == "k:DEFAULT_MODEL")
    assert c.kind == "constant"
    assert "claude-opus-4-5" in c.signature


def test_jsdoc_attaches_only_to_immediate_next(tmp_path: Path) -> None:
    r = _parse(
        tmp_path,
        "d.ts",
        "/** Documented. */\n"
        "export function alpha(): void {}\n"
        "\n"
        "export function beta(): void {}\n",
    )
    docs = {s.qname: s.docstring for s in r.symbols}
    assert docs["d:alpha"] == "Documented."
    assert docs["d:beta"] is None


def test_named_import_resolves_call_target(tmp_path: Path) -> None:
    r = _parse(
        tmp_path,
        "app.ts",
        "import { greet } from './util';\n"
        "export function run() { return greet('world'); }\n",
    )
    call = next(c for c in r.calls if c.callee_name == "greet")
    assert call.callee_qname == "util:greet"


def test_jsx_usage_is_captured_as_component_call(tmp_path: Path) -> None:
    r = _parse(
        tmp_path,
        "page.tsx",
        "import { Button } from './Button';\n"
        "export const Page = () => <Button label='hi' />;\n",
    )
    jsx_calls = [c for c in r.calls if c.callee_name == "<Button>"]
    assert jsx_calls
    assert jsx_calls[0].callee_qname == "Button:Button"


def test_plain_jsx_file_parses(tmp_path: Path) -> None:
    """A `.jsx` file (JavaScript + JSX, no TS types) parses via the TSX grammar."""
    r = _parse(
        tmp_path,
        "Card.jsx",
        "export const Card = ({ title }) => <div>{title}</div>;\n"
        "export function helper(x) { return x * 2; }\n",
    )
    kinds = {s.qname: s.kind for s in r.symbols}
    assert kinds["Card:Card"] == "component"
    assert kinds["Card:helper"] == "function"


def test_plain_js_file_parses(tmp_path: Path) -> None:
    """A `.js` file (plain JavaScript) parses — TS grammar is a superset of JS."""
    r = _parse(
        tmp_path,
        "util.js",
        "export function greet(name) { return 'hi ' + name; }\n"
        "export const PI = 3.14159;\n",
    )
    qnames = {s.qname for s in r.symbols}
    assert "util:greet" in qnames
    assert "util:PI" in qnames


def test_index_tsx_collapses_to_directory(tmp_path: Path) -> None:
    r = _parse(
        tmp_path,
        "index.tsx",
        "export const X = 1;\n",
    )
    # module path should be empty string (file is index.tsx directly at root);
    # qname becomes ":X"
    c = next(s for s in r.symbols if s.qname.endswith(":X"))
    assert c.qname == ":X"
