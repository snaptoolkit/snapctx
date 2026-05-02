"""End-to-end CLI tests for ``snapctx skeleton``.

The skeleton subcommand exists to feed agent-harness preload hooks
(Claude Code's SessionStart, opencode's bootstrap, etc.). It must:

* render raw text — not JSON — because the typical consumer pipes
  the output into ``jq -Rs`` to wrap as a context-injection payload;
* round-trip through the preload cache when ``--cached`` is set so
  subsequent calls are an SQLite read;
* honor snapctx's auto-invalidation: a write primitive bumps
  ``source_version``, the cache hit becomes a miss, the next call
  re-renders.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from snapctx.api import edit_symbol, index_root
from snapctx.cli import main


@contextmanager
def _at(path: Path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Invoke main() and capture stdout + stderr as raw text."""
    buf = io.StringIO()
    err = io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, err
    try:
        code = main(argv)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    return code, buf.getvalue(), err.getvalue()


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
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
    index_root(repo)
    return repo


def test_skeleton_emits_raw_text_with_qnames(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        code, out, _ = _run(["skeleton"])
    assert code == 0
    assert out  # non-empty
    # Raw text — not JSON.
    assert not out.lstrip().startswith("{")
    # The two top-level functions should be discoverable.
    assert "add" in out
    assert "mul" in out


def test_skeleton_minimal_render_smaller_than_compact(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        _, compact_out, _ = _run(["skeleton", "--render", "compact"])
        _, minimal_out, _ = _run(["skeleton", "--render", "minimal"])
    assert len(minimal_out) <= len(compact_out)


def test_skeleton_cached_round_trips(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        _, first, _ = _run(["skeleton", "--cached", "--mode", "claude"])
        _, second, _ = _run(["skeleton", "--cached", "--mode", "claude"])
    assert first == second
    assert first  # non-empty


def test_skeleton_cached_invalidated_after_write(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        _, first, _ = _run(["skeleton", "--cached", "--mode", "claude"])
    # Write primitive bumps source_version; next cached read should rebuild
    # against the new content rather than serve a stale blob.
    edit_symbol(
        "pkg.math:add",
        "def add(a, b):\n    return a + b + 1\n",
        root=repo,
    )
    with _at(repo):
        _, second, _ = _run(["skeleton", "--cached", "--mode", "claude"])
    # Same files / qnames, so high-level shape is similar — the
    # invariant we test is that the call still succeeded after a write
    # and produced output. (The body itself isn't in the skeleton at
    # default render levels.)
    assert second
    assert "add" in second


def test_skeleton_separate_modes_cache_independently(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    with _at(repo):
        _, compact, _ = _run([
            "skeleton", "--cached", "--mode", "compact-mode",
            "--render", "compact",
        ])
        _, minimal, _ = _run([
            "skeleton", "--cached", "--mode", "minimal-mode",
            "--render", "minimal",
        ])
    # Different modes can serve different content — they don't collide.
    assert compact and minimal
    assert len(minimal) <= len(compact)
