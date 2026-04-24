"""SQLite index: schema, incremental upsert, and FTS5 over symbols.

Layout under a repo root:
    .neargrep/
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

from neargrep.qname import identifier_parts
from neargrep.schema import Call, Import, ParseResult, Symbol

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


def db_path_for(root: Path) -> Path:
    """Resolve the standard SQLite path for a repo root."""
    return (root / ".neargrep" / "index.db").resolve()


class Index:
    """Thin wrapper around the SQLite connection with convenience methods."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---------- file-level helpers ----------

    def current_sha(self, file: str) -> str | None:
        row = self.conn.execute("SELECT sha FROM files WHERE path = ?", (file,)).fetchone()
        return row["sha"] if row else None

    def forget_file(self, file: str) -> None:
        """Remove all rows for a file (symbols, calls, imports, FTS, vectors, files)."""
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
        self.conn.executemany(
            "INSERT OR REPLACE INTO symbol_vectors(qname, vector) VALUES (?, ?)", rows
        )
        self.conn.commit()

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

    def demote_unresolved_calls(self) -> int:
        """Null out callee_qname for any call pointing at a qname that isn't a real symbol.

        Returns the number of rows demoted. This is the second half of the
        optimistic-resolution strategy: the parser may emit a best-guess qname
        (e.g. Mixin.method via MRO; Django-ORM chains like Model:objects.filter),
        and this sweep removes the ones that didn't pan out.
        """
        cur = self.conn.execute(
            "UPDATE calls SET callee_qname = NULL "
            "WHERE callee_qname IS NOT NULL "
            "  AND callee_qname NOT IN (SELECT qname FROM symbols)"
        )
        self.conn.commit()
        return cur.rowcount

    # ---------- transaction helper ----------

    @contextmanager
    def tx(self) -> Iterator[None]:
        try:
            self.conn.execute("BEGIN")
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
