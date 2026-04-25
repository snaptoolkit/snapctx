"""Shared pytest fixtures for the snapctx test suite."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURE_SRC = Path(__file__).parent / "fixtures"


@pytest.fixture
def indexed_root(tmp_path: Path) -> Path:
    """Copy the sample fixture into a tmp dir and run a full index over it.

    Returns the tmp root. Each test gets a fresh index, so they don't stomp
    on each other's state.
    """
    from snapctx.api import index_root

    root = tmp_path / "repo"
    shutil.copytree(
        FIXTURE_SRC,
        root,
        ignore=shutil.ignore_patterns(".snapctx", "__pycache__"),
    )
    index_root(root)
    return root
