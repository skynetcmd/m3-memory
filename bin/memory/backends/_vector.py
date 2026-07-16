"""Shared vector-search scoring — the DB-blind half of `vector_search`.

Both backends fetch candidate ``(id, embedding_blob)`` rows in their own SQL
dialect, then hand the packed float32 blobs here for identical Rust-cosine
scoring and top-N selection. Keeping the scoring in ONE place guarantees the two
backends produce the same VectorHit ordering for the same rows — the seam
invariant — with only the fetch differing.

Cycle-break (§2): the Rust-cosine helper is imported lazily inside the function.
"""
from __future__ import annotations

from .base import VectorHit


def score_and_rank(
    query_vector: "list[float]",
    rows: "list[tuple[str, object]]",
    dim: int,
    limit: int,
) -> "list[VectorHit]":
    """Score packed-blob candidate rows against the query and return top-N.

    ``rows`` is ``[(memory_id, embedding_blob), ...]`` where ``embedding_blob``
    is the raw packed float32 bytes as stored (SQLite BLOB / Postgres BYTEA — the
    bytes are identical). Uses the same ``_cosine_batch_packed`` Rust path the
    existing search uses, so scores match today's behavior exactly. HIGHER score
    = better (cosine similarity); ties keep fetch order (stable sort). A blob of
    the wrong byte length scores 0.0 (handled inside ``_cosine_batch_packed``).
    """
    if not rows:
        return []
    # Lazy import: util owns the canonical Rust/numpy/pure-Python cascade.
    from ..util import _cosine_batch_packed

    ids = [r[0] for r in rows]
    blobs = [r[1] for r in rows]
    # psycopg returns BYTEA as `memoryview`/`bytes`; _cosine_batch_packed expects
    # bytes-like. memoryview is bytes-like already; normalize just in case.
    blobs = [bytes(b) if isinstance(b, memoryview) else b for b in blobs]
    scores = _cosine_batch_packed(query_vector, blobs, dim)
    hits = [VectorHit(memory_id=i, score=float(s)) for i, s in zip(ids, scores)]
    # Highest cosine first; stable so equal scores keep candidate order.
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
