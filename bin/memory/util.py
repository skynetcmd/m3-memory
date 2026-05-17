"""Pure utility helpers for the m3-memory core.

Stateless helpers shared across modules. Phase 1 added `sha256_hex` only.
Phase 4 expanded scope to include `_batch_cosine` because the write-path
(`_check_contradictions`) and the search-path (`_cosine_batch_packed`)
both need it — and putting it in `memory.search` would create a
write -> search dependency that's the wrong direction.

This module imports from external libs (`embedding_utils`, optional
numpy) but NEVER from other m3-memory modules — keeps the dependency
graph clean and circular-import-free.
"""
from __future__ import annotations

from crypto_provider import get_sha256 as _sha256_hex_py
from embedding_utils import (
    batch_cosine as _batch_cosine_py,
    unpack_many as _unpack_many,
    HAS_NUMPY as _HAS_NUMPY,
)

if _HAS_NUMPY:
    import numpy as _np  # type: ignore
else:
    _np = None  # type: ignore

from . import config

__all__ = ["sha256_hex", "_batch_cosine"]


def sha256_hex(data: bytes) -> str:
    """SHA-256 hex digest.

    Deliberately NOT routed through m3_core_rs. Benchmarking (tests/
    bench_oxidation.py) showed the Rust path is slower for every realistic
    input size: hashlib is already OpenSSL C with SHA-NI, and the PyO3 FFI
    crossing adds fixed overhead that the hashing work never amortizes on
    turn-sized content (~bytes to low KB). ring and hashlib only tie above
    ~64KB. FIPS is unaffected — when CPython is built against a FIPS-validated
    OpenSSL, hashlib.sha256 IS the validated path; the ring-based m3-hash
    crate stays FIPS-gated in the workspace for any Rust-side hashing.
    """
    return _sha256_hex_py(data)


def _batch_cosine(query, matrix) -> list[float]:
    """Cosine of one query against many vectors.

    Fast paths, in order:
      1. ndarray input -> hand to `embedding_utils.batch_cosine` (numpy gemv).
      2. Rust core + homogeneous list-of-lists -> `cosine_batch` (rayon).
      3. Python+numpy fallback.

    The previous always-O(N) homogeneity scan is skipped on the ndarray path
    where homogeneity is guaranteed by the array shape.

    Used by both the write path (`_check_contradictions`) and the search path
    (`_cosine_batch_packed` falls through to this for the non-Rust branch).
    """
    if matrix is None:
        return []
    # ndarray fast path — no per-row dim check, numpy does gemv in one shot.
    if _HAS_NUMPY and isinstance(matrix, _np.ndarray):
        return _batch_cosine_py(query, matrix)  # routes to ndarray branch inside
    if not matrix:
        return []
    if config.m3_core_rs is not None:
        q_dim = len(query)
        if all(len(v) == q_dim for v in matrix):
            return config.m3_core_rs.cosine_batch(query, matrix)
    return _batch_cosine_py(query, matrix)
