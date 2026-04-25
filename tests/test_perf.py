"""Perf regression test.

Opt-in (gated on ``RUN_PERF=1``) because it generates a ~500-file synthetic
fixture and exercises the embedding model — both relatively expensive.

The thresholds are deliberately generous vs. what we measure locally:
  * cold index, 500 files / ~5 000 symbols: < 30 s wall  (observed ~10 s)
  * warm ``context()`` call after one throwaway call:       < 250 ms  (observed ~10 ms)

The point is to catch regressions like the 85 s walker/embedder issue we
recently fixed, not to hit a tight budget on every CI run. Raise the
thresholds or skip locally if your hardware is slow — don't lower them;
that removes the signal.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PERF") != "1",
    reason="Perf test is opt-in. Re-run with RUN_PERF=1 to exercise it.",
)


def _write_synthetic_repo(root: Path, *, n_files: int = 500, symbols_per_file: int = 10) -> None:
    """Create ``n_files`` .py files under ``root``, each with ~``symbols_per_file`` symbols
    and a small call graph. Keeps the shapes close to real-world code so the
    parser, indexer, and embedder all see realistic input.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "pkg").mkdir(exist_ok=True)
    for i in range(n_files):
        f = root / "pkg" / f"mod_{i:04d}.py"
        body: list[str] = [
            f'"""Module {i} — utilities for the synthetic perf fixture."""',
            "from __future__ import annotations",
            "",
        ]
        if i > 0:
            body.append(f"from pkg.mod_{(i - 1):04d} import helper_0")
        body.append("")
        body.append(f"DEFAULT_TIMEOUT_{i} = 30")
        body.append("")
        for j in range(symbols_per_file // 2):
            body.append(f"def helper_{j}(x: int) -> int:")
            body.append(f"    \"\"\"Helper {j} in module {i}.\"\"\"")
            body.append(f"    return x + {j}")
            body.append("")
        body.append(f"class Widget_{i}:")
        body.append(f"    \"\"\"Widget class for module {i}.\"\"\"")
        body.append("    DEFAULT = 'x'")
        for j in range(symbols_per_file // 2):
            body.append(f"    def method_{j}(self, x: int) -> int:")
            body.append(f"        \"\"\"Method {j} on Widget_{i}.\"\"\"")
            body.append(f"        return self.method_{(j + 1) % (symbols_per_file // 2)}(x) + {j}")
        body.append("")
        f.write_text("\n".join(body))


def test_cold_index_stays_under_30s(tmp_path: Path) -> None:
    """500-file synthetic repo indexes in well under 30 s cold."""
    from snapctx.api import index_root

    _write_synthetic_repo(tmp_path, n_files=500, symbols_per_file=10)
    t0 = time.monotonic()
    summary = index_root(tmp_path)
    elapsed = time.monotonic() - t0

    # The fixture should produce >2 000 real symbols (function + method + class + constant).
    assert summary["symbols_indexed"] > 2000, summary
    assert elapsed < 30.0, (
        f"cold index regressed: {elapsed:.1f}s > 30 s budget "
        f"(symbols={summary['symbols_indexed']})"
    )


def test_warm_context_under_250ms(tmp_path: Path) -> None:
    """After a warmup call, a hybrid context() returns in under 250 ms."""
    from snapctx.api import context, index_root

    _write_synthetic_repo(tmp_path, n_files=500, symbols_per_file=10)
    index_root(tmp_path)

    # Warmup — loads the fastembed model and primes caches.
    context("helper 0 utilities for perf fixture", root=tmp_path)

    t0 = time.monotonic()
    out = context("helper 0 utilities for perf fixture", root=tmp_path)
    elapsed = time.monotonic() - t0

    assert out["seeds"], "warm context returned empty seeds"
    assert elapsed < 0.250, (
        f"warm context() regressed: {elapsed * 1000:.1f}ms > 250 ms budget"
    )
