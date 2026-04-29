"""SQLite index: schema, incremental upsert, and FTS5 over symbols.

Layout under a repo root:
    .snapctx/
      index.db       — symbols, calls, imports, files, symbols_fts

All queries go through `Index`; callers never touch the raw connection.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from snapctx.qname import identifier_parts
from snapctx.schema import Call, Import, ParseResult, Symbol

# Bump whenever a parser change affects what symbols/calls/imports are
# emitted for an UNCHANGED source file. SHA-keyed incremental reindex
# alone won't pick up such changes — files whose bytes match the
# stored SHA would skip the parser entirely. ``index_root`` reads
# ``PRAGMA user_version`` and, on mismatch, wipes the index so the
# next pass reparses everything against the current parser.
#
# Version log:
#   1 — initial format (implicit before this stamp existed).
#   2 — module symbols emitted unconditionally (issue #21 / #22).
INDEX_PARSER_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    sha           TEXT NOT NULL,
    language      TEXT NOT NULL,
    indexed_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    qname         TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    language      TEXT NOT NULL,
    signature     TEXT NOT NULL,
    docstring     TEXT,
    file          TEXT NOT NULL,
    line_start    INTEGER NOT NULL,
    line_end      INTEGER NOT NULL,
    parent_qname  TEXT,
    decorators    TEXT,
    bases         TEXT,
    source_sha    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_file    ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_symbols_kind    ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_parent  ON symbols(parent_qname);

CREATE TABLE IF NOT EXISTS calls (
    caller_qname  TEXT NOT NULL,
    callee_name   TEXT NOT NULL,
    callee_qname  TEXT,
    file          TEXT NOT NULL,
    line          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_caller       ON calls(caller_qname);
CREATE INDEX IF NOT EXISTS idx_calls_callee       ON calls(callee_qname);
CREATE INDEX IF NOT EXISTS idx_calls_callee_name  ON calls(callee_name);
CREATE INDEX IF NOT EXISTS idx_calls_file         ON calls(file);

CREATE TABLE IF NOT EXISTS imports (
    file          TEXT NOT NULL,
    module        TEXT NOT NULL,
    name          TEXT,
    alias         TEXT,
    line          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imports_file   ON imports(file);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    qname UNINDEXED,
    qname_tokens,
    signature,
    docstring,
    decorators,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS symbol_vectors (
    qname   TEXT PRIMARY KEY,
    vector  BLOB NOT NULL,
    FOREIGN KEY (qname) REFERENCES symbols(qname) ON DELETE CASCADE
);
"""


def sha_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def db_path_for(root: Path, scope: str | None = None) -> Path:
    """Resolve the SQLite index path for a root.

    ``scope=None`` (default) returns the repo's own index. A non-None
    scope (a package directory name like ``"django"``) returns that
    package's isolated index. Per-package isolation keeps vector
    neighborhoods focused — a search for ``QuerySet`` inside the
    django scope can't be polluted by the user's own filter classes,
    and vice versa.
    """
    if scope is None:
        return (root / ".snapctx" / "index.db").resolve()
    return (root / ".snapctx" / "vendor" / scope / "index.db").resolve()


class Index:
    """Thin wrapper around the SQLite connection with convenience methods."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # Manual transaction control. Python's default ``isolation_level=""``
        # auto-begins a transaction on the first DML, which then collides
        # with the explicit ``BEGIN`` issued by ``tx()`` ("cannot start a
        # transaction within a transaction"). With ``None`` the only txn
        # boundaries are the explicit BEGIN/COMMIT in ``tx()``.
        self.conn.isolation_level = None
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Make concurrent writers wait for the active write txn to drain
        # instead of failing immediately with "database is locked". SQLite's
        # WAL journal already lets readers run during a write, but writers
        # still serialize against each other; without a busy timeout the
        # second writer raises ``OperationalError`` on first contact. Agent
        # tooling commonly fires multiple write ops in parallel (e.g. several
        # ``add_import`` calls), so we set a generous timeout — actual reindex
        # work is sub-second on normal repos. See issue #10.
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.executescript(SCHEMA)
        # Compare the index's parser version against the running code.
        # ``user_version=0`` is the SQLite default — treated as "older
        # than 1" so freshly created indexes match the current code
        # but pre-stamp indexes auto-trigger a rebuild on the next
        # ``index_root`` pass.
        stored_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        self.parser_version_outdated = stored_version != INDEX_PARSER_VERSION

    def stamp_parser_version(self) -> None:
        """Mark the index as written by the current parser version.

        Called after ``index_root`` has wiped or rebuilt to the
        current parser's output. Subsequent opens will see a matching
        version and skip the rebuild trigger.
        """
        self.conn.execute(f"PRAGMA user_version = {INDEX_PARSER_VERSION}")
        self.parser_version_outdated = False

    def close(self) -> None:
        self.conn.close()

    # ---------- file-level helpers ----------

    def current_sha(self, file: str) -> str | None:
        row = self.conn.execute("SELECT sha FROM files WHERE path = ?", (file,)).fetchone()
        return row["sha"] if row else None

    def forget_file(self, file: str) -> None:
        """Remove all rows for a file (symbols, calls, imports, FTS, vectors, files)."""
        with self.tx():
            cur = self.conn.cursor()
            cur.execute(
                "DELETE FROM symbols_fts WHERE qname IN (SELECT qname FROM symbols WHERE file = ?)",
                (file,),
            )
            cur.execute(
                "DELETE FROM symbol_vectors WHERE qname IN (SELECT qname FROM symbols WHERE file = ?)",
                (file,),
            )
            cur.execute("DELETE FROM symbols WHERE file = ?", (file,))
            cur.execute("DELETE FROM calls   WHERE file = ?", (file,))
            cur.execute("DELETE FROM imports WHERE file = ?", (file,))
            cur.execute("DELETE FROM files   WHERE path = ?", (file,))

    def wipe_all(self) -> None:
        """Drop every data row, keeping the schema. Used when the project
        root has been renamed/moved and the stored absolute paths no longer
        resolve — full rebuild is cheaper than path-rewriting."""
        with self.tx():
            for tbl in (
                "symbols_fts", "symbol_vectors",
                "symbols", "calls", "imports", "files",
            ):
                self.conn.execute(f"DELETE FROM {tbl}")

    # ---------- ingest ----------

    def ingest(self, file: str, language: str, file_sha: str, result: ParseResult) -> None:
        """Replace all data for a file atomically."""
        with self.tx():
            self.forget_file(file)
            self.conn.execute(
                "INSERT INTO files(path, sha, language, indexed_at) VALUES (?, ?, ?, ?)",
                (file, file_sha, language, time.time()),
            )
            self._insert_symbols(result.symbols)
            self._insert_calls(result.calls)
            self._insert_imports(result.imports)

    def _insert_symbols(self, symbols: list[Symbol]) -> None:
        if not symbols:
            return
        # Dedupe by qname within this file. Sources like conditional
        # redefinitions (``if x: def foo(): ... else: def foo(): ...``) or
        # bundled/minified JS that reuse short names in different scopes can
        # produce duplicate qnames for one file; keep the first occurrence.
        seen: set[str] = set()
        unique: list[Symbol] = []
        for s in symbols:
            if s.qname in seen:
                continue
            seen.add(s.qname)
            unique.append(s)
        symbols = unique
        rows = [
            (
                s.qname,
                s.kind,
                s.language,
                s.signature,
                s.docstring,
                s.file,
                s.line_start,
                s.line_end,
                s.parent_qname,
                "\n".join(s.decorators),
                "\n".join(s.bases),
                s.source_sha,
            )
            for s in symbols
        ]
        self.conn.executemany(
            "INSERT INTO symbols(qname, kind, language, signature, docstring, file, "
            "line_start, line_end, parent_qname, decorators, bases, source_sha) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        fts_rows = [
            (
                s.qname,
                identifier_parts(s.qname),
                s.signature,
                s.docstring or "",
                "\n".join(s.decorators),
            )
            for s in symbols
        ]
        self.conn.executemany(
            "INSERT INTO symbols_fts(qname, qname_tokens, signature, docstring, decorators) "
            "VALUES (?, ?, ?, ?, ?)",
            fts_rows,
        )

    def _insert_calls(self, calls: list[Call]) -> None:
        if not calls:
            return
        self.conn.executemany(
            "INSERT INTO calls(caller_qname, callee_name, callee_qname, file, line) "
            "VALUES (?, ?, ?, ?, ?)",
            [(c.caller_qname, c.callee_name, c.callee_qname, c.file, c.line) for c in calls],
        )

    def _insert_imports(self, imports: list[Import]) -> None:
        if not imports:
            return
        self.conn.executemany(
            "INSERT INTO imports(file, module, name, alias, line) VALUES (?, ?, ?, ?, ?)",
            [(i.file, i.module, i.name, i.alias, i.line) for i in imports],
        )

    # ---------- read queries ----------

    def fts_search(
        self, query: str, limit: int, kind: str | None = None
    ) -> list[sqlite3.Row]:
        """Return symbol rows ranked by FTS5 BM25, optionally filtered by kind."""
        where_kind = " AND s.kind = ?" if kind else ""
        sql = (
            "SELECT s.*, bm25(symbols_fts) AS score "
            "FROM symbols_fts "
            "JOIN symbols s ON s.qname = symbols_fts.qname "
            "WHERE symbols_fts MATCH ?" + where_kind + " "
            "ORDER BY score ASC "
            "LIMIT ?"
        )
        params: tuple = (query, kind, limit) if kind else (query, limit)
        return self.conn.execute(sql, params).fetchall()

    def get_symbol(self, qname: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM symbols WHERE qname = ?", (qname,)).fetchone()

    def symbols_in_file(self, file: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM symbols WHERE file = ? ORDER BY line_start ASC", (file,)
        ).fetchall()

    def callees_of(self, caller_qname: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM calls WHERE caller_qname = ? ORDER BY line ASC", (caller_qname,)
        ).fetchall()

    def callers_of(self, callee_qname: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM calls WHERE callee_qname = ? ORDER BY caller_qname, line ASC",
            (callee_qname,),
        ).fetchall()

    def imports_for_file(self, file: str) -> list[sqlite3.Row]:
        """Return all imports declared in a file, ordered by line.

        Used by cross-package call resolution: when a call's callee is
        unresolved, we look at what the file imports to figure out which
        sibling vendor index might know the target.
        """
        return self.conn.execute(
            "SELECT module, name, alias, line FROM imports "
            "WHERE file = ? ORDER BY line ASC",
            (file,),
        ).fetchall()

    # ---------- vector store ----------

    def symbols_without_vectors(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT qname, signature, docstring FROM symbols "
            "WHERE qname NOT IN (SELECT qname FROM symbol_vectors)"
        ).fetchall()

    def upsert_vectors(self, qnames: list[str], vectors) -> None:
        import numpy as np  # local to keep vector path optional at import time

        rows = [
            (q, np.asarray(v, dtype=np.float32).tobytes()) for q, v in zip(qnames, vectors)
        ]
        with self.tx():
            self.conn.executemany(
                "INSERT OR REPLACE INTO symbol_vectors(qname, vector) VALUES (?, ?)", rows
            )

    def vector_search(
        self, query_vec, limit: int, kind: str | None = None
    ) -> list[tuple[sqlite3.Row, float]]:
        """Cosine-similarity search over the symbol_vectors table.

        Vectors are L2-normalized at ingest, so dot product = cosine similarity.
        Returns up to ``limit`` (row, score) tuples, highest similarity first.

        Matrix assembly: concat all BLOBs and interpret as one contiguous array.
        Avoids ``np.vstack`` of thousands of small arrays, which is a large
        Python-level overhead for an otherwise native-speed operation.
        """
        import numpy as np

        rows = self.conn.execute("SELECT qname, vector FROM symbol_vectors").fetchall()
        if not rows:
            return []
        qnames = [r["qname"] for r in rows]
        blob = b"".join(r["vector"] for r in rows)
        matrix = np.frombuffer(blob, dtype=np.float32).reshape(len(rows), -1)
        scores = matrix @ query_vec.astype(np.float32)
        order = np.argsort(-scores)

        if kind is not None:
            allowed = {
                r["qname"]
                for r in self.conn.execute(
                    "SELECT qname FROM symbols WHERE kind = ?", (kind,)
                ).fetchall()
            }
            order = [i for i in order if qnames[i] in allowed]

        top = list(order[:limit])
        top_qnames = [qnames[i] for i in top]
        top_scores = {qnames[i]: float(scores[i]) for i in top}
        if not top_qnames:
            return []
        placeholders = ",".join("?" * len(top_qnames))
        symbol_rows = self.conn.execute(
            f"SELECT * FROM symbols WHERE qname IN ({placeholders})", top_qnames
        ).fetchall()
        by_qname = {r["qname"]: r for r in symbol_rows}
        return [(by_qname[q], top_scores[q]) for q in top_qnames if q in by_qname]

    def promote_self_calls(self) -> int:
        """Resolve forward-referenced `self.X()` / `this.X()` calls against the complete symbol table.

        Both Python and TS parsers resolve instance-method calls optimistically
        at parse time, but they can only match against symbols already emitted.
        When a method (``emit``) is defined before another method it calls
        (``_publish_delta`` at a later line of the same class), the early
        caller's resolution fails. After all files are ingested we can look
        the targets up for real.

        For each call where ``callee_qname IS NULL`` and ``callee_name``
        starts with ``self.`` (Python) or ``this.`` (TS/JS), derive the
        enclosing class from the caller's qname (strip the last component)
        and check whether ``<class>.<method>`` now exists. If yes, update.

        Returns the number of rows updated.
        """
        pending = self.conn.execute(
            "SELECT rowid, caller_qname, callee_name FROM calls "
            "WHERE callee_qname IS NULL "
            "  AND (callee_name LIKE 'self.%' OR callee_name LIKE 'this.%') "
            "  AND callee_name NOT LIKE '%.%.%'"
        ).fetchall()

        updates: list[tuple[str, int]] = []
        for row in pending:
            caller = row["caller_qname"]
            callee = row["callee_name"]
            # Strip trailing ".method" from caller_qname to get the class qname.
            if "." not in caller:
                continue
            class_qname, _, _ = caller.rpartition(".")
            # Strip the self./this. prefix (5 chars).
            method = callee[5:]
            guess = f"{class_qname}.{method}"
            updates.append((guess, row["rowid"]))

        if not updates:
            return 0
        # Validate each guess against the symbols table, update if hit.
        n = 0
        with self.tx():
            for guess, rowid in updates:
                exists = self.conn.execute(
                    "SELECT 1 FROM symbols WHERE qname = ?", (guess,)
                ).fetchone()
                if exists is None:
                    continue
                self.conn.execute(
                    "UPDATE calls SET callee_qname = ? WHERE rowid = ?",
                    (guess, rowid),
                )
                n += 1
        return n

    def demote_unresolved_calls(self) -> int:
        """Null out callee_qname for any call pointing at a qname that isn't a real symbol.

        Returns the number of rows demoted. This is the second half of the
        optimistic-resolution strategy: the parser may emit a best-guess qname
        (e.g. Mixin.method via MRO; Django-ORM chains like Model:objects.filter),
        and this sweep removes the ones that didn't pan out.
        """
        with self.tx():
            cur = self.conn.execute(
                "UPDATE calls SET callee_qname = NULL "
                "WHERE callee_qname IS NOT NULL "
                "  AND callee_qname NOT IN (SELECT qname FROM symbols)"
            )
            return cur.rowcount

    # ---------- transaction helper ----------

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Open a transaction, or join the caller's if one is already open.

        Reentrancy lets composite ops (e.g. ``ingest`` calls ``forget_file``)
        each wrap themselves in ``tx()`` for safety when called standalone,
        without nesting ``BEGIN``/``COMMIT`` when called from another tx.
        """
        if self.conn.in_transaction:
            yield
            return
        try:
            # IMMEDIATE acquires the RESERVED write lock at txn start rather
            # than at first write. Two reasons:
            # (1) ``busy_timeout`` reliably waits at ``BEGIN IMMEDIATE`` —
            # with plain ``BEGIN`` (DEFERRED), an upgrade collision can
            # raise ``OperationalError: database is locked`` immediately
            # under WAL with multiple writer connections (issue #10).
            # (2) prevents two concurrent writers from both acquiring SHARED,
            # then deadlocking when each tries to upgrade.
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
