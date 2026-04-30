"""Tests for ``session_skeleton`` — multi-root compact text skeleton."""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root, session_skeleton


def _build_simple_root(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "math.py").write_text(
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
    (repo / "pkg" / "models.py").write_text(
        '"""Domain models."""\n'
        "\n"
        "\n"
        "class Product:\n"
        '    """A product."""\n'
        "\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n"
    )
    index_root(repo)
    return repo


def test_skeleton_compact_includes_files_summaries_signatures(
    tmp_path: Path,
) -> None:
    repo = _build_simple_root(tmp_path)
    out = session_skeleton([repo], render="compact")

    assert "pkg/math.py" in out
    assert "Math helpers." in out  # module summary surfaced
    assert "[function] pkg.math:add" in out
    assert "(a, b)" in out  # signature surfaced
    assert "[class] pkg.models:Product" in out


def test_skeleton_minimal_drops_signatures_and_summaries(tmp_path: Path) -> None:
    repo = _build_simple_root(tmp_path)
    out = session_skeleton([repo], render="minimal")

    assert "pkg.math:add" in out
    # Minimal omits the [kind] prefix and signature.
    assert "[function]" not in out
    assert "(a, b)" not in out
    assert "Math helpers." not in out


def test_skeleton_accepts_single_path_or_list(tmp_path: Path) -> None:
    repo = _build_simple_root(tmp_path)
    a = session_skeleton(repo)
    b = session_skeleton([repo])
    assert a == b


def test_skeleton_multi_root_tags_each_directory(tmp_path: Path) -> None:
    backend = _build_simple_root(tmp_path, name="backend")
    frontend = _build_simple_root(tmp_path, name="frontend")
    out = session_skeleton([backend, frontend])

    # With two roots, directory headers must be prefixed by the root
    # label so the agent can route correctly.
    assert "## backend/pkg/" in out
    assert "## frontend/pkg/" in out
    # Both roots' symbols are present.
    assert "pkg.math:add" in out
    assert out.count("pkg.math:add") == 2


def test_skeleton_truncates_at_max_chars(tmp_path: Path) -> None:
    repo = _build_simple_root(tmp_path)
    # Tiny budget: should clip and surface the truncated marker.
    out = session_skeleton([repo], max_chars=200)

    assert "(truncated" in out
    assert len(out) <= 400  # buffer for the trailing marker line


def test_skeleton_missing_index_surfaces_inline(tmp_path: Path) -> None:
    """A root without an index doesn't crash — we surface a comment."""
    repo = tmp_path / "no_index"
    repo.mkdir()
    out = session_skeleton([repo])
    assert "no snapctx index" in out


def test_skeleton_invalid_render_raises(tmp_path: Path) -> None:
    repo = _build_simple_root(tmp_path)
    try:
        session_skeleton([repo], render="bogus")  # type: ignore[arg-type]
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_skeleton_compact_includes_decorators(tmp_path: Path) -> None:
    """Decorators are often the most identifying fact about a symbol."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "events.py").write_text(
        '"""Events."""\n'
        "\n"
        "from dataclasses import dataclass\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Event:\n"
        "    name: str\n"
    )
    index_root(repo)
    out = session_skeleton([repo], render="compact")
    assert "@dataclass" in out


def test_skeleton_size_under_8k_for_simple_repo(tmp_path: Path) -> None:
    """A small repo should produce a compact (≤ 8 KB) skeleton — the whole
    point of this surface is fitting inside an Anthropic cached preamble."""
    repo = _build_simple_root(tmp_path)
    out = session_skeleton([repo], render="compact")
    assert len(out) < 8000
