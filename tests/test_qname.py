from __future__ import annotations

from pathlib import Path

import pytest

from snapctx.qname import (
    identifier_parts,
    make_qname,
    python_module_path,
    split_identifier,
    validate_writable_qname,
)


def test_python_module_path_collapses_init(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    assert python_module_path(tmp_path / "pkg" / "__init__.py", tmp_path) == "pkg"


def test_python_module_path_nested(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    f = tmp_path / "pkg" / "sub" / "mod.py"
    f.write_text("")
    assert python_module_path(f, tmp_path) == "pkg.sub.mod"


def test_make_qname_module_only() -> None:
    assert make_qname("pkg.mod", []) == "pkg.mod:"


def test_make_qname_with_members() -> None:
    assert make_qname("pkg.mod", ["Widget", "spin"]) == "pkg.mod:Widget.spin"


def test_split_identifier_camel_and_snake() -> None:
    assert split_identifier("SessionManager.refresh_token") == [
        "session", "manager", "refresh", "token",
    ]


def test_split_identifier_acronym_preserved() -> None:
    # HTTPServer -> http server (consecutive caps collapse into one token
    # until a lowercase follows)
    assert split_identifier("HTTPServer") == ["http", "server"]


def test_identifier_parts_joins_with_spaces() -> None:
    assert identifier_parts("pkg.Auth:Login") == "pkg auth login"


# ---------------------------------------------------------------------------
# validate_writable_qname — write primitives must reject empty-symbol qnames
# (silent data-loss bug: ``"module:"`` was accepted and treated as
# "the whole module", replacing/deleting the entire file).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "qname",
    [
        "pkg.mod:Symbol",
        "pkg.mod:Symbol.method",
        "pkg.deep.mod:Klass.inner.method",
        "path/to/file:Symbol",
        "frontend/lib/api:getLexicon",
        "frontend/lib/api:default",
    ],
)
def test_validate_writable_qname_accepts_valid(qname: str) -> None:
    # No raise == accepted.
    validate_writable_qname(qname)


@pytest.mark.parametrize(
    "qname",
    [
        "pkg.mod:",
        "backend.translation.services:",
        "frontend/lib/api:",
    ],
)
def test_validate_writable_qname_rejects_empty_symbol(qname: str) -> None:
    with pytest.raises(ValueError, match="empty symbol after colon"):
        validate_writable_qname(qname)


def test_validate_writable_qname_rejects_missing_colon() -> None:
    with pytest.raises(ValueError, match="missing ':' separator"):
        validate_writable_qname("backend/bible/models")


def test_validate_writable_qname_rejects_empty_module() -> None:
    with pytest.raises(ValueError, match="empty module before colon"):
        validate_writable_qname(":Verse")


@pytest.mark.parametrize("qname", ["", "   ", "\t\n"])
def test_validate_writable_qname_rejects_empty_or_whitespace(qname: str) -> None:
    with pytest.raises(ValueError, match="empty or whitespace-only"):
        validate_writable_qname(qname)


def test_validate_writable_qname_rejects_none() -> None:
    with pytest.raises(TypeError, match="expected str"):
        validate_writable_qname(None)  # type: ignore[arg-type]


def test_validate_writable_qname_message_names_offending_qname() -> None:
    bad = "backend.translation.services:"
    with pytest.raises(ValueError) as exc:
        validate_writable_qname(bad)
    # The hint must include the bad qname so logs are actionable.
    assert bad in str(exc.value)
