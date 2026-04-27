"""Regression: ``get_source`` and ``expand`` accept common LLM paraphrases of qnames.

LLMs generating qname references occasionally:
* keep the file extension on a TS qname (``components/Verse.tsx:Verse``
  instead of the canonical ``components/Verse:Verse``).
* apply Python dotted style to TS files (``components.Verse:Verse``)
  or, less commonly, slashed style to Python (``parser/utils:func``).

These all used to return ``not_found``, which broke navigator agents
that paraphrased mid-conversation. Now we canonicalize them on lookup
and tell the caller what the canonical form was.
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import expand, get_source, index_root


def _build_repo(tmp_path: Path) -> Path:
    """Create a tiny repo with one Python and one TS file, then index it."""
    repo = tmp_path / "repo"
    (repo / "components").mkdir(parents=True)
    (repo / "lib").mkdir(parents=True)

    (repo / "components" / "Verse.tsx").write_text(
        "export function Verse(props) { return props.text; }\n"
    )
    (repo / "lib" / "utils.py").write_text(
        '"""Utility functions."""\n\n'
        "def fetch_verse():\n"
        '    """Fetch a verse from the database."""\n'
        "    return 'verse'\n"
    )
    index_root(repo)
    return repo


def test_get_source_resolves_paraphrased_tsx_extension(tmp_path: Path) -> None:
    """LLM kept ``.tsx`` on the module — we strip and resolve."""
    repo = _build_repo(tmp_path)
    out = get_source("components/Verse.tsx:Verse", root=repo)

    assert "error" not in out, out
    assert out["qname"] == "components/Verse:Verse"
    assert "paraphrase_hint" in out
    assert ".tsx" in out["paraphrase_hint"]


def test_get_source_resolves_dotted_to_slashed_for_ts(tmp_path: Path) -> None:
    """LLM applied Python dotted style to a TS file — we swap separators."""
    repo = _build_repo(tmp_path)
    out = get_source("components.Verse:Verse", root=repo)

    assert "error" not in out, out
    assert out["qname"] == "components/Verse:Verse"
    assert "paraphrase_hint" in out


def test_get_source_canonical_qname_has_no_paraphrase_hint(tmp_path: Path) -> None:
    """When the qname is already canonical, the hint should be absent."""
    repo = _build_repo(tmp_path)
    out = get_source("components/Verse:Verse", root=repo)

    assert "error" not in out
    assert "paraphrase_hint" not in out


def test_expand_resolves_paraphrased_qname(tmp_path: Path) -> None:
    """``expand`` should also tolerate paraphrases."""
    repo = _build_repo(tmp_path)
    out = expand("components/Verse.tsx:Verse", root=repo)

    assert out.get("error") != "not_found", out
    assert out["qname"] == "components/Verse:Verse"
    assert "paraphrase_hint" in out


def test_truly_unknown_qname_still_returns_not_found(tmp_path: Path) -> None:
    """Sanity: paraphrase resolution doesn't mask real misses."""
    repo = _build_repo(tmp_path)
    out = get_source("does/not:Exist", root=repo)

    assert out.get("error") == "not_found"
