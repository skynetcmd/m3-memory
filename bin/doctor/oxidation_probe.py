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


def run(brief: bool = False) -> int:
    """Report m3_core_rs presence and per-function availability. Always 0.

    brief=True emits a single line for `m3 doctor --brief`."""
    def _b(line: str) -> None:  # brief-only one-liner
        if brief:
            print(line)

    if not brief:
        print()
        print("=== oxidation status (m3_core_rs native extension) ===")

    try:
        from memory import config
    except Exception as e:  # noqa: BLE001 — probe must never crash the doctor
        # Name WHERE it failed, not just that it did. `memory/__init__` imports
        # a dozen submodules, so the statement that raised is rarely the module
        # at fault: on 2026-10-02 this printed "could not import memory.config:
        # AttributeError: 'object' object has no attribute 'CircuitBreaker'",
        # which is accurate about what was OBSERVED and says nothing about the
        # submodule whose import-time code actually raised (memory/embed.py).
        tb = e.__traceback__
        where = ""
        while tb is not None:  # walk to the deepest frame
            f = tb.tb_frame.f_code.co_filename
            where = f"{f}:{tb.tb_lineno}"
            tb = tb.tb_next
        if brief:
            _b("oxidation: unknown (memory.config not importable)")
        else:
            print(f"  could not import memory.config: {type(e).__name__}: {e}")
            if where:
                print(f"  raised at : {where}")
                print("  note     : importing `memory` pulls in its submodules, "
                      "so the file above is the one to look at, not config.py")
        return 0

    if getattr(config, "_OXIDATION_DISABLED", False):
        _b("oxidation: disabled (pure-Python by choice)")
        if not brief:
            print("  status   : disabled via M3_CORE_RS_DISABLE (pure-Python by choice)")
        return 0

    rs = config.m3_core_rs
    if rs is None:
        _b("⚠️  oxidation: not installed (pure-Python fallback, slower)")
        if not brief:
            print("  status   : not installed — pure-Python fallback (supported, slower)")
            print("  hint     : `pip install m3-memory[oxidation]` for native speedups")
        return 0

    try:
        version = getattr(rs, "__version__", "unknown")
        present = [n for n, _ in _EXPECTED if hasattr(rs, n)]
        missing = [(n, why) for n, why in _EXPECTED if not hasattr(rs, n)]
    except Exception as e:  # noqa: BLE001 — a hostile/broken extension object
        _b("⚠️  oxidation: installed but uninspectable")
        if not brief:
            print(f"  status   : installed but uninspectable: {type(e).__name__}: {e}")
        return 0

    if not brief:
        print(f"  status   : installed (version {version})")
        print(f"  functions: {len(present)}/{len(_EXPECTED)} expected native paths present")

    # Version staleness — independent of function presence. A wheel can expose
    # every expected function yet still be an OLD build (carrying bugs already
    # fixed in a newer release). Compare the installed __version__ against the
    # version the installer targets. Best-effort: an unknown/unparseable version
    # is reported but not treated as definitively stale.
    version_stale = False
    try:
        from m3_memory.rust_core_install import (
            M3_CORE_RS_VERSION,
            _parse_version,
        )
        if version and version != "unknown":
            if _parse_version(version) < _parse_version(M3_CORE_RS_VERSION):
                version_stale = True
                if not brief:
                    print(f"  version  : STALE — installed {version} < expected "
                          f"{M3_CORE_RS_VERSION}")
        elif not brief:
            print("  version  : installed wheel does not report a version "
                  "(cannot confirm currency)")
    except Exception as e:  # noqa: BLE001 — probe must never crash the doctor
        logger.debug("version-staleness check skipped: %s", e)

    if not missing and not version_stale:
        _b(f"✅ oxidation: current ({len(present)}/{len(_EXPECTED)} native paths)")
        if not brief:
            print("  result   : current — all expected native paths active")
        return 0

    if brief:
        # Name the COMMAND, not just the verb. `m3 doctor` without --verbose is
        # the form users actually run, and "— reinstall" told them a state to
        # change with no way to change it; the remediation only appeared in the
        # verbose block below, which someone already unsure of the problem has
        # no reason to go looking for. A stale core is reached by an ordinary
        # upgrade — the native wheel is a SEPARATE distribution, so
        # `pipx upgrade m3-memory` advances the Python code and leaves the
        # extension behind (observed 2026-09-30: m3-memory 2026.9.21.0 against
        # m3_core_rs 3.9.7, expected 3.10.1) — so this line is on a common path,
        # not an edge case. One line, because brief mode must stay one line.
        print(f"⚠️  oxidation: STALE ({len(present)}/{len(_EXPECTED)} paths"
              f"{', version behind' if version_stale else ''})"
              " — fix: `m3 embedder install-gpu`")
        return 0

    # Present-but-stale: the wheel is old relative to the Python code's
    # expectations (missing functions) and/or behind the target version. Loud,
    # actionable, but non-fatal.
    print("  result   : STALE — reinstall recommended")
    if missing:
        print("             missing expected functions:")
        for name, why in missing:
            print(f"             - {name}: {why}")
    if version_stale:
        print(f"             installed version {version} is behind the target "
              f"{M3_CORE_RS_VERSION}")
    print("  impact   : stale paths silently use the slower Python fallback;")
    print("             a stale wheel can also carry bugs already fixed in source.")
    print("  fix      : `m3 embedder install-gpu` (reinstalls the current wheel)")
    return 0
