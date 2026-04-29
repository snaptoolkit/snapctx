"""Query classification, FTS query construction, and rank fusion.

The ranking layer decides *how* to score search hits. It's intentionally
separate from ``_search`` so swapping in a different ranker (a tuned
RRF, a learned-to-rank scorer, etc.) doesn't drag the search-orchestration
code along with it.

Three kinds of work live here:

1. **Query shaping** — turning a free-form user query into the FTS5
   ``MATCH`` syntax (``_build_fts_query``) and tokenizing for stopword
   counts (``_tokenize_query``).
2. **Query classification** — labelling a query as ``identifier`` /
   ``natural`` / ``mixed`` so the hybrid ranker can pick weights.
3. **Rank fusion** — Reciprocal Rank Fusion of the lexical and vector
   rankings, with a test-file penalty so test code never crowds out
   real domain code in the top-K.
"""

from __future__ import annotations

import re
import sqlite3


# Words that strongly signal a query is English prose rather than a symbol
# lookup. Used by ``classify_query`` to pick ranker weights — not stripped
# from the query itself. We're conservative: only common "wh-" words,
# auxiliaries, and a handful of prepositions. A snake_case identifier like
# ``user_is_active`` contains "is" but we care about token-level matches,
# not substring.
_NL_STOPWORDS = frozenset({
    "how", "what", "why", "where", "when", "which", "who", "whose",
    "does", "do", "did", "is", "are", "was", "were", "be", "been", "being",
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at",
    "from", "with", "by", "about", "after", "before", "between", "into",
    "through", "via", "over", "under", "that", "this", "these", "those",
})


_CAMEL_RE = re.compile(r"[a-z][A-Z]")


def build_fts_query(user_query: str) -> str:
    """Map a natural-language-ish query into FTS5 MATCH syntax.

    Splits the input into bare tokens and ORs them, so a multi-word query
    matches any of the terms. FTS5's own tokenizer handles further normalization.
    """
    tokens = [t for t in tokenize_query(user_query) if t]
    if not tokens:
        return user_query
    return " OR ".join(tokens)


def tokenize_query(q: str) -> list[str]:
    return [t for t in re.findall(r"\w+", q.lower()) if t]


def looks_like_identifier(token: str) -> bool:
    """Heuristic: does this token look like a source-code identifier?"""
    if not token:
        return False
    if any(c in token for c in "._:/"):
        return True          # dotted / qname-ish
    if "_" in token and token.replace("_", "").isalnum():
        return True          # snake_case
    if token.isupper() and len(token) >= 2:
        return True          # CONSTANT_CASE
    if _CAMEL_RE.search(token):
        return True          # camelCase / PascalCase
    return False


def classify_query(query: str) -> str:
    """Return 'identifier' | 'natural' | 'mixed'.

    - ``identifier``: ≤ 2 tokens AND at least one looks like a source
      identifier (camelCase, snake_case, dotted, or qname).
    - ``natural``: 5+ tokens with at least one English stopword, OR 4+ tokens
      with 2+ stopwords.
    - ``mixed``: everything else (short freeform like "rate limit", or
      medium-length hybrid queries).
    """
    raw_tokens = query.split()
    tokens = tokenize_query(query)
    if not raw_tokens:
        return "mixed"
    # Identifier lookup: a dotted qname like ``apps.auth:login`` is a single
    # raw word even though it contains multiple ``\w+`` matches, so count
    # whitespace-split words here.
    if len(raw_tokens) <= 2 and any(looks_like_identifier(t) for t in raw_tokens):
        return "identifier"
    n_stop = sum(1 for t in tokens if t in _NL_STOPWORDS)
    if (len(tokens) >= 5 and n_stop >= 1) or (len(tokens) >= 4 and n_stop >= 2):
        return "natural"
    return "mixed"


def hybrid_weights(qclass: str) -> tuple[float, float]:
    """Map a query class to ``(lex_weight, vec_weight)`` for RRF.

    "How does the frontend …" is natural prose — trust the embedding model.
    ``run_exscript`` is an identifier lookup — BM25 nails it exactly.
    A short freeform "rate limit" falls in the middle.
    """
    if qclass == "natural":
        return 0.5, 2.5
    if qclass == "identifier":
        return 1.5, 0.8
    return 1.0, 1.5


DOC_LANGUAGES = frozenset({"markdown", "html", "text"})


def rrf_merge(
    lexical_pairs,
    vector_pairs,
    *,
    k_fuse: int = 60,
    limit: int = 5,
    lex_weight: float = 1.0,
    vec_weight: float = 1.5,
    test_penalty: float = 0.6,
    docs_penalty: float = 0.7,
    query: str | None = None,
):
    """Weighted Reciprocal Rank Fusion of two ranked lists of (row, score) tuples.

    Defaults (tuned empirically on the biblereader benchmark):
      * ``vec_weight=1.5`` — embeddings beat BM25 on identifier-heavy queries
        because BM25's camel/snake token splits mix real matches with noise.
      * ``test_penalty=0.6`` — symbols living under a ``tests/`` path or in a
        module ending in ``.tests`` or starting with ``test_`` get their RRF
        score multiplied by this factor. Agents almost never want a test as
        their top hit, but tests still appear (just demoted).
      * ``docs_penalty=0.7`` — for natural / mixed queries where at least
        one *code-language* candidate is in the pool, demote markdown /
        html / text seeds. The HTML+templates parser indexes prose, so a
        feature query like "how does X work" otherwise pulls a README
        heading above the implementation. The penalty is conditional:
        when only doc seeds exist (e.g. "where is the install guide"
        in a docs-only repo), no penalty applies and docs win cleanly.

    When ``query`` is a single identifier-shaped token (``Button``, ``url_for``,
    ``StateCreator``), symbols whose simple name equals that token receive a
    name-match bonus equivalent to a top-3 lexical hit. Without this, FTS5
    BM25 over signature+docstring lets test methods or impl classes that
    *reference* the identifier outrank the canonical definition.
    """
    name_match_token = _exact_name_token(query)
    qclass = classify_query(query) if query else "mixed"
    docs_demotion_active = qclass in ("natural", "mixed")

    scores: dict[str, float] = {}
    kept: dict[str, object] = {}
    for pairs, weight in ((lexical_pairs, lex_weight), (vector_pairs, vec_weight)):
        for rank, (row, _) in enumerate(pairs, start=1):
            q = row["qname"]
            scores[q] = scores.get(q, 0.0) + weight / (k_fuse + rank)
            kept[q] = row
    if name_match_token is not None:
        # Scale the bonus to the larger of the two weights so an identifier
        # query (lex_weight 1.5, vec_weight 0.8) gets a meaningful bump even
        # when the lex side is buried by sibling tokens. Equivalent to ~a
        # rank-1 hit on the dominant ranker; not enough to flip results when
        # a name-match symbol is genuinely absent from both lists.
        bonus = max(lex_weight, vec_weight) / (k_fuse + 1)
        for q in list(scores):
            if _simple_name(q) == name_match_token:
                scores[q] += bonus
    has_code_seed = any(
        _row_language(row) not in DOC_LANGUAGES for row in kept.values()
    )
    for q, row in kept.items():
        if looks_like_test(q, row["file"]):
            scores[q] *= test_penalty
        if (
            docs_demotion_active
            and has_code_seed
            and _row_language(row) in DOC_LANGUAGES
        ):
            scores[q] *= docs_penalty
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(kept[q], s) for q, s in ordered[:limit]]


def _row_language(row) -> str:
    """``row['language']`` if present; ``''`` otherwise.

    Some test fixtures and call sites pass mock rows that don't carry a
    language column, so we tolerate the missing field instead of
    KeyError-ing — better to skip the docs check than corrupt the merge.
    """
    try:
        return row["language"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _exact_name_token(query: str | None) -> str | None:
    """Return the lowercased token if ``query`` is a single identifier-shaped
    word, else ``None``. Multi-word queries like "session prepare and send"
    don't trigger the name-match bonus — only direct identifier lookups do."""
    if not query:
        return None
    raw = query.strip().split()
    if len(raw) != 1:
        return None
    if not looks_like_identifier(raw[0]):
        # Plain words like ``Button`` aren't camelCase but still common
        # identifier lookups; accept any single bare alphanumeric+underscore
        # token that's at least two characters.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]+", raw[0]):
            return None
    return raw[0].lower()


def _simple_name(qname: str) -> str:
    """Last segment of a qname: ``app.auth:User.login`` → ``login``. Used for
    the name-match bonus so dotted method qnames still match a bare query."""
    tail = qname.split(":", 1)[-1]
    return tail.rsplit(".", 1)[-1].lower()


def looks_like_test(qname: str, file: str) -> bool:
    return (
        "/tests/" in file
        or "/test/" in file
        or file.endswith("/tests.py")
        or "tests:" in qname
        or ":Test" in qname
        or qname.endswith(".tests")
    )


def suggest_next_action(row: sqlite3.Row) -> str:
    if row["kind"] in ("class", "module"):
        return "outline"
    if row["docstring"] and len(row["docstring"]) > 40:
        return "expand"
    return "read_body"


_AUDIT_PHRASE_RE = re.compile(
    r"\b(list every|every place|every call|every use|audit|find all|"
    r"all the|where (?:are|is|do)|enumerate)\b",
    re.IGNORECASE,
)


# Words that audit-style queries pad around the actual identifier we want
# to find. Stripping these keeps the literal extractor from latching onto
# generic English when the query is "every transaction.atomic *site*".
_AUDIT_FILLERS = frozenset({
    "site", "sites", "usage", "usages", "use", "uses", "used",
    "place", "places", "call", "calls", "called", "caller", "callers",
    "occurrence", "occurrences", "instance", "instances",
    "code", "codebase", "repo", "project", "module", "modules",
    "function", "functions", "method", "methods", "class", "classes",
    "model", "models", "field", "fields",
})


def extract_audit_literal(query: str) -> str | None:
    """If the query is an audit phrasing wrapping a single identifier, return it.

    Used by ``context()`` to decide whether to run ``find`` alongside the
    ranked search. Conservative on purpose: returns ``None`` whenever the
    extracted candidate is ambiguous so the agent isn't surprised by a
    misfired exhaustive scan.

    Heuristic: strip the audit phrase itself, then look for identifier-
    shaped tokens (camelCase, snake_case, dotted, CONSTANT_CASE) in the
    remainder. A single dotted token wins outright (``transaction.atomic``
    is unambiguous). Otherwise a single non-filler identifier wins.
    Multiple plausible candidates → ``None``.
    """
    if not query or not _AUDIT_PHRASE_RE.search(query):
        return None
    cleaned = _AUDIT_PHRASE_RE.sub(" ", query)
    tokens = [t.strip(".,;:!?\"'`()[]{}") for t in cleaned.split()]
    tokens = [t for t in tokens if t]

    dotted = [t for t in tokens if "." in t and looks_like_identifier(t)]
    if len(dotted) == 1:
        return dotted[0]
    if len(dotted) > 1:
        return None

    others = [
        t for t in tokens
        if looks_like_identifier(t) and t.lower() not in _AUDIT_FILLERS
    ]
    if len(others) == 1:
        return others[0]
    return None


def search_hint(
    results: list[dict],
    *,
    query: str = "",
    with_bodies: bool = False,
    also_used: bool = False,
) -> str:
    """One-line hint nudging the agent toward the next-best operation.

    The ranker emits these as part of every search response. Beyond the
    classic "next_action on the top hit" cue, we also nudge toward the
    audit-class flags (``--with-bodies``, ``--also``) when the query
    phrasing looks like an audit and those flags weren't used. Saves
    the agent from having to remember the full toolset.
    """
    if not results:
        return (
            "No matches. Try synonyms (e.g. 'throttle' instead of 'rate limit'), "
            "or a different `kind` filter."
        )

    looks_like_audit = bool(_AUDIT_PHRASE_RE.search(query)) if query else False
    if looks_like_audit and not with_bodies:
        return (
            "Audit-class query detected. Add --with-bodies to inline each "
            "hit's source AND pre-resolve referenced constants in one call "
            "(no follow-up `source` needed)."
            + (" Use --also TERM to batch additional keywords." if not also_used else "")
        )

    top = results[0]
    qname = top["qname"]
    if top["next_action"] == "expand":
        return f"To see callees of the top result, call expand({qname!r})."
    if top["next_action"] == "outline":
        return f"To see {qname}'s members, call outline with its file."
    if not with_bodies and any(r.get("next_action") == "read_body" for r in results[:3]):
        return (
            "Several top results suggest reading bodies. Re-run with "
            "--with-bodies to inline source for all hits in one call."
        )
    return f"If the signature isn't enough, call get_source({qname!r})."
