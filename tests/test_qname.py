from __future__ import annotations

from pathlib import Path

from snapctx.qname import identifier_parts, make_qname, python_module_path, split_identifier


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
