"""``grep_files`` — literal/regex search across every text file under a root."""

from __future__ import annotations

from pathlib import Path

from snapctx.api import grep_files, index_root


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "settings.toml").write_text(
        "# Database config\n"
        "[database]\n"
        'url = "postgres://localhost/dev"\n'
    )
    (pkg / "service.py").write_text(
        '"""Service."""\n\n'
        "DATABASE_URL = 'postgres://localhost/dev'\n\n"
        "def connect():\n"
        '    """Open a DB connection."""\n'
        "    return DATABASE_URL\n"
    )
    (repo / "README.md").write_text(
        "# Project\n\n"
        "## Database\n\n"
        "We use Postgres for everything.\n"
    )
    (repo / ".env").write_text(
        "DATABASE_URL=postgres://localhost/dev\n"
        "DEBUG=true\n"
    )
    index_root(repo)
    return repo


def test_grep_finds_literal_in_code_and_annotates_with_qname(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = grep_files("DATABASE_URL", root=repo)
    assert "error" not in result
    files = {Path(m["file"]).name for m in result["matches"]}
    assert "service.py" in files
    code_hits = [m for m in result["matches"] if m["file"].endswith("service.py")]
    # The hit on the function body line gets the function qname; the
    # module-level constant line falls inside the module's range.
    qnames = {m.get("qname") for m in code_hits if "qname" in m}
    assert any("connect" in (q or "") for q in qnames), qnames


def test_grep_finds_in_markdown_and_env_files(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = grep_files("DATABASE_URL", root=repo)
    files = {Path(m["file"]).name for m in result["matches"]}
    assert ".env" in files, "grep must reach .env files"
    # README.md doesn't contain DATABASE_URL — search for "Postgres" instead.
    md_result = grep_files("Postgres", root=repo)
    md_files = {Path(m["file"]).name for m in md_result["matches"]}
    assert "README.md" in md_files


def test_grep_regex_mode(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = grep_files(r"DATABASE_\w+", root=repo, regex=True)
    assert result["match_count"] >= 2
    # Invalid regex returns a structured error.
    bad = grep_files("(unclosed", root=repo, regex=True)
    assert bad.get("error") == "invalid_regex"


def test_grep_case_insensitive(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    hit_default = grep_files("database", root=repo)
    hit_ci = grep_files("database", root=repo, case_insensitive=True)
    assert hit_ci["match_count"] > hit_default["match_count"]


def test_grep_in_path_narrows_walk(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    full = grep_files("DATABASE_URL", root=repo)
    scoped = grep_files("DATABASE_URL", root=repo, in_path="pkg")
    assert scoped["match_count"] < full["match_count"]
    assert all("/pkg/" in m["file"] for m in scoped["matches"])


def test_grep_skips_binary_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f(): return 'magic_token'\n")
    (repo / "blob.bin").write_bytes(b"magic_token\x00\x00\x00\x00")
    index_root(repo)
    result = grep_files("magic_token", root=repo)
    files = {Path(m["file"]).name for m in result["matches"]}
    assert "a.py" in files
    assert "blob.bin" not in files


def test_grep_respects_gitignore(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("ignored/\n")
    (repo / "a.py").write_text("X = 'present'\n")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "b.py").write_text("Y = 'present'\n")
    index_root(repo)
    result = grep_files("present", root=repo)
    files = {Path(m["file"]).name for m in result["matches"]}
    assert "a.py" in files
    assert "b.py" not in files


def test_grep_empty_pattern_returns_hint(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    result = grep_files("", root=repo)
    assert result["match_count"] == 0
    assert "non-empty" in result["hint"].lower()


def test_grep_max_results_truncates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "many.txt").write_text("\n".join(f"line {i} target" for i in range(50)))
    index_root(repo)
    result = grep_files("target", root=repo, max_results=10)
    assert result["match_count"] == 10
    assert result["truncated"]


def test_grep_context_lines(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("alpha\nbravo\ncharlie\ndelta\necho\n")
    index_root(repo)
    result = grep_files("charlie", root=repo, context_lines=2)
    m = result["matches"][0]
    assert m["before"] == ["alpha", "bravo"]
    assert m["after"] == ["delta", "echo"]
