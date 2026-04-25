"""Optional per-repo configuration via ``snapctx.toml``.

The config file is **opt-in**. Without one, the walker behaves exactly as
it always has — same skip list, same vendor-bundle filter, same 250 KB
size cap, same gitignore handling. Drop a ``snapctx.toml`` at the repo
root and you can override any of those defaults for the repo it lives in.

The schema is small on purpose. Add a knob only when there's a real
project the default fails on; resist adding "might be useful" toggles.

Schema (every key optional; missing keys keep their default):

```toml
[walker]
# Add these directory names to the always-skip list (joined with the
# defaults: .git, .venv, node_modules, vendor, dist, build, ...).
extra_skip_dirs = ["legacy", "third_party"]

# Add these filename suffixes to the vendor-bundle skip list (joined
# with .min.js, .bundle.js, *-bundle.js, *.standalone.js, .map, ...).
extra_skip_suffixes = [".generated.ts"]

# Globs to *force-include* even when .gitignore would skip them. Lets
# you index a single directory inside an otherwise-gitignored vendor tree.
extra_include = ["vendor/internal-fork/**"]

# Globs to *force-exclude* regardless of gitignore. Useful for noisy
# generated code that's checked in.
extra_exclude = ["docs/generated/**", "**/*.snapshot.tsx"]

# Toggles (defaults match current behavior).
skip_vendor_bundles = true       # filter .min.js / *-bundle.js / .map / ...
respect_gitignore   = true       # honor the repo's .gitignore
max_file_size       = 256000     # bytes; default 250 KiB

# Restrict to specific languages. Default: every parser is active.
# Valid values: "python", "typescript".
languages = ["python", "typescript"]
```
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # noqa: F401
else:  # pragma: no cover — pyproject pins >=3.11.
    raise RuntimeError("snapctx requires Python 3.11+ (uses tomllib)")


CONFIG_FILENAME = "snapctx.toml"

DEFAULT_MAX_FILE_SIZE = 250 * 1024


@dataclass(frozen=True)
class WalkerConfig:
    """Walker-level overrides. Every field has a default that matches
    the pre-config behavior; an empty config produces zero behavioral
    change."""

    extra_skip_dirs: tuple[str, ...] = ()
    extra_skip_suffixes: tuple[str, ...] = ()
    extra_include: tuple[str, ...] = ()
    extra_exclude: tuple[str, ...] = ()
    skip_vendor_bundles: bool = True
    respect_gitignore: bool = True
    max_file_size: int = DEFAULT_MAX_FILE_SIZE
    # ``None`` = every registered parser is active. Otherwise restrict
    # to this set of language identifiers (e.g. {"python"}).
    languages: frozenset[str] | None = None


@dataclass(frozen=True)
class Config:
    walker: WalkerConfig = field(default_factory=WalkerConfig)

    @classmethod
    def default(cls) -> Config:
        return cls()


def load_config(root: Path) -> Config:
    """Load ``<root>/snapctx.toml`` if present, else return defaults.

    Unknown keys are tolerated (warnings would be noise) so that a config
    written for a future version doesn't break older snapctx installs.
    Type errors on known keys raise — silently ignoring them masks bugs.
    """
    path = root / CONFIG_FILENAME
    if not path.exists():
        return Config.default()

    import tomllib

    with path.open("rb") as f:
        data: dict[str, Any] = tomllib.load(f)

    walker_data = data.get("walker", {})
    if not isinstance(walker_data, dict):
        raise ValueError(
            f"{path}: [walker] must be a table, got {type(walker_data).__name__}"
        )

    walker = WalkerConfig(
        extra_skip_dirs=_str_tuple(walker_data, "extra_skip_dirs", path),
        extra_skip_suffixes=_str_tuple(walker_data, "extra_skip_suffixes", path),
        extra_include=_str_tuple(walker_data, "extra_include", path),
        extra_exclude=_str_tuple(walker_data, "extra_exclude", path),
        skip_vendor_bundles=_bool(walker_data, "skip_vendor_bundles", path, True),
        respect_gitignore=_bool(walker_data, "respect_gitignore", path, True),
        max_file_size=_int(walker_data, "max_file_size", path, DEFAULT_MAX_FILE_SIZE),
        languages=_str_set_optional(walker_data, "languages", path),
    )
    return Config(walker=walker)


# ---------- typed-getter helpers ----------


def _str_tuple(d: dict, key: str, path: Path) -> tuple[str, ...]:
    if key not in d:
        return ()
    val = d[key]
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise ValueError(f"{path}: [walker].{key} must be a list of strings")
    return tuple(val)


def _bool(d: dict, key: str, path: Path, default: bool) -> bool:
    if key not in d:
        return default
    val = d[key]
    if not isinstance(val, bool):
        raise ValueError(f"{path}: [walker].{key} must be a boolean")
    return val


def _int(d: dict, key: str, path: Path, default: int) -> int:
    if key not in d:
        return default
    val = d[key]
    if not isinstance(val, int) or isinstance(val, bool):
        raise ValueError(f"{path}: [walker].{key} must be an integer")
    if val <= 0:
        raise ValueError(f"{path}: [walker].{key} must be > 0")
    return val


def _str_set_optional(d: dict, key: str, path: Path) -> frozenset[str] | None:
    if key not in d:
        return None
    val = d[key]
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise ValueError(f"{path}: [walker].{key} must be a list of strings")
    if not val:
        raise ValueError(
            f"{path}: [walker].{key} cannot be empty — omit the key to enable all parsers"
        )
    return frozenset(val)
