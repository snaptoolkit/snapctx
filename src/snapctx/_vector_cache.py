"""Bounded process-local vector snapshots, invalidated by SQLite commits.

A dedicated read-only observer is necessary: PRAGMA data_version values are
only comparable on the same connection and do not change for that connection's
own writes. API Index handles are short-lived; observers survive them. No source
text or query result is cached. Explicit caller transactions bypass the cache.
"""
from __future__ import annotations

import atexit
import os
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

_LOCK = threading.RLock()
_CACHE: OrderedDict[str, dict] = OrderedDict()
_MAX_DATABASES = 3
_MAX_BYTES = 128 * 1024 * 1024
_PID = os.getpid()


def _read(conn):
    import numpy as np
    rows = conn.execute('SELECT qname, vector FROM symbol_vectors').fetchall()
    names = tuple(r['qname'] for r in rows)
    blob = b''.join(r['vector'] for r in rows)
    matrix = np.frombuffer(blob, dtype=np.float32).reshape(len(rows), -1) if rows else None
    return names, matrix


def clear_vector_cache() -> None:
    with _LOCK:
        for entry in _CACHE.values():
            entry['observer'].close()
        _CACHE.clear()


atexit.register(clear_vector_cache)


def vector_snapshot(db_path: Path, conn: sqlite3.Connection, connection_identity):
    """Get an immutable matrix valid at this call's SQLite version check."""
    global _PID
    if conn.in_transaction:
        return _read(conn)
    path = db_path.resolve()
    key = str(path)
    with _LOCK:
        if os.getpid() != _PID:
            clear_vector_cache()
            _PID = os.getpid()
        try:
            stat = path.stat()
        except OSError:
            return _read(conn)
        identity = (stat.st_dev, stat.st_ino)
        if identity != connection_identity:
            return _read(conn)
        entry = _CACHE.get(key)
        if entry is not None and entry['identity'] != identity:
            _CACHE.pop(key)['observer'].close()
            entry = None
        if entry is None:
            observer = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, check_same_thread=False,
                                       isolation_level=None)
            entry = {'observer': observer, 'identity': identity, 'version': None,
                     'names': (), 'matrix': None, 'bytes': 0}
            _CACHE[key] = entry
        observer = entry['observer']
        version = observer.execute('PRAGMA data_version').fetchone()[0]
        if entry['version'] == version:
            _CACHE.move_to_end(key)
            return entry['names'], entry['matrix']
        names, matrix = _read(conn)
        after = observer.execute('PRAGMA data_version').fetchone()[0]
        # A commit during the read makes the snapshot unsuitable for reuse.
        # The current caller still receives the consistent SELECT snapshot.
        size = (matrix.nbytes if matrix is not None else 0) + sum(len(q) * 4 + 64 for q in names)
        if version == after and size <= _MAX_BYTES:
            entry.update(version=version, names=names, matrix=matrix, bytes=size)
            _CACHE.move_to_end(key)
        else:
            _CACHE.pop(key)['observer'].close()
        while len(_CACHE) > _MAX_DATABASES or sum(e['bytes'] for e in _CACHE.values()) > _MAX_BYTES:
            _CACHE.popitem(last=False)[1]['observer'].close()
        return names, matrix
