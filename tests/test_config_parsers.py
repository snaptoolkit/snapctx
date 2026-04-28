"""Config-file parsers — TOML, JSON, YAML, .env."""

from __future__ import annotations

from pathlib import Path

from snapctx.parsers.config import (
    EnvParser,
    JsonParser,
    TomlParser,
    YamlParser,
)


def test_toml_emits_module_table_and_keys(tmp_path: Path) -> None:
    f = tmp_path / "cfg.toml"
    f.write_text(
        "# Top-level config\n"
        'name = "demo"\n'
        "\n"
        "[database]\n"
        'url = "postgres://localhost"\n'
        "pool_size = 10\n"
    )
    result = TomlParser().parse(f, tmp_path)
    qnames = {s.qname for s in result.symbols}
    assert "cfg.toml:" in qnames
    assert "cfg.toml:database" in qnames
    assert "cfg.toml:database.url" in qnames
    assert "cfg.toml:database.pool_size" in qnames
    assert "cfg.toml:name" in qnames


def test_toml_invalid_still_yields_module(tmp_path: Path) -> None:
    f = tmp_path / "bad.toml"
    f.write_text("[unclosed\nx = ")
    result = TomlParser().parse(f, tmp_path)
    # Module symbol always emitted; key extraction stops on parse failure.
    assert any(s.kind == "module" for s in result.symbols)


def test_json_emits_top_level_keys_only(tmp_path: Path) -> None:
    f = tmp_path / "pkg.json"
    f.write_text(
        "{\n"
        '  "name": "demo",\n'
        '  "version": "1.0",\n'
        '  "scripts": {\n'
        '    "test": "pytest"\n'
        "  }\n"
        "}\n"
    )
    result = JsonParser().parse(f, tmp_path)
    qnames = {s.qname for s in result.symbols}
    assert "pkg.json:name" in qnames
    assert "pkg.json:version" in qnames
    assert "pkg.json:scripts" in qnames
    # Nested key should NOT be a top-level symbol.
    assert "pkg.json:test" not in qnames


def test_json_invalid_still_yields_module(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text("{invalid")
    result = JsonParser().parse(f, tmp_path)
    assert any(s.kind == "module" for s in result.symbols)


def test_yaml_emits_top_level_keys(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text(
        "# CI config\n"
        "name: demo\n"
        "version: 1.0\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
    )
    result = YamlParser().parse(f, tmp_path)
    qnames = {s.qname for s in result.symbols}
    assert "config.yaml:name" in qnames
    assert "config.yaml:version" in qnames
    assert "config.yaml:jobs" in qnames
    # Nested key should not appear.
    assert "config.yaml:build" not in qnames
    assert "config.yaml:runs-on" not in qnames


def test_yaml_handles_yml_extension(tmp_path: Path) -> None:
    f = tmp_path / "ci.yml"
    f.write_text("foo: bar\n")
    result = YamlParser().parse(f, tmp_path)
    assert any(s.qname == "ci.yml:foo" for s in result.symbols)


def test_env_parser_extracts_keys(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text(
        "# Production env\n"
        "DATABASE_URL=postgres://localhost\n"
        "DEBUG=false\n"
        "export API_KEY=secret\n"
    )
    result = EnvParser().parse(f, tmp_path)
    qnames = {s.qname for s in result.symbols}
    assert ".env:DATABASE_URL" in qnames
    assert ".env:DEBUG" in qnames
    assert ".env:API_KEY" in qnames


def test_env_parser_skips_comments_and_blanks(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text(
        "# header\n"
        "\n"
        "FOO=1\n"
        "# another\n"
        "BAR=2\n"
    )
    result = EnvParser().parse(f, tmp_path)
    keys = [s for s in result.symbols if s.kind == "constant"]
    assert {s.qname for s in keys} == {".env:FOO", ".env:BAR"}
