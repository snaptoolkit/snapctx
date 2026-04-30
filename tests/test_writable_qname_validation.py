"""Empty-symbol qnames must NOT silently destroy files.

Regression for the production bug where ``edit_symbol(qname="m:", ...)``
was accepted and treated as "the whole module", replacing or deleting
the entire file. The same bug existed in every write primitive.

Each test below:

1. Builds a tiny indexed repo with a known file body.
2. Calls a write primitive with ``"pkg.math:"`` — note the trailing
   colon and the EMPTY symbol after it.
3. Asserts the call raises ``ValueError`` (so the agent sees a tool-call
   failure, not a "success" with destructive effect).
4. Asserts the file on disk is BYTE-FOR-BYTE unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snapctx.api import (
    delete_symbol,
    edit_symbol,
    edit_symbol_batch,
    edit_symbol_search_replace,
    edit_symbol_search_replace_batch,
    index_root,
    insert_symbol,
    rename_symbol,
)

EMPTY_SYMBOL = "pkg.math:"


def _build_repo(tmp_path: Path) -> tuple[Path, Path, bytes]:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    target = repo / "pkg" / "math.py"
    target.write_text(
        '"""Math helpers."""\n'
        "\n"
        "\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def mul(a, b):\n"
        "    return a * b\n"
    )
    index_root(repo)
    return repo, target, target.read_bytes()


def test_edit_symbol_rejects_empty_symbol_qname(tmp_path: Path) -> None:
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="empty symbol after colon"):
        edit_symbol(EMPTY_SYMBOL, "x = 1\n", root=repo)
    assert target.read_bytes() == before


def test_edit_symbol_batch_rejects_empty_symbol_qname(tmp_path: Path) -> None:
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="empty symbol after colon"):
        edit_symbol_batch(
            [{"qname": EMPTY_SYMBOL, "new_body": "x = 1\n"}],
            root=repo,
        )
    assert target.read_bytes() == before


def test_edit_symbol_batch_rejects_when_one_edit_is_empty(
    tmp_path: Path,
) -> None:
    """One bad qname in a batch must fail the WHOLE call. We can't apply
    any edits if one is potentially destructive — the agent would see a
    half-success and not retry.
    """
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="empty symbol after colon"):
        edit_symbol_batch(
            [
                {
                    "qname": "pkg.math:add",
                    "new_body": "def add(a, b):\n    return a + b + 0\n",
                },
                {"qname": EMPTY_SYMBOL, "new_body": "x = 1\n"},
            ],
            root=repo,
        )
    assert target.read_bytes() == before


def test_edit_symbol_search_replace_rejects_empty_symbol_qname(
    tmp_path: Path,
) -> None:
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="empty symbol after colon"):
        edit_symbol_search_replace(
            EMPTY_SYMBOL, search="a + b", replace="a + b + 0", root=repo,
        )
    assert target.read_bytes() == before


def test_edit_symbol_search_replace_batch_rejects_empty_symbol_qname(
    tmp_path: Path,
) -> None:
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="empty symbol after colon"):
        edit_symbol_search_replace_batch(
            [{"qname": EMPTY_SYMBOL, "search": "a + b", "replace": "a + b + 0"}],
            root=repo,
        )
    assert target.read_bytes() == before


def test_insert_symbol_rejects_empty_anchor_qname(tmp_path: Path) -> None:
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="empty symbol after colon"):
        insert_symbol(
            anchor_qname=EMPTY_SYMBOL,
            new_text="def new():\n    return 1\n",
            root=repo,
        )
    assert target.read_bytes() == before


def test_delete_symbol_rejects_empty_symbol_qname(tmp_path: Path) -> None:
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="empty symbol after colon"):
        delete_symbol(EMPTY_SYMBOL, root=repo)
    assert target.read_bytes() == before


def test_rename_symbol_rejects_empty_symbol_qname(tmp_path: Path) -> None:
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="empty symbol after colon"):
        rename_symbol(EMPTY_SYMBOL, "renamed", root=repo)
    assert target.read_bytes() == before


# --- the other "shape" rejections ------------------------------------------


def test_edit_symbol_rejects_qname_without_colon(tmp_path: Path) -> None:
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="missing ':' separator"):
        edit_symbol("pkg.math", "x = 1\n", root=repo)
    assert target.read_bytes() == before


def test_edit_symbol_rejects_empty_module(tmp_path: Path) -> None:
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="empty module before colon"):
        edit_symbol(":add", "x = 1\n", root=repo)
    assert target.read_bytes() == before


def test_edit_symbol_rejects_empty_string(tmp_path: Path) -> None:
    repo, target, before = _build_repo(tmp_path)
    with pytest.raises(ValueError, match="empty or whitespace-only"):
        edit_symbol("", "x = 1\n", root=repo)
    assert target.read_bytes() == before
