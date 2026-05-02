"""Tests for the routing-decorator boost in ``rrf_merge``.

When a query contains a routing-implying token (``api``, ``endpoint``,
``view``, ``route``, ``handler``, ``webhook``, ``url``, ``request``,
``controller``) AND a candidate row carries a routing decorator
(``@api_view``, ``@app.get(...)``, ``@route(...)``, etc.), the
candidate's score is multiplied by ``route_decorator_boost``.

Why: surfaced by the biblereader benchmark — Django REST views
named ``read`` / ``list`` / ``create`` (the standard verb-style
DRF naming) were buried by class methods of the same name on
unrelated symbols. The decorator is the strongest signal that
"this is a view"; we just weren't using it.
"""

from __future__ import annotations

from snapctx.api._ranking import (
    _has_route_decorator,
    _query_implies_route,
    rrf_merge,
)


def _row(qname: str, decorators: str = "", file: str = "x.py", language: str = "python") -> dict:
    """Build a fake symbol row that quacks like the sqlite3 row passed
    to rrf_merge."""
    return {
        "qname": qname,
        "decorators": decorators,
        "file": file,
        "language": language,
    }


# ---------- helper predicates ----------


def test_query_implies_route_picks_up_view_token() -> None:
    assert _query_implies_route("which view returns verses")
    assert _query_implies_route("API endpoint for chapter")
    assert _query_implies_route("handler for the webhook")
    assert not _query_implies_route("how does authentication work")
    assert not _query_implies_route("compute hash digest")
    assert not _query_implies_route("")
    assert not _query_implies_route(None)


def test_has_route_decorator_recognizes_drf_and_fastapi() -> None:
    assert _has_route_decorator("@api_view(['GET'])\n@permission_classes([IsAuthenticated])")
    assert _has_route_decorator("@action(detail=True, methods=['post'])")
    assert _has_route_decorator("@app.get('/users')")
    assert _has_route_decorator("@router.post('/items')")
    assert _has_route_decorator("@blueprint.route('/')")
    # Non-routing decorators must not match.
    assert not _has_route_decorator("@admin.display(description='Active')")
    assert not _has_route_decorator("@cached_property")
    assert not _has_route_decorator("@dataclass(frozen=True)")
    assert not _has_route_decorator("")
    assert not _has_route_decorator(None)


# ---------- merge integration ----------


def test_route_query_promotes_decorated_candidate() -> None:
    """``read`` undecorated + ``read`` with ``@api_view`` decorator,
    same lexical/vector rank — the decorated one should rank higher
    when the query says ``view``."""
    plain = _row("translation.svc:Reader.read")
    decorated = _row("translation.views:read", decorators="@api_view(['GET'])")
    # Both at rank 1 in lexical, equal-ish in vector — without the
    # boost, RRF treats them as ties.
    lex = [(plain, 1.0), (decorated, 1.0)]
    vec = [(plain, 1.0), (decorated, 1.0)]
    out = rrf_merge(lex, vec, query="API view that returns verses")
    top_qname = out[0][0]["qname"]
    assert top_qname == "translation.views:read", out


def test_non_route_query_does_not_promote_decorated_candidate() -> None:
    """Boost is gated on the query — without a routing token, the
    decorator is ignored and ranking falls back to RRF only."""
    plain = _row("a:foo")
    decorated = _row("b:bar", decorators="@api_view(['GET'])")
    lex = [(plain, 1.0), (decorated, 1.0)]
    vec = [(plain, 1.0), (decorated, 1.0)]
    out = rrf_merge(lex, vec, query="how does the parser tokenize input")
    qnames = [row["qname"] for row, _ in out]
    # Original RRF order with stable tie-breaking; the decorated row
    # gets no special treatment.
    assert qnames == ["a:foo", "b:bar"]


def test_route_query_no_decorator_falls_back_to_rrf() -> None:
    """Route-implying query but no candidate has a routing decorator —
    boost is a no-op; ranking is plain RRF."""
    a = _row("a:f")
    b = _row("b:g")
    lex = [(a, 1.0), (b, 1.0)]
    vec = [(b, 1.0), (a, 1.0)]
    out = rrf_merge(lex, vec, query="which view handles the request")
    # Normal RRF (a and b each appear once in each list at rank 1) — tie.
    assert {row["qname"] for row, _ in out} == {"a:f", "b:g"}
