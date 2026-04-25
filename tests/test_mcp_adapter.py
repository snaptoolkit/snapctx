"""Smoke tests for the MCP stdio adapter: init, tools/list, tools/call.

These spawn a real subprocess and speak JSON-RPC over stdin/stdout. They
exercise the boundary where Claude Code would sit. Keep them minimal — unit
coverage of the underlying ops lives in test_api / test_context.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from snapctx.api import index_root

FIXTURE_SRC = Path(__file__).parent / "fixtures"
# Resolve the entry-point alongside the currently-running python. Works whether
# the test is invoked via `pytest`, `uv run pytest`, or `uv tool`-installed.
MCP_BIN = Path(sys.executable).parent / "snapctx-mcp"


@pytest.fixture
def live_server(tmp_path):
    """Index a copy of the fixture and yield a running MCP subprocess."""
    root = tmp_path / "repo"
    shutil.copytree(
        FIXTURE_SRC, root, ignore=shutil.ignore_patterns(".snapctx", "__pycache__")
    )
    index_root(root)

    proc = subprocess.Popen(
        [str(MCP_BIN), "--root", str(root), "--no-warm"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv() -> dict:
        line = proc.stdout.readline()
        assert line, f"server exited: stderr={proc.stderr.read()!r}"
        return json.loads(line)

    # Handshake
    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        }
    )
    recv()
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    try:
        yield send, recv
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_mcp_lists_all_five_tools(live_server):
    send, recv = live_server
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    r = recv()
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == {"context", "search", "expand", "outline", "source"}


def test_mcp_context_returns_json_text_content(live_server):
    send, recv = live_server
    send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "context", "arguments": {"query": "refresh session"}},
        }
    )
    r = recv()
    content = r["result"]["content"]
    assert content and content[0]["type"] == "text"
    payload = json.loads(content[0]["text"])
    assert payload["seeds"]
    assert payload["seeds"][0]["qname"] == "sample_pkg.auth:SessionManager.refresh"


def test_mcp_source_returns_body(live_server):
    send, recv = live_server
    send(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "source",
                "arguments": {"qname": "sample_pkg.utils:hash_token"},
            },
        }
    )
    r = recv()
    payload = json.loads(r["result"]["content"][0]["text"])
    assert "def hash_token" in payload["source"]
