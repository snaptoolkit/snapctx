from __future__ import annotations

from pathlib import Path

from snapctx.parsers.python import PythonParser


def test_typing_overload_stubs_are_skipped(tmp_path: Path) -> None:
    """@t.overload / @typing.overload / @overload stubs share a qname
    with the real implementation. The parser must skip the stubs so
    the real impl wins the qname."""
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "mod.py").write_text(
        "import typing as t\n"
        "\n"
        "@t.overload\n"
        "def get(silent: t.Literal[False] = False) -> int: ...\n"
        "\n"
        "@t.overload\n"
        "def get(silent: bool = ...) -> int | None: ...\n"
        "\n"
        "def get(silent: bool = False) -> int | None:\n"
        '    """Real impl."""\n'
        "    return 42\n"
    )

    result = PythonParser().parse(src / "mod.py", tmp_path)
    fns = [s for s in result.symbols if s.qname == "pkg.mod:get"]
    assert len(fns) == 1, f"expected one ``get`` symbol, got {len(fns)}: {[s.line_start for s in fns]}"
    # The surviving symbol must be the REAL implementation, not a stub.
    assert fns[0].docstring == "Real impl."


def test_extracts_symbols_with_correct_qnames(tmp_path: Path) -> None:
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "mod.py").write_text(
        '"""Top doc."""\n\n'
        "def top_level():\n"
        '    """Top-level fn."""\n'
        "    return 1\n\n"
        "class Widget:\n"
        '    """A widget."""\n\n'
        "    def spin(self) -> None:\n"
        '        """Spin."""\n'
        "        return None\n"
    )

    result = PythonParser().parse(src / "mod.py", tmp_path)
    qnames = {s.qname: s.kind for s in result.symbols}
    assert qnames == {
        "pkg.mod:": "module",
        "pkg.mod:top_level": "function",
        "pkg.mod:Widget": "class",
        "pkg.mod:Widget.spin": "method",
    }
    module_sym = next(s for s in result.symbols if s.kind == "module")
    assert module_sym.docstring == "Top doc."


def test_decorator_arg_calls_not_attributed_to_decorated_fn(tmp_path: Path) -> None:
    """@decorator(arg=OtherCall()) → OtherCall() evaluates at module load and
    must NOT be attributed as a callee of the decorated function.

    Without this, every Celery task picks up its decorator's OpenApiParameter
    calls as spurious callees and pollutes the call graph.
    """
    (tmp_path / "m.py").write_text(
        "def describe(x): return x\n"
        "def wrap(**kw): return lambda f: f\n"
        "\n"
        "@wrap(param=describe('hello'))\n"
        "def target():\n"
        "    wrap()\n"
    )
    result = PythonParser().parse(tmp_path / "m.py", tmp_path)
    target_calls = [c for c in result.calls if c.caller_qname == "m:target"]
    names = {c.callee_name for c in target_calls}
    # Only the runtime call inside target() should be attributed.
    assert names == {"wrap"}, names
    # describe('hello') happens at decorator-evaluation (module-load) time.
    assert "describe" not in names


def test_default_value_calls_not_attributed(tmp_path: Path) -> None:
    """`def f(x=factory()):` — the factory() evaluates once at def time,
    not per call. Don't attribute it as f's callee."""
    (tmp_path / "m.py").write_text(
        "def factory(): return 1\n"
        "def use(): return 2\n"
        "def f(x=factory()):\n"
        "    return use()\n"
    )
    result = PythonParser().parse(tmp_path / "m.py", tmp_path)
    f_calls = {c.callee_name for c in result.calls if c.caller_qname == "m:f"}
    assert f_calls == {"use"}


def test_forward_self_call_resolves_after_post_pass(tmp_path: Path) -> None:
    """When a method defined earlier in a class calls a method defined later,
    the parse-time lookup can't find the target yet. The index's
    promote_self_calls post-pass should still resolve it once all symbols
    are in."""
    from snapctx.api import index_root
    from snapctx.index import Index, db_path_for

    (tmp_path / "m.py").write_text(
        "class C:\n"
        "    def early(self):\n"
        "        self.late()\n"
        "    def late(self):\n"
        "        return 42\n"
    )
    index_root(tmp_path)
    idx = Index(db_path_for(tmp_path))
    try:
        rows = idx.conn.execute(
            "SELECT caller_qname, callee_qname, callee_name FROM calls "
            "WHERE caller_qname = 'm:C.early'"
        ).fetchall()
    finally:
        idx.close()
    assert rows
    assert rows[0]["callee_qname"] == "m:C.late"


def test_module_symbol_from_leading_comment_block(tmp_path: Path) -> None:
    """Files with no triple-quoted docstring but a substantial leading `#`
    comment block should still get a module symbol."""
    (tmp_path / "settings.py").write_text(
        "#!/usr/bin/env python3\n"
        "# -*- coding: utf-8 -*-\n"
        "# Django settings for the production environment.\n"
        "#\n"
        "# Configures Redis for sessions, CORS for the frontend origin,\n"
        "# and the Celery broker for async task execution.\n"
        "\n"
        "import os\n"
    )
    result = PythonParser().parse(tmp_path / "settings.py", tmp_path)
    mod = next((s for s in result.symbols if s.kind == "module"), None)
    assert mod is not None
    assert "Django settings for the production environment." in mod.docstring
    assert "Celery broker" in mod.docstring


def test_leading_comment_skips_separators_and_directives(tmp_path: Path) -> None:
    """Pure `# ----` separators and `# noqa`-style directives must not count
    toward the module doc content threshold — the module symbol still
    exists (issue #21) but its ``docstring`` field is None."""
    (tmp_path / "a.py").write_text(
        "# ==========================\n"
        "# noqa\n"
        "# type: ignore\n"
        "import os\n"
    )
    result = PythonParser().parse(tmp_path / "a.py", tmp_path)
    mod = next((s for s in result.symbols if s.kind == "module"), None)
    assert mod is not None
    assert mod.docstring is None


def test_leading_comment_ignores_short_content(tmp_path: Path) -> None:
    """A tiny comment isn't documentation — module symbol exists with
    ``docstring=None`` rather than absorbing the trivial header."""
    (tmp_path / "short.py").write_text("# hello\nimport os\n")
    result = PythonParser().parse(tmp_path / "short.py", tmp_path)
    mod = next((s for s in result.symbols if s.kind == "module"), None)
    assert mod is not None
    assert mod.docstring is None


def test_leading_comment_rejects_commented_out_code(tmp_path: Path) -> None:
    """A block of commented-out imports / stubs is NOT module documentation —
    common in abandoned test fixtures. Module symbol still emitted with
    ``docstring=None`` so callers can address the file as a whole."""
    (tmp_path / "t.py").write_text(
        "# from mixer.backend.django import mixer\n"
        "# from django.test import TestCase\n"
        "# from apps.docs.models import Folder, Doc, DocHistory\n"
        "# from .base_view import BaseApiTestCase\n"
        "\n"
        "def test_x(): pass\n"
    )
    result = PythonParser().parse(tmp_path / "t.py", tmp_path)
    mod = next((s for s in result.symbols if s.kind == "module"), None)
    assert mod is not None
    assert mod.docstring is None


def test_leading_comment_mixed_prose_and_code(tmp_path: Path) -> None:
    """Mostly-prose with an example ``# import`` line should still be docs."""
    (tmp_path / "m.py").write_text(
        "# Helpers for parsing YAML config files. We keep the loader\n"
        "# minimal and let callers plug in their own schema validation.\n"
        "# Example usage:\n"
        "#     from config.loader import load_yaml\n"
        "#     config = load_yaml('app.yml')\n"
        "import yaml\n"
    )
    result = PythonParser().parse(tmp_path / "m.py", tmp_path)
    mod = next((s for s in result.symbols if s.kind == "module"), None)
    assert mod is not None
    assert "parsing YAML config files" in mod.docstring


def test_docstring_preferred_over_comments(tmp_path: Path) -> None:
    """When both are present, the triple-quoted docstring wins."""
    (tmp_path / "m.py").write_text(
        "# This is some comment header that should be ignored here since\n"
        "# there is a proper docstring below.\n"
        '"""Real docstring."""\n'
        "import os\n"
    )
    result = PythonParser().parse(tmp_path / "m.py", tmp_path)
    mod = next((s for s in result.symbols if s.kind == "module"), None)
    assert mod is not None
    assert mod.docstring == "Real docstring."


def test_module_symbol_emitted_even_without_docstring(tmp_path: Path) -> None:
    """Every parsed file gets a kind='module' symbol so callers can
    address it as a whole — ``snapctx_source <file>:`` and
    ``snapctx_edit_symbol <file>:`` need a row to point at. Files
    without a docstring have ``docstring=None``. Issue #21."""
    (tmp_path / "bare.py").write_text("def f(): pass\n")
    result = PythonParser().parse(tmp_path / "bare.py", tmp_path)
    mod = next((s for s in result.symbols if s.kind == "module"), None)
    assert mod is not None
    assert mod.docstring is None
    assert mod.line_start == 1
    assert mod.line_end >= 1


def test_resolves_imported_call(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "from b import helper\n\n"
        "def caller():\n"
        "    helper(1)\n"
    )
    (tmp_path / "b.py").write_text("def helper(x): return x\n")

    result = PythonParser().parse(tmp_path / "a.py", tmp_path)
    assert len(result.calls) == 1
    assert result.calls[0].callee_name == "helper"
    assert result.calls[0].callee_qname == "b:helper"


def test_resolves_local_constructor_call(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        "class Thing: pass\n\n"
        "def make():\n"
        "    return Thing()\n"
    )
    result = PythonParser().parse(tmp_path / "mod.py", tmp_path)
    call = next(c for c in result.calls if c.callee_name == "Thing")
    assert call.callee_qname == "mod:Thing"


def test_self_attribute_chain_is_not_mis_resolved(tmp_path: Path) -> None:
    """`self.attr.method()` should leave callee_qname unresolved — we don't guess."""
    (tmp_path / "mod.py").write_text(
        "class C:\n"
        "    def __init__(self):\n"
        "        self.cache = {}\n"
        "    def use(self):\n"
        "        self.cache.pop('x', None)\n"
    )
    result = PythonParser().parse(tmp_path / "mod.py", tmp_path)
    pop_calls = [c for c in result.calls if c.callee_name == "self.cache.pop"]
    assert pop_calls and pop_calls[0].callee_qname is None


def test_signature_renders_kwonly_and_defaults(tmp_path: Path) -> None:
    (tmp_path / "s.py").write_text(
        "def f(a, b: int = 2, *, c: str, d: bool = False) -> None:\n"
        "    return None\n"
    )
    result = PythonParser().parse(tmp_path / "s.py", tmp_path)
    fn = next(s for s in result.symbols if s.kind == "function")
    assert fn.signature == "def f(a, b: int = 2, *, c: str, d: bool = False) -> None"


def test_syntax_error_returns_empty_result(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def f(:\n")
    result = PythonParser().parse(tmp_path / "bad.py", tmp_path)
    assert result.symbols == [] and result.calls == [] and result.imports == []


def test_class_bases_captured(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "class Base: pass\n"
        "class Child(Base, object): pass\n"
    )
    result = PythonParser().parse(tmp_path / "m.py", tmp_path)
    child = next(s for s in result.symbols if s.qname == "m:Child")
    assert child.bases == ["Base", "object"]


def test_self_call_resolves_via_mixin_base(tmp_path: Path) -> None:
    """`self.mixin_method()` inside a class that extends an imported mixin
    should resolve to <mixin_qname>.mixin_method (optimistic — validated later
    by the index's post-ingest demotion pass)."""
    (tmp_path / "mixins.py").write_text(
        "class UtilMixin:\n"
        "    def helper(self): ...\n"
    )
    (tmp_path / "app.py").write_text(
        "from mixins import UtilMixin\n"
        "class App(UtilMixin):\n"
        "    def run(self):\n"
        "        self.helper()\n"
    )
    result = PythonParser().parse(tmp_path / "app.py", tmp_path)
    call = next(c for c in result.calls if c.callee_name == "self.helper")
    assert call.callee_qname == "mixins:UtilMixin.helper"
