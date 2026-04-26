"""Minimal snapctx daemon for warm-path CLI calls.

Holds the fastembed model + open Index handles in one long-running
process. Clients send a one-line JSON request over a Unix socket; the
daemon dispatches to the matching ``snapctx.api`` op and returns the
JSON result on the same socket.

Usage:
    python -m snapctx._serve <root> <socket-path>

The companion ``_warm`` client opens the socket, sends a request, and
prints the reply — mimicking the ``snapctx`` CLI's stdout shape so an
agent can swap binaries without changing its prompts.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

from snapctx.api import (
    context,
    expand,
    get_source,
    index_root,
    outline,
    search_code,
)

_OPS = {
    "context":  context,
    "search":   search_code,
    "expand":   expand,
    "outline":  outline,
    "source":   get_source,
}


def _handle(conn: socket.socket, root: Path) -> None:
    try:
        chunks: list[bytes] = []
        while True:
            buf = conn.recv(65536)
            if not buf:
                break
            chunks.append(buf)
            if buf.endswith(b"\n"):
                break
        if not chunks:
            return
        req = json.loads(b"".join(chunks).decode("utf-8"))
        op_name = req["op"]
        kwargs = req.get("kwargs", {})
        kwargs.setdefault("root", str(root))
        op = _OPS.get(op_name)
        if op is None:
            result = {"error": f"unknown op: {op_name}"}
        else:
            result = op(**kwargs)
        payload = (json.dumps(result) + "\n").encode("utf-8")
        conn.sendall(payload)
    except Exception as e:
        conn.sendall(
            (json.dumps({"error": f"{type(e).__name__}: {e}"}) + "\n").encode("utf-8")
        )
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: snapctx._serve <root> <socket-path>\n")
        return 2
    root = Path(sys.argv[1]).resolve()
    sock_path = sys.argv[2]

    # Make sure the index exists and the embedder is warm before we accept
    # the first request — that's the whole point.
    sys.stderr.write(f"snapctx-serve: indexing {root}...\n")
    index_root(root)
    sys.stderr.write("snapctx-serve: warming embedder...\n")
    from snapctx.embeddings import embed_texts
    embed_texts(["warmup"])
    sys.stderr.write(f"snapctx-serve: ready on {sock_path}\n")

    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(sock_path)
    s.listen(8)

    try:
        while True:
            conn, _ = s.accept()
            t = threading.Thread(target=_handle, args=(conn, root), daemon=True)
            t.start()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.unlink(sock_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
