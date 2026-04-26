"""Shell-script parser: function detection, intra-script call edges,
``source``/``.`` imports, and module-level docstring extraction.

Tests use ``tmp_path`` and exercise the parser via ``index_root`` (so
walker → parser → index integration is covered too).
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root, outline, search_code
from snapctx.parsers.shell import ShellParser


def _write(root: Path, name: str, body: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def test_module_symbol_with_leading_comment_docstring(tmp_path: Path) -> None:
    f = _write(tmp_path, "deploy.sh", (
        "#!/bin/bash\n"
        "# Deploys the staging environment.\n"
        "# Reads AWS credentials from ~/.aws.\n"
        "\n"
        "echo hi\n"
    ))
    parser = ShellParser()
    result = parser.parse(f, tmp_path)

    modules = [s for s in result.symbols if s.kind == "module"]
    assert len(modules) == 1
    m = modules[0]
    assert m.qname == "deploy:"
    assert m.docstring is not None
    assert "staging" in m.docstring
    assert "AWS" in m.docstring


def test_posix_function_definition(tmp_path: Path) -> None:
    f = _write(tmp_path, "lib.sh", (
        "setup() {\n"
        "    echo 'setting up'\n"
        "}\n"
    ))
    result = ShellParser().parse(f, tmp_path)
    funcs = [s for s in result.symbols if s.kind == "function"]
    assert len(funcs) == 1
    assert funcs[0].qname == "lib:setup"
    assert funcs[0].parent_qname == "lib:"


def test_ksh_function_definition(tmp_path: Path) -> None:
    """``function NAME { … }`` (no parens) is a valid bash form."""
    f = _write(tmp_path, "lib.sh", (
        "function deploy {\n"
        "    echo 'deploying'\n"
        "}\n"
    ))
    result = ShellParser().parse(f, tmp_path)
    funcs = [s for s in result.symbols if s.kind == "function"]
    assert any(s.qname == "lib:deploy" for s in funcs)


def test_function_docstring_from_preceding_comments(tmp_path: Path) -> None:
    f = _write(tmp_path, "ops.sh", (
        "# Restart the worker.\n"
        "# Waits 5s for graceful shutdown.\n"
        "restart_worker() {\n"
        "    kill $WORKER_PID\n"
        "}\n"
    ))
    result = ShellParser().parse(f, tmp_path)
    fn = next(s for s in result.symbols if s.qname == "ops:restart_worker")
    assert fn.docstring is not None
    assert "Restart" in fn.docstring
    assert "graceful" in fn.docstring


def test_intra_script_call_resolves_to_qname(tmp_path: Path) -> None:
    f = _write(tmp_path, "deploy.sh", (
        "setup_aws() {\n"
        "    echo 'aws'\n"
        "}\n"
        "\n"
        "deploy() {\n"
        "    setup_aws\n"
        "    echo 'deploying'\n"
        "}\n"
    ))
    result = ShellParser().parse(f, tmp_path)
    deploy_calls = [c for c in result.calls if c.caller_qname == "deploy:deploy"]
    assert len(deploy_calls) == 1
    assert deploy_calls[0].callee_name == "setup_aws"
    assert deploy_calls[0].callee_qname == "deploy:setup_aws"


def test_external_command_is_not_emitted_as_call(tmp_path: Path) -> None:
    """``aws s3 cp`` shouldn't become a call edge — ``aws`` is an
    external binary, not a defined function."""
    f = _write(tmp_path, "deploy.sh", (
        "deploy() {\n"
        "    aws s3 cp file s3://bucket/\n"
        "    docker build .\n"
        "}\n"
    ))
    result = ShellParser().parse(f, tmp_path)
    callees = {c.callee_name for c in result.calls}
    assert "aws" not in callees
    assert "docker" not in callees


def test_source_directive_emits_import(tmp_path: Path) -> None:
    f = _write(tmp_path, "main.sh", (
        "source ./lib/util.sh\n"
        ". ./helpers.sh\n"
        "echo done\n"
    ))
    result = ShellParser().parse(f, tmp_path)
    modules = sorted(i.module for i in result.imports)
    assert modules == ["helpers", "lib/util"]


def test_brace_in_string_doesnt_break_function_extent(tmp_path: Path) -> None:
    """A ``}`` inside a quoted string mustn't be treated as the end of
    the function body."""
    f = _write(tmp_path, "tricky.sh", (
        "make_json() {\n"
        '    echo "{ \\"k\\": \\"v\\" }"\n'
        "    echo 'a } b'\n"
        "    echo done\n"
        "}\n"
    ))
    result = ShellParser().parse(f, tmp_path)
    fn = next(s for s in result.symbols if s.qname == "tricky:make_json")
    # The function's line range should cover the whole body, including
    # the line with the ``}`` inside the string.
    assert fn.line_end - fn.line_start >= 4


def test_call_inside_pipeline_is_detected(tmp_path: Path) -> None:
    """Bash pipelines and command chains: ``setup && deploy`` —
    ``deploy`` is also at command position after ``&&``."""
    f = _write(tmp_path, "ci.sh", (
        "setup() { echo s; }\n"
        "deploy() { echo d; }\n"
        "main() {\n"
        "    setup && deploy\n"
        "}\n"
    ))
    result = ShellParser().parse(f, tmp_path)
    main_calls = {c.callee_name for c in result.calls if c.caller_qname == "ci:main"}
    assert main_calls == {"setup", "deploy"}


def test_call_in_comment_is_ignored(tmp_path: Path) -> None:
    f = _write(tmp_path, "ci.sh", (
        "setup() { echo s; }\n"
        "main() {\n"
        "    # call setup here later\n"
        "    echo todo\n"
        "}\n"
    ))
    result = ShellParser().parse(f, tmp_path)
    main_calls = [c for c in result.calls if c.caller_qname == "ci:main"]
    assert main_calls == []


def test_index_root_picks_up_shell_files(tmp_path: Path) -> None:
    """End-to-end: walker + parser registry actually ingest .sh files."""
    _write(tmp_path, "scripts/deploy.sh", (
        "deploy() { echo deploying; }\n"
    ))
    _write(tmp_path, "app.py", "def main(): pass\n")

    summary = index_root(tmp_path)
    assert summary["files_updated"] >= 2

    res = search_code("deploy", k=5, root=tmp_path)
    qnames = {r["qname"] for r in res["results"]}
    assert "scripts/deploy:deploy" in qnames


def test_outline_of_shell_file(tmp_path: Path) -> None:
    f = _write(tmp_path, "ops.sh", (
        "setup() { echo s; }\n"
        "deploy() { echo d; }\n"
        "teardown() { echo t; }\n"
    ))
    index_root(tmp_path)

    out = outline(f, root=tmp_path)
    qnames = {s["qname"] for s in out["symbols"]}
    children = {
        c["qname"]
        for s in out["symbols"]
        for c in s.get("children", [])
    }
    assert "ops:" in qnames  # module
    assert {"ops:setup", "ops:deploy", "ops:teardown"} <= children
