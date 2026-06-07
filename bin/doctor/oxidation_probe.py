"""Oxidation status probe — is the installed `m3_core_rs` present and current?

The Python hot paths (FTS sanitize/compile, vector ops, ranking) route through
the native `m3_core_rs` extension when it exposes the matching function, and
silently fall back to a slower pure-Python body otherwise. That silent fallback
is correct for *un*installed extensions — but it also hides a present-but-STALE
wheel: an old build missing newer functions degrades performance and, worse,
can ship behavior that a later source fix already corrected. A stale wheel
missing `sanitize_fts`/`compile_fts_query` is exactly what masked the FTS
operator-char crash for days (the Python fallback carried the bug while the
fixed Rust sat unbuilt).

This probe makes that state visible instead of silent (DESIGN §3 fail-loud,
§8 performance). It never fails the doctor run — a Python-only deployment is a
legitimate, supported configuration — it only reports, returning 0 always.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("memory.doctor.oxidation_probe")

# Native functions the Python oxidation paths expect to route through. Each is
# a (name, why-it-matters) pair so the report explains the cost of its absence.
# Keep this list curated and load-bearing — it is the contract between the
# Python fallbacks and the shipped wheel, not an exhaustive symbol dump.
_EXPECTED = [
    ("sanitize_fts", "FTS5 query sanitization (search correctness + speed)"),
    ("compile_fts_query", "FTS5 query compilation (hot search path)"),
    ("token_jaccard", "lexical overlap scoring (ranker hot path)"),
    ("token_jaccard_batch", "batched lexical overlap (ranker hot path)"),
    ("rank_hybrid_packed", "packed hybrid ranking (search hot path)"),
    ("cosine_batch_packed", "packed batch cosine (vector search hot path)"),
    ("mmr_rerank_scored_packed", "packed MMR rerank (diversity rerank)"),
    ("scrub", "redaction scrubbing (write boundary)"),
]


def run() -> int:
    """Report m3_core_rs presence and per-function availability. Always 0."""
    print()
    print("=== oxidation status (m3_core_rs native extension) ===")

    try:
        from memory import config
    except Exception as e:  # noqa: BLE001 — probe must never crash the doctor
        print(f"  could not import memory.config: {type(e).__name__}: {e}")
        return 0

    if getattr(config, "_OXIDATION_DISABLED", False):
        print("  status   : disabled via M3_CORE_RS_DISABLE (pure-Python by choice)")
        return 0

    rs = config.m3_core_rs
    if rs is None:
        print("  status   : not installed — pure-Python fallback (supported, slower)")
        print("  hint     : `pip install m3-memory[oxidation]` for native speedups")
        return 0

    try:
        version = getattr(rs, "__version__", "unknown")
        present = [n for n, _ in _EXPECTED if hasattr(rs, n)]
        missing = [(n, why) for n, why in _EXPECTED if not hasattr(rs, n)]
    except Exception as e:  # noqa: BLE001 — a hostile/broken extension object
        print(f"  status   : installed but uninspectable: {type(e).__name__}: {e}")
        return 0

    print(f"  status   : installed (version {version})")
    print(f"  functions: {len(present)}/{len(_EXPECTED)} expected native paths present")

    if not missing:
        print("  result   : current — all expected native paths active")
        return 0

    # Present-but-stale: the wheel is old relative to the Python code's
    # expectations. Loud, actionable, but non-fatal.
    print("  result   : STALE — installed wheel is missing expected functions:")
    for name, why in missing:
        print(f"             - {name}: {why}")
    print("  impact   : those paths silently use the slower Python fallback;")
    print("             a stale wheel can also carry bugs already fixed in source.")
    print("  fix      : rebuild/reinstall m3_core_rs from the current m3-core-rs")
    print("             (e.g. reinstall the matching wheel or `maturin develop`).")
    return 0
