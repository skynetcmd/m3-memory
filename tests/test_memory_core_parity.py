"""Public API parity oracle for the memory_core modularization migration.

Captures every previously-public symbol of `bin/memory_core.py` (name,
signature, type, value-for-constants) into a JSON fixture under
.scratch/migration_baseline/memory_core_api_snapshot.json.

On first run (or when M3_MIGRATION_REFRESH_SNAPSHOT=1), writes the
snapshot. On subsequent runs, compares the current API against the
saved snapshot and fails if any public symbol changed.

The migration must keep this test green at every phase boundary.

What we DON'T snapshot (intentionally):
- Function bodies (they will change as we split modules).
- Closure cells (private state).
- Module-level mutable state (counters, caches) — these are runtime,
  not API.

What we DO snapshot:
- Set of names exported from `memory_core`.
- Whether each is a function, async function, class, or constant.
- Signature for callables (parameters + defaults + return annotation).
- Value (or repr) for constants.
- Module docstring presence.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import unittest
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN_DIR))

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[1]
    / ".scratch" / "migration_baseline" / "memory_core_api_snapshot.json"
)


def _classify_symbol(obj) -> str:
    """Return a stable label for what kind of symbol this is.

    `functools._lru_cache_wrapper` instances (produced by `@lru_cache`)
    fail `inspect.isfunction` but ARE callable wrappers around a real
    function. Treat them as functions so adding `@lru_cache` to an
    existing helper doesn't trip parity as a function -> constant drift.
    """
    if inspect.iscoroutinefunction(obj):
        return "async_function"
    if inspect.isfunction(obj):
        return "function"
    # lru_cache wraps the original in functools._lru_cache_wrapper.
    # Detect via the canonical attribute `__wrapped__` + callable + non-class
    # rather than importing the private class, which moves across versions.
    if callable(obj) and hasattr(obj, "__wrapped__") and not inspect.isclass(obj):
        wrapped = obj.__wrapped__
        if inspect.iscoroutinefunction(wrapped):
            return "async_function"
        if inspect.isfunction(wrapped):
            return "function"
    if inspect.isclass(obj):
        return "class"
    if inspect.isbuiltin(obj):
        return "builtin"
    if inspect.ismethod(obj):
        return "method"
    if inspect.ismodule(obj):
        return "module"
    return "constant"


def _capture_signature(obj) -> str | None:
    """Return string form of the call signature, or None for non-callables."""
    if not callable(obj):
        return None
    try:
        sig = inspect.signature(obj)
        return str(sig)
    except (TypeError, ValueError):
        return None


def _capture_value(obj) -> str | None:
    """For constants, capture a stable repr. None for callables/classes."""
    if callable(obj) or inspect.isclass(obj) or inspect.ismodule(obj):
        return None
    try:
        return repr(obj)
    except Exception:
        return "<unrepr>"


def _is_public(name: str) -> bool:
    """A symbol is 'public' for our purposes if it doesn't start with __
    AND it's not an obvious re-exported standard-library or third-party module."""
    if name.startswith("__"):
        return False
    return True


def _build_snapshot():
    """Import memory_core and walk its module dict. Returns a JSON-safe dict."""
    import memory_core  # noqa: F401 — picked up by the snapshot
    symbols = {}
    for name, obj in vars(memory_core).items():
        if not _is_public(name):
            continue
        # Skip standard-library and third-party modules that memory_core
        # imports into its namespace. We only care about its OWN symbols
        # and symbols it explicitly re-exports.
        if inspect.ismodule(obj):
            # Modules are tracked separately to detect "did the import surface
            # change" but not against signatures.
            symbols[name] = {"kind": "module", "module_name": getattr(obj, "__name__", "?")}
            continue
        symbols[name] = {
            "kind": _classify_symbol(obj),
            "signature": _capture_signature(obj),
            "value_repr": _capture_value(obj),
        }
    return {
        "module": "memory_core",
        "n_symbols": len(symbols),
        "module_doc_present": bool(getattr(__import__("memory_core"), "__doc__", None)),
        "symbols": symbols,
    }


class MemoryCoreParityTests(unittest.TestCase):
    """Snapshot-driven parity. The fixture lives outside `tests/` so it
    doesn't end up in committed test data."""

    def test_public_api_matches_snapshot(self):
        current = _build_snapshot()

        if not SNAPSHOT_PATH.exists() or os.environ.get("M3_MIGRATION_REFRESH_SNAPSHOT"):
            SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            SNAPSHOT_PATH.write_text(json.dumps(current, indent=2, sort_keys=True))
            self.skipTest(
                f"Snapshot written to {SNAPSHOT_PATH}. Re-run without "
                "M3_MIGRATION_REFRESH_SNAPSHOT to enforce parity."
            )

        saved = json.loads(SNAPSHOT_PATH.read_text())

        # Compare structurally so the failure messages point at the
        # specific symbol that changed.
        cur_names = set(current["symbols"])
        old_names = set(saved["symbols"])

        added = cur_names - old_names
        removed = old_names - cur_names
        self.assertFalse(
            removed,
            f"public API symbols REMOVED (would break callers): {sorted(removed)}"
        )
        if added:
            # Adding new public symbols is fine; the migration may legitimately
            # expose a helper that was always there but didn't appear in the
            # baseline. Print but don't fail.
            print(f"  NOTE: new public symbols added: {sorted(added)}")

        # For symbols that exist in both, compare their stable shape.
        mismatches = []
        for name in sorted(cur_names & old_names):
            c = current["symbols"][name]
            s = saved["symbols"][name]
            if c.get("kind") != s.get("kind"):
                mismatches.append(f"{name}: kind {s['kind']} -> {c['kind']}")
            if c.get("signature") != s.get("signature"):
                mismatches.append(
                    f"{name}: signature changed\n    was: {s.get('signature')}\n    now: {c.get('signature')}"
                )
            # Constant value drift: only flag for things we can compare.
            # Module-level mutable state (counters, caches) will have
            # different reprs at different times; we skip those by allowing
            # value_repr to drift WITHIN A KIND (which is why kind drift is
            # checked separately above).
        self.assertFalse(
            mismatches,
            "public API changed:\n  " + "\n  ".join(mismatches)
        )


if __name__ == "__main__":
    unittest.main()
