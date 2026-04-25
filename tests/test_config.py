"""Optional ``snapctx.toml`` per-repo configuration.

Each test creates a fresh tmp_path repo, optionally drops a config
file, and exercises the walker (or full ``index_root``) to verify the
override took effect. The no-config case is covered by every other
test in the suite — these tests focus on what *changes* when a config
is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snapctx.api import index_root
from snapctx.config import Config, WalkerConfig, load_config
from snapctx.walker import iter_source_files


def _write_config(root: Path, body: str) -> None:
    (root / "snapctx.toml").write_text(body)


def test_no_config_returns_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg == Config.default()
    assert cfg.walker.skip_vendor_bundles is True
    assert cfg.walker.respect_gitignore is True
    assert cfg.walker.languages is None


def test_loads_extra_skip_dirs(tmp_path: Path) -> None:
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "old.py").write_text("def x(): pass\n")
    (tmp_path / "active").mkdir()
    (tmp_path / "active" / "live.py").write_text("def y(): pass\n")

    _write_config(tmp_path, '[walker]\nextra_skip_dirs = ["legacy"]\n')
    cfg = load_config(tmp_path)
    files = {p.name for p in iter_source_files(tmp_path, cfg.walker)}
    assert files == {"live.py"}


def test_extra_skip_suffixes(tmp_path: Path) -> None:
    (tmp_path / "real.ts").write_text("export const x = 1;\n")
    (tmp_path / "tool.generated.ts").write_text("export const y = 2;\n")

    _write_config(tmp_path, '[walker]\nextra_skip_suffixes = [".generated.ts"]\n')
    cfg = load_config(tmp_path)
    files = {p.name for p in iter_source_files(tmp_path, cfg.walker)}
    assert files == {"real.ts"}


def test_skip_vendor_bundles_off_keeps_minified(tmp_path: Path) -> None:
    (tmp_path / "real.js").write_text("export const x = 1;\n")
    (tmp_path / "lib.min.js").write_text("/*minified*/\n")

    _write_config(tmp_path, '[walker]\nskip_vendor_bundles = false\n')
    cfg = load_config(tmp_path)
    files = {p.name for p in iter_source_files(tmp_path, cfg.walker)}
    assert files == {"real.js", "lib.min.js"}


def test_default_skip_vendor_bundles_drops_minified(tmp_path: Path) -> None:
    (tmp_path / "real.js").write_text("export const x = 1;\n")
    (tmp_path / "lib.min.js").write_text("/*minified*/\n")

    files = {p.name for p in iter_source_files(tmp_path)}
    assert files == {"real.js"}


def test_max_file_size_override(tmp_path: Path) -> None:
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * 10_000)  # ~60 KB
    small = tmp_path / "small.py"
    small.write_text("y = 1\n")

    # Drop the cap so big.py also gets through (default would have kept it
    # since 60 KB < 250 KB; lower cap to 1 KB to verify the override).
    _write_config(tmp_path, '[walker]\nmax_file_size = 1024\n')
    cfg = load_config(tmp_path)
    files = {p.name for p in iter_source_files(tmp_path, cfg.walker)}
    assert files == {"small.py"}


def test_languages_filter_drops_typescript(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def x(): pass\n")
    (tmp_path / "ui.ts").write_text("export const x = 1;\n")

    _write_config(tmp_path, '[walker]\nlanguages = ["python"]\n')
    cfg = load_config(tmp_path)
    files = {p.name for p in iter_source_files(tmp_path, cfg.walker)}
    assert files == {"main.py"}


def test_languages_filter_drops_python(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def x(): pass\n")
    (tmp_path / "ui.ts").write_text("export const x = 1;\n")

    _write_config(tmp_path, '[walker]\nlanguages = ["typescript"]\n')
    cfg = load_config(tmp_path)
    files = {p.name for p in iter_source_files(tmp_path, cfg.walker)}
    assert files == {"ui.ts"}


def test_extra_exclude_matches_globs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("def x(): pass\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "generated").mkdir()
    (tmp_path / "docs" / "generated" / "stub.py").write_text("def y(): pass\n")

    _write_config(tmp_path, '[walker]\nextra_exclude = ["docs/generated/**"]\n')
    cfg = load_config(tmp_path)
    files = {str(p.relative_to(tmp_path)) for p in iter_source_files(tmp_path, cfg.walker)}
    assert files == {"src/real.py"}


def test_extra_include_overrides_gitignore(tmp_path: Path) -> None:
    """``extra_include`` re-allows paths that .gitignore would skip."""
    (tmp_path / ".gitignore").write_text("vendor/\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "fork").mkdir()
    (tmp_path / "vendor" / "fork" / "patched.py").write_text("def x(): pass\n")
    (tmp_path / "vendor" / "junk").mkdir()
    (tmp_path / "vendor" / "junk" / "raw.py").write_text("def y(): pass\n")

    # Without the override, vendor/ is hidden by .gitignore AND by the
    # ALWAYS_SKIP "vendor" entry. We can override the gitignore but not
    # ALWAYS_SKIP without renaming the dir — verify the include works
    # against a plain .gitignore-only case.
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "internal.py").write_text("def z(): pass\n")
    (tmp_path / ".gitignore").write_text("private/\n")

    _write_config(tmp_path, '[walker]\nextra_include = ["private/**"]\n')
    cfg = load_config(tmp_path)
    files = {p.name for p in iter_source_files(tmp_path, cfg.walker)}
    assert "internal.py" in files


def test_respect_gitignore_off_indexes_ignored_files(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("private/\n")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "secret.py").write_text("API_KEY = 'fake'\n")
    (tmp_path / "public.py").write_text("x = 1\n")

    _write_config(tmp_path, '[walker]\nrespect_gitignore = false\n')
    cfg = load_config(tmp_path)
    files = {p.name for p in iter_source_files(tmp_path, cfg.walker)}
    assert files == {"secret.py", "public.py"}


def test_index_root_loads_config(tmp_path: Path) -> None:
    """End-to-end: ``snapctx.toml`` at the root affects ``index_root`` output."""
    (tmp_path / "main.py").write_text("def x(): pass\n")
    (tmp_path / "ui.ts").write_text("export const x = 1;\n")
    _write_config(tmp_path, '[walker]\nlanguages = ["python"]\n')

    summary = index_root(tmp_path)
    # Only main.py should have been ingested.
    assert summary["files_updated"] == 1


def test_invalid_config_raises_with_clear_path(tmp_path: Path) -> None:
    _write_config(tmp_path, '[walker]\nextra_skip_dirs = "not_a_list"\n')
    with pytest.raises(ValueError, match="extra_skip_dirs"):
        load_config(tmp_path)


def test_unknown_keys_are_tolerated(tmp_path: Path) -> None:
    """Forward-compatibility: an unknown key shouldn't break older clients."""
    _write_config(
        tmp_path,
        '[walker]\nextra_skip_dirs = ["legacy"]\nfuture_knob = 42\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.walker.extra_skip_dirs == ("legacy",)


def test_empty_languages_list_is_rejected(tmp_path: Path) -> None:
    """Empty list is ambiguous — force the user to omit the key instead."""
    _write_config(tmp_path, '[walker]\nlanguages = []\n')
    with pytest.raises(ValueError, match="cannot be empty"):
        load_config(tmp_path)


def test_max_file_size_must_be_positive(tmp_path: Path) -> None:
    _write_config(tmp_path, '[walker]\nmax_file_size = 0\n')
    with pytest.raises(ValueError, match="must be > 0"):
        load_config(tmp_path)


def test_walker_config_dataclass_is_immutable() -> None:
    """``WalkerConfig`` is frozen so accidental mutation can't poison shared state."""
    cfg = WalkerConfig()
    with pytest.raises(Exception):  # FrozenInstanceError
        cfg.extra_skip_dirs = ("foo",)  # type: ignore[misc]
