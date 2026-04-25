"""Tests for the file-watcher loop — hits the real watchdog observer."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from snapctx.watch import _IndexHandler
from snapctx.api import index_root
from snapctx.index import Index, db_path_for


def test_debounce_coalesces_rapid_events(tmp_path: Path) -> None:
    """Many rapid events should produce one re-index, not one per event."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def f(): pass\n")
    index_root(root)

    fire_count = 0
    lock = threading.Lock()

    def on_fire(summary):
        nonlocal fire_count
        with lock:
            fire_count += 1

    handler = _IndexHandler(root, on_fire=on_fire, debounce_seconds=0.2)

    # Fire 10 fake events in rapid succession.
    class E:
        def __init__(self, p):
            self.src_path = str(p)
            self.is_directory = False

    for _ in range(10):
        handler.on_any_event(E(root / "a.py"))
        time.sleep(0.02)

    # Wait longer than debounce for the re-index to fire.
    time.sleep(0.6)
    # Should have run exactly once.
    assert fire_count == 1


def test_only_supported_extensions_trigger(tmp_path: Path) -> None:
    """A .txt save must not schedule a re-index."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "readme.txt").write_text("hello")
    index_root(root)

    fired = []
    handler = _IndexHandler(root, on_fire=lambda s: fired.append(s), debounce_seconds=0.1)

    class E:
        def __init__(self, p):
            self.src_path = str(p)
            self.is_directory = False

    handler.on_any_event(E(root / "readme.txt"))
    time.sleep(0.3)
    assert fired == []


def test_reindex_picks_up_edit(tmp_path: Path) -> None:
    """End-to-end: an edit, routed through the handler, produces updated symbols."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def alpha(): pass\n")
    index_root(root)

    (root / "m.py").write_text("def alpha(): pass\ndef beta(): pass\n")

    fired = threading.Event()
    handler = _IndexHandler(
        root, on_fire=lambda s: fired.set(), debounce_seconds=0.1
    )

    class E:
        def __init__(self, p):
            self.src_path = str(p)
            self.is_directory = False

    handler.on_any_event(E(root / "m.py"))
    assert fired.wait(timeout=2.0), "watcher never fired"

    idx = Index(db_path_for(root))
    try:
        qnames = {
            row["qname"]
            for row in idx.conn.execute("SELECT qname FROM symbols").fetchall()
        }
    finally:
        idx.close()
    assert "m:alpha" in qnames and "m:beta" in qnames
