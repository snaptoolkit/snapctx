"""End-to-end test for the optimistic resolve + post-ingest demotion pass."""

from __future__ import annotations

import shutil
from pathlib import Path

from snapctx.api import index_root
from snapctx.index import Index, db_path_for


def test_mixin_call_is_resolved_end_to_end(tmp_path: Path) -> None:
    """self.<method> where <method> lives on an imported mixin survives demotion."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mixins.py").write_text(
        "class UtilMixin:\n"
        "    def helper(self): return 1\n"
    )
    (root / "app.py").write_text(
        "from mixins import UtilMixin\n"
        "class App(UtilMixin):\n"
        "    def run(self): self.helper()\n"
    )

    summary = index_root(root)
    assert summary["files_updated"] == 2

    idx = Index(db_path_for(root))
    try:
        rows = idx.callees_of("app:App.run")
        assert len(rows) == 1
        assert rows[0]["callee_qname"] == "mixins:UtilMixin.helper"
    finally:
        idx.close()


def test_fake_attribute_chain_is_demoted(tmp_path: Path) -> None:
    """Django-ORM-style `Model.objects.filter()` must not leave a dangling qname."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "models.py").write_text(
        "class Widget: pass\n"
    )
    (root / "views.py").write_text(
        "from models import Widget\n"
        "def index():\n"
        "    return Widget.objects.filter(id=1)\n"
    )

    summary = index_root(root)
    assert summary["calls_demoted"] >= 1

    idx = Index(db_path_for(root))
    try:
        rows = idx.callees_of("views:index")
        # Widget.objects.filter is not a real symbol — should be demoted to NULL.
        assert all(r["callee_qname"] is None for r in rows)
    finally:
        idx.close()
