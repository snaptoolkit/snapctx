"""File-watcher loop: re-index on save.

Design:
- Uses ``watchdog`` to observe ``root`` recursively (uses macOS FSEvents /
  Linux inotify / Windows ReadDirectoryChangesW under the hood).
- Events are debounced: rapid bursts (e.g. a git checkout that touches 200
  files) coalesce into a single re-index after the dust settles.
- Only events inside the supported extension set and outside the
  ``ALWAYS_SKIP`` directory list trigger a re-index.
- ``index_root`` is already incremental, so a trigger re-parses only the
  files whose SHA changed — typically <500 ms.

The watcher is meant to run in its own terminal tab so the index stays
fresh while you work. SQLite WAL mode handles the concurrent reader/writer.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from snapctx.api import index_root
from snapctx.parsers.registry import supported_extensions
from snapctx.walker import ALWAYS_SKIP


class _IndexHandler(FileSystemEventHandler):
    """Debounced handler. Calls ``on_fire(summary)`` after each re-index run."""

    def __init__(
        self,
        root: Path,
        on_fire: Callable[[dict], None],
        debounce_seconds: float = 0.5,
    ) -> None:
        super().__init__()
        self.root = root
        self._on_fire = on_fire
        self._debounce = debounce_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._exts = set(supported_extensions())

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # Consider both the src path and, for moves, the dest.
        candidates: list[str] = [getattr(event, "src_path", "") or ""]
        dest = getattr(event, "dest_path", "") or ""
        if dest:
            candidates.append(dest)

        for raw in candidates:
            if not raw:
                continue
            p = Path(raw)
            if p.suffix not in self._exts:
                continue
            try:
                rel = p.relative_to(self.root)
            except ValueError:
                continue  # outside the watched root
            if any(part in ALWAYS_SKIP for part in rel.parts):
                continue
            self._schedule()
            return

    def _schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        try:
            t0 = time.monotonic()
            summary = index_root(self.root)
            summary["_duration_ms"] = round((time.monotonic() - t0) * 1000, 1)
            self._on_fire(summary)
        except Exception as e:
            print(f"[snapctx-watch] re-index failed: {type(e).__name__}: {e}", file=sys.stderr)


def run_watch(root: Path, *, debounce_seconds: float = 0.5) -> None:
    """Block forever, re-indexing ``root`` whenever a supported file changes.

    Prints a one-line summary on every re-index (to stderr, so it doesn't
    pollute stdout if piped). ``Ctrl+C`` exits cleanly.
    """
    root = root.resolve()

    def report(summary: dict) -> None:
        parts = []
        if summary.get("files_updated"):
            parts.append(f"{summary['files_updated']} updated")
        if summary.get("files_removed"):
            parts.append(f"{summary['files_removed']} removed")
        if summary.get("symbols_embedded"):
            parts.append(f"{summary['symbols_embedded']} embedded")
        detail = ", ".join(parts) or "no changes"
        print(
            f"[snapctx-watch] re-index in {summary['_duration_ms']} ms — {detail}",
            file=sys.stderr,
        )

    # Initial run — brings the index up to date before we start listening.
    t0 = time.monotonic()
    init = index_root(root)
    print(
        f"[snapctx-watch] initial index: {init['symbols_indexed']} symbols indexed, "
        f"{init['files_updated']} files updated, "
        f"{init['files_removed']} removed, "
        f"in {(time.monotonic() - t0) * 1000:.0f} ms",
        file=sys.stderr,
    )
    print(f"[snapctx-watch] watching {root} (Ctrl-C to stop)", file=sys.stderr)

    handler = _IndexHandler(root, on_fire=report, debounce_seconds=debounce_seconds)
    observer = Observer()
    observer.schedule(handler, str(root), recursive=True)
    observer.start()
    try:
        while observer.is_alive():
            observer.join(timeout=1.0)
    except KeyboardInterrupt:
        print("[snapctx-watch] stopping", file=sys.stderr)
    finally:
        observer.stop()
        observer.join()
