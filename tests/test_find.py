"""``find`` — exhaustive literal-substring search over indexed bodies.

This is the structural complement to ``search``: ranking-free, top-K-free
enumeration of every symbol whose source body contains a literal.
Closes the audit-class gap where grep+read trivially enumerates every
match but ``search`` ranks and cuts off.
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import find_literal, index_root


def _write(root: Path, name: str, body: str) -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_find_enumerates_every_match(tmp_path: Path) -> None:
    """The structural win over ranked search: every site, not the top-K."""
    repo = tmp_path / "repo"
    # 10 functions, each containing the literal — beyond a -k=5 search cap.
    for i in range(10):
        _write(repo, f"mod{i}.py", (
            f"def caller_{i}():\n"
            f"    with transaction.atomic():\n"
            f"        do_thing_{i}()\n"
        ))
    # Plus a decoy that doesn't contain the literal.
    _write(repo, "unrelated.py", "def helper(): pass\n")
    index_root(repo)

    out = find_literal("transaction.atomic", root=repo)
    assert out["match_count"] == 10
    assert out["truncated"] is False
    qnames = {m["qname"] for m in out["matches"]}
    assert qnames == {f"mod{i}:caller_{i}" for i in range(10)}


def test_find_returns_innermost_symbol(tmp_path: Path) -> None:
    """A method nested in a class beats the class for the same line."""
    repo = tmp_path / "repo"
    _write(repo, "svc.py", (
        "class Service:\n"
        "    def run(self):\n"
        "        with transaction.atomic():\n"
        "            do_work()\n"
    ))
    index_root(repo)

    out = find_literal("transaction.atomic", root=repo)
    assert out["match_count"] == 1
    hit = out["matches"][0]
    # The method, not the enclosing class.
    assert hit["qname"] == "svc:Service.run"


def test_find_with_bodies_inlines_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo, "svc.py", (
        "def caller():\n"
        "    with transaction.atomic():\n"
        "        do_work()\n"
    ))
    index_root(repo)

    out = find_literal("transaction.atomic", root=repo, with_bodies=True)
    body = out["matches"][0]["source"]
    assert "transaction.atomic" in body
    assert "do_work" in body


def test_find_in_path_narrows_scan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "wanted").mkdir(parents=True)
    (repo / "other").mkdir(parents=True)
    _write(repo, "wanted/a.py", "def in_wanted():\n    transaction.atomic\n")
    _write(repo, "other/b.py", "def in_other():\n    transaction.atomic\n")
    index_root(repo)

    out = find_literal(
        "transaction.atomic", root=repo, in_path=str(repo / "wanted"),
    )
    qnames = {m["qname"] for m in out["matches"]}
    assert qnames == {"wanted/a:in_wanted"} or qnames == {"wanted.a:in_wanted"}


def test_find_kind_filter(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo, "x.py", (
        "TRANS = 'transaction.atomic flag'\n"  # constant
        "def caller():\n"
        "    transaction.atomic()\n"
    ))
    index_root(repo)

    only_funcs = find_literal(
        "transaction.atomic", root=repo, kind="function",
    )
    qnames = {m["qname"] for m in only_funcs["matches"]}
    assert "x:caller" in qnames


def test_find_no_matches_returns_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo, "x.py", "def hello(): pass\n")
    index_root(repo)

    out = find_literal("nonexistent_pattern_zzz", root=repo)
    assert out["match_count"] == 0
    assert out["matches"] == []
    assert "No symbol body contains" in out["hint"]


def test_find_match_text_carries_the_line(tmp_path: Path) -> None:
    """Each match exposes the line number and original line text — the
    grep-style breadcrumb the agent uses to read what was actually matched
    without re-fetching the body."""
    repo = tmp_path / "repo"
    _write(repo, "x.py", (
        "def caller():\n"
        "    result = transaction.atomic_with_args(timeout=5)\n"
    ))
    index_root(repo)

    out = find_literal("transaction.atomic_with_args", root=repo)
    hit = out["matches"][0]
    assert hit["match_line"] == 2
    assert "transaction.atomic_with_args" in hit["match_text"]


def test_find_max_results_truncates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for i in range(15):
        _write(repo, f"m{i}.py", f"def f{i}():\n    do_thing_marker()\n")
    index_root(repo)

    out = find_literal("do_thing_marker", root=repo, max_results=5)
    assert out["match_count"] == 5
    assert out["truncated"] is True


def test_find_with_callers_attaches_deduped_callers(tmp_path: Path) -> None:
    """The audit-with-impact case: each hit lists the distinct callers that
    invoke it, so "every X site AND who triggers them" is one call.
    """
    repo = tmp_path / "repo"
    # callee contains the literal; callers do not (so they don't show up
    # as direct find hits, only attached to the callee).
    _write(repo, "svc.py", (
        "def writer():\n"
        "    transaction.atomic()\n"
        "\n"
        "def first(): writer()\n"
        "def second(): writer()\n"
        "def first_again(): writer()\n"  # second call from `first` should dedupe
    ))
    index_root(repo)

    out = find_literal("transaction.atomic", root=repo, with_callers=True)
    assert out["match_count"] == 1
    callers = out["matches"][0]["callers"]
    assert callers is not None
    caller_qnames = {c["qname"] for c in callers}
    assert caller_qnames == {"svc:first", "svc:second", "svc:first_again"}
    # Each entry has a representative line.
    assert all("line" in c for c in callers)


def test_find_with_callers_no_callers_omits_field(tmp_path: Path) -> None:
    """A symbol with zero callers shouldn't get an empty `callers` field."""
    repo = tmp_path / "repo"
    _write(repo, "svc.py", "def writer():\n    transaction.atomic()\n")
    index_root(repo)

    out = find_literal("transaction.atomic", root=repo, with_callers=True)
    assert "callers" not in out["matches"][0]


def test_find_via_cli_command(tmp_path: Path) -> None:
    """End-to-end CLI invocation through the QueryCommand registry."""
    import subprocess
    import json
    import sys

    repo = tmp_path / "repo"
    _write(repo, "x.py", "def caller():\n    transaction.atomic\n")
    index_root(repo)

    proc = subprocess.run(
        [sys.executable, "-m", "snapctx.cli", "find", "transaction.atomic", "--root", str(repo)],
        capture_output=True, text=True, cwd=repo,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["match_count"] == 1
