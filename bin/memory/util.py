"""Pure utility helpers for the m3-memory core.

This module is intentionally tiny in Phase 1. It exists as a home for
truly stateless helpers that:

  - Have no dependencies on other m3-memory modules (`.config`,
    `.db`, `.embed`, etc.).
  - Don't read mutable global state.
  - Can be unit-tested without any I/O or DB setup.

Larger helper bundles (FTS5 sanitization, title-overlap math, content-hash
+ pack/unpack, regex compendia) move in later phases as their owning
modules come online. See `docs/MEMORY_CORE_MODULARIZATION.md`.
"""
from __future__ import annotations

from crypto_provider import get_sha256 as _sha256_hex_py

__all__ = ["sha256_hex"]


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
