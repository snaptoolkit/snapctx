"""Unit tests for ``search_hint`` — the one-line nudge attached to every
``snapctx_search`` response.

The hint matters because it's the agent's main feedback loop: when the
result set is broad or low-quality, the hint is the cheapest place to
suggest a better follow-up call. We test the priority order
explicitly: empty → audit → mixed-kind → next-action.
"""

from __future__ import annotations

from snapctx.api._ranking import search_hint


def test_empty_results_recommends_synonyms_or_kind() -> None:
    hint = search_hint([], query="rate limiter")
    assert "synonyms" in hint.lower() or "kind" in hint.lower()


def test_audit_query_suggests_with_bodies_when_unset() -> None:
    """The ranker spots audit phrasing and nudges toward the
    one-shot ``--with-bodies`` flow, not body-pull-per-hit."""
    results = [{
        "qname": "pkg.mod:foo", "kind": "function", "score": 0.5,
        "next_action": "read_body",
    }]
    hint = search_hint(results, query="find all the places that call foo")
    assert "with-bodies" in hint.lower()


def test_mixed_kind_top3_suggests_kind_filter() -> None:
    """When --kind isn't set and the top 3 hits span 3+ different
    kinds, suggest narrowing — saves the trial-and-error rounds an
    agent would otherwise do on broad queries like 'view'."""
    results = [
        {"qname": "a:f", "kind": "function", "score": 0.5, "next_action": "read_body"},
        {"qname": "a:C", "kind": "class",    "score": 0.4, "next_action": "outline"},
        {"qname": "a:M", "kind": "module",   "score": 0.3, "next_action": "outline"},
        {"qname": "a:K", "kind": "constant", "score": 0.2, "next_action": "read_body"},
    ]
    hint = search_hint(results, query="view")
    assert "kind" in hint.lower()
    # Lists at least two of the kinds it observed.
    observed_in_hint = sum(
        kind in hint for kind in ("function", "class", "module", "constant")
    )
    assert observed_in_hint >= 2


def test_mixed_kind_skipped_when_kind_filter_already_set() -> None:
    """If the user already passed --kind, don't tell them to."""
    results = [
        {"qname": "a:f", "kind": "function", "score": 0.5, "next_action": "read_body"},
        {"qname": "a:C", "kind": "class",    "score": 0.4, "next_action": "outline"},
        {"qname": "a:M", "kind": "module",   "score": 0.3, "next_action": "outline"},
    ]
    hint = search_hint(results, query="view", kind_filter="function")
    assert "Re-run with --kind" not in hint


def test_uniform_kind_does_not_trigger_mixed_nudge() -> None:
    """Three hits of the same kind shouldn't trigger the mixed-kind
    nudge — the agent's filter is already implicitly tight."""
    results = [
        {"qname": "a:f1", "kind": "function", "score": 0.5, "next_action": "read_body"},
        {"qname": "a:f2", "kind": "function", "score": 0.4, "next_action": "read_body"},
        {"qname": "a:f3", "kind": "function", "score": 0.3, "next_action": "read_body"},
    ]
    hint = search_hint(results, query="parse")
    assert "Re-run with --kind" not in hint


def test_next_action_expand_is_fallback_when_no_other_hints() -> None:
    results = [{
        "qname": "pkg.mod:Cls", "kind": "class", "score": 0.5,
        "next_action": "expand",
    }]
    hint = search_hint(results, query="cls")
    assert "expand" in hint
