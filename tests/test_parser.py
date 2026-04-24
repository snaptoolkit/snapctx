from __future__ import annotations

from pathlib import Path

from neargrep.parsers.python import PythonParser


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
        "pkg.mod:top_level": "function",
        "pkg.mod:Widget": "class",
        "pkg.mod:Widget.spin": "method",
    }


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
    sig = result.symbols[0].signature
    assert sig == "def f(a, b: int = 2, *, c: str, d: bool = False) -> None"


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
