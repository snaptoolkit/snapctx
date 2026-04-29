"""``insert_symbol``: add a new top-level symbol adjacent to an anchor."""

from __future__ import annotations

from pathlib import Path

from snapctx.api import get_source, index_root, insert_symbol, outline


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "math.py").write_text(
        '"""Math helpers."""\n'
        "\n"
        "\n"
        "def add(a, b):\n"
        '    """Sum two numbers."""\n'
        "    return a + b\n"
        "\n"
        "\n"
        "def mul(a, b):\n"
        '    """Multiply two numbers."""\n'
        "    return a * b\n"
    )
    index_root(repo)
    return repo


def test_insert_after_anchor_appends_function(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)

    # Two leading blank lines = PEP-8 spacing between top-level fns.
    new_fn = (
        "\n\ndef sub(a, b):\n"
        '    """Subtract."""\n'
        "    return a - b\n"
    )
    result = insert_symbol("pkg.math:add", new_fn, root=repo, position="after")

    assert "error" not in result, result
    # 2 blank-line prefix + 3 body lines = 5 lines spliced in.
    assert result["lines_inserted"] == 5

    src = get_source("pkg.math:sub", root=repo)
    assert "error" not in src
    assert "return a - b" in src["source"]

    # Existing functions still resolve correctly.
    assert "error" not in get_source("pkg.math:add", root=repo)
    assert "error" not in get_source("pkg.math:mul", root=repo)


def test_insert_before_anchor_prepends_function(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)

    new_fn = (
        "def negate(x):\n"
        '    """Return -x."""\n'
        "    return -x\n"
        "\n\n"
    )
    result = insert_symbol("pkg.math:add", new_fn, root=repo, position="before")
    assert "error" not in result, result

    # New symbol resolves; existing add and mul still resolve.
    for q in ("pkg.math:negate", "pkg.math:add", "pkg.math:mul"):
        assert "error" not in get_source(q, root=repo)

    # Order in the file's outline: negate, add, mul (top-down).
    o = outline("pkg/math.py", root=repo)
    fn_names = [s["qname"].split(":", 1)[1] for s in o["symbols"] if s["kind"] == "function"]
    assert fn_names == ["negate", "add", "mul"]


def test_insert_unknown_anchor_returns_not_found(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = insert_symbol("pkg.math:missing", "def x(): pass\n", root=repo)
    assert result["error"] == "not_found"
    assert (repo / "pkg" / "math.py").read_text().count("def add") == 1


def test_insert_refuses_when_file_changed(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    (repo / "pkg" / "math.py").write_text(
        "# external edit\n" + (repo / "pkg" / "math.py").read_text()
    )

    result = insert_symbol("pkg.math:add", "def x(): pass\n", root=repo)
    assert result["error"] == "stale_coordinates"


def test_insert_invalid_position(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = insert_symbol(
        "pkg.math:add", "def x(): pass\n",
        root=repo, position="middle",  # type: ignore[arg-type]
    )
    assert result["error"] == "invalid_position"


def test_insert_refuses_python_syntax_error(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    bad = "\n\ndef bad(a:\n    return a\n"  # truncated annotation
    result = insert_symbol("pkg.math:add", bad, root=repo)
    assert result["error"] == "syntax_error"
    assert "unparseable" in result["hint"]
    # File still parses, original symbols intact.
    src = (repo / "pkg" / "math.py").read_text()
    import ast as _ast
    _ast.parse(src)
    assert "def bad" not in src


def test_insert_then_subsequent_edit_resolves(tmp_path: Path) -> None:
    """After inserting, the next op (edit on a sibling) must still find
    the right line range — exercises the in-process re-index."""
    repo = _build_repo(tmp_path)

    insert_symbol(
        "pkg.math:add",
        "\n\ndef double(x):\n    return x + x\n",
        root=repo, position="after",
    )

    from snapctx.api import edit_symbol
    result = edit_symbol(
        "pkg.math:mul",
        "def mul(a, b):\n"
        '    """Multiply two numbers (now defensively)."""\n'
        "    return a * b if a is not None else 0\n",
        root=repo,
    )
    assert "error" not in result

    src = get_source("pkg.math:mul", root=repo)
    assert "defensively" in src["source"]


def test_opencode_bridge_insert_symbol_arg_mapping(tmp_path: Path) -> None:
    """The opencode TS wrapper sends ``{anchor_qname, position, new_text}``;
    the bridge fans those into ``api.insert_symbol(root=root, **args)``.

    Regression for #11: the wrapper previously sent ``file=`` and ``body=``,
    which the Python API rejects. This test pins the contract by replaying
    exactly what the wrapper sends through ``_snapctx_writer.py``.
    """
    import json as _json
    import subprocess
    import sys

    repo = _build_repo(tmp_path)
    bridge = Path(__file__).resolve().parent.parent / "opencode" / "tools" / "_snapctx_writer.py"
    assert bridge.exists(), bridge

    payload = {
        "op": "insert_symbol",
        "root": str(repo),
        "args": {
            "anchor_qname": "pkg.math:add",
            "position": "after",
            "new_text": "\n\ndef triple(x):\n    return x * 3\n",
        },
    }
    result = subprocess.run(
        [sys.executable, str(bridge)],
        input=_json.dumps(payload),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = _json.loads(result.stdout)
    assert "error" not in out, out
    assert (repo / "pkg" / "math.py").read_text().count("def triple") == 1
