"""Tests for the TypeScript / TSX parser."""

from __future__ import annotations

from pathlib import Path

from snapctx.parsers.typescript import TypeScriptParser


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


def test_module_symbol_emitted_with_no_docstring_for_plain_block_comment(
    tmp_path: Path,
) -> None:
    """A plain /* … */ (not /** … */) is a license header, not docs — so
    the module symbol still exists (issue #21) but its ``docstring``
    field is None."""
    r = _parse(
        tmp_path,
        "licensed.ts",
        "/* Copyright 2024. All rights reserved. */\n"
        "export function run() {}\n",
    )
    mod = next((s for s in r.symbols if s.kind == "module"), None)
    assert mod is not None
    assert mod.docstring is None


def test_module_symbol_emitted_without_leading_comment(tmp_path: Path) -> None:
    """Every TS file gets a module symbol so callers can address it via
    ``path/to/file:`` whether or not it has a JSDoc header. Issue #21."""
    r = _parse(tmp_path, "bare.ts", "export function run() {}\n")
    mod = next((s for s in r.symbols if s.kind == "module"), None)
    assert mod is not None
    assert mod.docstring is None
    assert mod.line_start == 1


def test_this_call_optimistically_resolves_to_enclosing_class(tmp_path: Path) -> None:
    """`this.method()` inside a class should produce a call edge pointing at
    the enclosing class's method — even before the promote post-pass."""
    r = _parse(
        tmp_path,
        "svc.ts",
        "class Svc {\n"
        "  run() { this.step(); }\n"
        "  step() {}\n"
        "}\n",
    )
    run_calls = [c for c in r.calls if c.caller_qname == "svc:Svc.run"]
    assert run_calls
    # Parse-time resolution uses the ClassQname.method shape.
    # If `step` is emitted before run's body visits the call, it's resolved
    # here; otherwise, promote_self_calls in the Index fills it in later.
    c = run_calls[0]
    assert c.callee_name == "this.step"
    # Either resolved at parse time, or None for promote to fix up.
    assert c.callee_qname in (None, "svc:Svc.step")


def test_this_call_with_forward_reference_resolved_by_promote(tmp_path: Path) -> None:
    """this.X calls are resolved by the promote post-pass even if the parser
    couldn't see the target at call-emit time."""
    from snapctx.api import index_root
    from snapctx.index import Index, db_path_for

    (tmp_path / "m.ts").write_text(
        "export class Svc {\n"
        "  early() {\n"
        "    this.late();\n"
        "  }\n"
        "  late() { return 42; }\n"
        "}\n",
    )
    index_root(tmp_path)
    idx = Index(db_path_for(tmp_path))
    try:
        rows = idx.conn.execute(
            "SELECT callee_qname FROM calls "
            "WHERE caller_qname = 'm:Svc.early' AND callee_name = 'this.late'"
        ).fetchall()
    finally:
        idx.close()
    assert rows and rows[0]["callee_qname"] == "m:Svc.late"


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


def test_forwardref_component_indexed(tmp_path: Path) -> None:
    """``const Button = React.forwardRef(...)`` — the standard shadcn/Radix
    pattern. PascalCase + call expression at module scope is a wrapped
    component; index it as kind='component'. Without this, every shadcn UI
    primitive (Button, Input, Dialog, etc.) is invisible to search."""
    r = _parse(
        tmp_path,
        "button.tsx",
        "import * as React from 'react'\n"
        "export interface ButtonProps { variant?: string }\n"
        "const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(\n"
        "  ({ variant }, ref) => <button ref={ref}>{variant}</button>\n"
        ")\n"
        "Button.displayName = 'Button'\n"
        "export { Button }\n",
    )
    qnames = {s.qname: s for s in r.symbols}
    assert "button:Button" in qnames
    assert qnames["button:Button"].kind == "component"


def test_memo_component_indexed(tmp_path: Path) -> None:
    """``const Memoized = React.memo(...)`` — same pattern as forwardRef."""
    r = _parse(
        tmp_path,
        "card.tsx",
        "import * as React from 'react'\n"
        "const Card = React.memo(({ title }: { title: string }) => <div>{title}</div>)\n"
        "export { Card }\n",
    )
    qnames = {s.qname: s for s in r.symbols}
    assert "card:Card" in qnames
    assert qnames["card:Card"].kind == "component"


def test_upper_case_constant_with_call_value_indexed(tmp_path: Path) -> None:
    """``const COMMANDS = makeCommands()`` at module scope — UPPER_CASE
    convention is strong enough to trust without inspecting the value
    expression. Mirrors the Python parser's behavior for dispatch tables
    and registries."""
    r = _parse(
        tmp_path,
        "registry.ts",
        "function makeCommands() { return [{ name: 'a' }, { name: 'b' }] }\n"
        "const COMMANDS = makeCommands()\n"
        "export { COMMANDS }\n",
    )
    qnames = {s.qname: s for s in r.symbols}
    assert "registry:COMMANDS" in qnames
    assert qnames["registry:COMMANDS"].kind == "constant"


def test_cva_factory_constant_indexed(tmp_path: Path) -> None:
    """``const buttonVariants = cva(...)`` — class-variance-authority is
    shadcn/Radix's idiom for variant-based styling, and these constants ARE
    the public API of a component file (``import { buttonVariants }``).
    Index camelCase consts whose RHS is a call to a known variant factory."""
    r = _parse(
        tmp_path,
        "variants.ts",
        "function cva(_: string) { return () => '' }\n"
        "const buttonVariants = cva('inline-flex')\n",
    )
    qnames = {s.qname: s for s in r.symbols}
    assert "variants:buttonVariants" in qnames
    assert qnames["variants:buttonVariants"].kind == "constant"


def test_tv_factory_constant_indexed(tmp_path: Path) -> None:
    """``const cardStyles = tv(...)`` — tailwind-variants is the other
    common variant factory used in the React ecosystem."""
    r = _parse(
        tmp_path,
        "styles.ts",
        "function tv(_: object) { return () => '' }\n"
        "const cardStyles = tv({ base: 'rounded' })\n",
    )
    qnames = {s.qname: s for s in r.symbols}
    assert "styles:cardStyles" in qnames
    assert qnames["styles:cardStyles"].kind == "constant"


def test_unknown_camelcase_factory_still_skipped(tmp_path: Path) -> None:
    """Without an annotation and without a recognised factory, camelCase
    consts assigned to call expressions are still skipped — otherwise we'd
    flood the index with private compute results."""
    r = _parse(
        tmp_path,
        "compute.ts",
        "function makeThing(): number { return 1 }\n"
        "const result = makeThing()\n",
    )
    qnames = {s.qname for s in r.symbols}
    assert "compute:result" not in qnames


def test_function_declaration_returning_jsx_is_component(tmp_path: Path) -> None:
    """Modern shadcn convention: ``function Button(props) { return <X/> }``
    instead of ``const Button = ({...}) => <X/>``. Should be classified as a
    component, not a plain function — otherwise ``--kind component`` filters
    out the entire shadcn registry."""
    r = _parse(
        tmp_path,
        "button.tsx",
        "function Button({ variant }: { variant?: string }) {\n"
        "  return <button>{variant}</button>\n"
        "}\n",
    )
    qnames = {s.qname: s for s in r.symbols}
    assert "button:Button" in qnames
    assert qnames["button:Button"].kind == "component"


def test_function_declaration_lowercase_stays_function(tmp_path: Path) -> None:
    """Lowercase name ⇒ regular function even if it happens to return JSX
    (rare but happens in helpers / render-prop callbacks)."""
    r = _parse(
        tmp_path,
        "render.tsx",
        "function renderItem(x: string) {\n"
        "  return <span>{x}</span>\n"
        "}\n",
    )
    qnames = {s.qname: s for s in r.symbols}
    assert "render:renderItem" in qnames
    assert qnames["render:renderItem"].kind == "function"


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
