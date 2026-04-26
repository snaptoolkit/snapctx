"""Minimal warm-path client for the snapctx daemon.

Sends one JSON request to a Unix socket and prints the JSON reply —
mimicking the ``snapctx`` CLI's stdout shape so an agent can swap
binaries without changing prompts.

Usage:
    python -m snapctx._warm <socket-path> <subcommand> <args...>

Supported subcommands match a subset of the main CLI:
    context "<query>"          [--k-seeds N] [--source-for-top N]
    search  "<query>"          [-k N] [--mode M] [--kind K]
    expand  <qname>            [--direction callees|callers|both] [--depth N]
    outline <path>
    source  <qname>            [--with-neighbors]
"""

from __future__ import annotations

import argparse
import json
import socket
import sys


def _send(sock_path: str, op: str, kwargs: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall((json.dumps({"op": op, "kwargs": kwargs}) + "\n").encode("utf-8"))
    s.shutdown(socket.SHUT_WR)
    chunks: list[bytes] = []
    while True:
        buf = s.recv(65536)
        if not buf:
            break
        chunks.append(buf)
    s.close()
    return json.loads(b"".join(chunks).decode("utf-8"))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="snapctx-warm")
    p.add_argument("socket", help="Unix socket path of the snapctx-serve daemon")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("context")
    pc.add_argument("query")
    pc.add_argument("--k-seeds", type=int, default=5)
    pc.add_argument("--source-for-top", type=int, default=5)
    pc.add_argument("--file-outline-limit", type=int, default=8)
    pc.add_argument("--mode", default="hybrid")
    pc.add_argument("--kind", default=None)

    ps = sub.add_parser("search")
    ps.add_argument("query")
    ps.add_argument("-k", type=int, default=5)
    ps.add_argument("--mode", default="hybrid")
    ps.add_argument("--kind", default=None)
    ps.add_argument("--with-bodies", dest="with_bodies", action="store_true")
    ps.add_argument("--also", action="append", default=[], metavar="TERM")

    pe = sub.add_parser("expand")
    pe.add_argument("qname")
    pe.add_argument("--direction", default="callees")
    pe.add_argument("--depth", type=int, default=1)

    po = sub.add_parser("outline")
    po.add_argument("path")

    pso = sub.add_parser("source")
    pso.add_argument("qname")
    pso.add_argument("--with-neighbors", action="store_true")

    return p


_OP_FOR = {
    "context": ("context", lambda a: {
        "query": a.query,
        "k_seeds": a.k_seeds,
        "source_for_top": a.source_for_top,
        "file_outline_limit": a.file_outline_limit,
        "mode": a.mode,
        "kind": a.kind,
    }),
    "search": ("search", lambda a: {
        "query": a.query, "k": a.k, "mode": a.mode, "kind": a.kind,
        "with_bodies": a.with_bodies, "also": a.also or None,
    }),
    "expand": ("expand", lambda a: {
        "qname": a.qname, "direction": a.direction, "depth": a.depth,
    }),
    "outline": ("outline", lambda a: {"path": a.path}),
    "source": ("source", lambda a: {
        "qname": a.qname, "with_neighbors": a.with_neighbors,
    }),
}


def main() -> int:
    args = _build_parser().parse_args()
    op_name, kwargs_fn = _OP_FOR[args.cmd]
    result = _send(args.socket, op_name, kwargs_fn(args))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
