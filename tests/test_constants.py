"""Tests for module-level and class-level constant indexing."""

from __future__ import annotations

from pathlib import Path

from neargrep.parsers.python import PythonParser


def test_module_constants_captured(tmp_path: Path) -> None:
    (tmp_path / "cfg.py").write_text(
        'DEFAULT_MODEL = "claude-3-5-sonnet"\n'
        "MAX_RETRIES = 3\n"
        "TIMEOUT: float = 30.0\n"
        "lower = 'not a constant'\n"
    )
    result = PythonParser().parse(tmp_path / "cfg.py", tmp_path)
    const_qnames = {s.qname: s for s in result.symbols if s.kind == "constant"}
    assert "cfg:DEFAULT_MODEL" in const_qnames
    assert "cfg:MAX_RETRIES" in const_qnames
    assert "cfg:TIMEOUT" in const_qnames
    assert "cfg:lower" not in const_qnames  # lowercase, no annotation — skipped

    assert const_qnames["cfg:DEFAULT_MODEL"].signature.endswith(
        "= 'claude-3-5-sonnet'"
    )
    assert const_qnames["cfg:TIMEOUT"].signature == "TIMEOUT: float = 30.0"


def test_class_level_constants_captured(tmp_path: Path) -> None:
    (tmp_path / "agents.py").write_text(
        "class AnthropicAgent:\n"
        '    DEFAULT_MODEL = "claude-3-5-sonnet"\n'
        '    def chat(self): pass\n'
    )
    result = PythonParser().parse(tmp_path / "agents.py", tmp_path)
    const = next(
        s for s in result.symbols
        if s.kind == "constant" and s.qname == "agents:AnthropicAgent.DEFAULT_MODEL"
    )
    assert const.parent_qname == "agents:AnthropicAgent"
    assert "claude-3-5-sonnet" in const.signature


def test_constant_referencing_another_name(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        'PRIMARY_MODEL = "claude-3-5"\n'
        "FALLBACK = PRIMARY_MODEL\n"
    )
    result = PythonParser().parse(tmp_path / "m.py", tmp_path)
    qnames = {s.qname for s in result.symbols if s.kind == "constant"}
    assert "m:PRIMARY_MODEL" in qnames
    assert "m:FALLBACK" in qnames
