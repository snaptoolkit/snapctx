"""Embedding model + helpers.

Uses ``fastembed`` (ONNX runtime, no torch) with ``BAAI/bge-small-en-v1.5``
(384-dim, ~30 MB). The model is loaded lazily and cached for the process.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Sequence

import numpy as np

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_DIM = 384


@lru_cache(maxsize=1)
def _embedder():
    """Return a cached TextEmbedding instance."""
    from fastembed import TextEmbedding  # imported lazily so vector code is opt-in at runtime

    return TextEmbedding(model_name=_MODEL_NAME)


def dim() -> int:
    return _DIM


def embed_texts(texts: Sequence[str]) -> np.ndarray:
    """Encode ``texts`` into an L2-normalized ``(N, dim)`` float32 matrix.

    bge-small already returns normalized vectors, but we re-normalize defensively
    so dot product = cosine similarity always holds.
    """
    if not texts:
        return np.empty((0, _DIM), dtype=np.float32)
    vectors = list(_embedder().embed(list(texts)))
    arr = np.vstack(vectors).astype(np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def symbol_text_for_embedding(qname: str, signature: str, docstring: str | None) -> str:
    """Build the text that represents a symbol for retrieval.

    Order matters — bge's retrieval prefers the distinctive signal first. We lead
    with the qname (natural language identifiers split by dot/colon), then the
    rendered signature (types, parameter names), then a truncated docstring.
    """
    parts = [qname.replace(".", " ").replace(":", " "), signature]
    if docstring:
        parts.append(docstring[:512])
    return " · ".join(parts)
