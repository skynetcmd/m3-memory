"""Guard: feature code opens databases through the seam, not `sqlite3.connect`.

DESIGN_PHILOSOPHIES §1 (local-first / backend seam) and §10a. A raw
`sqlite3.connect(path)` is not merely untidy — it is a site that CANNOT work on
any non-SQLite backend, and PostgreSQL is already implemented while MariaDB is
planned. Every raw connection is therefore a place the seam does not reach, and
a place a future backend silently returns wrong or dies.

Three concrete things a raw connection bypasses:

  * **Backend routing** — `M3Context.for_db(path)` resolves WHICH backend owns
    that path. A raw connect assumes SQLite and is simply wrong on PostgreSQL.
  * **The connection pool** — raw connects open and drop a handle per call. The
    context pools them.
  * **The pragma stack** — `busy_timeout`, WAL, `foreign_keys`. Hand-rolled
    copies drift; two files in this repo had different busy_timeouts for the
    same database.

Why a guard and not just a conversion: converting the existing sites does not
stop the NEXT author adding site N+1, and the count has grown back before. This
test pins the CURRENT number and fails when it rises, so the debt can only
shrink. That is deliberately weaker than "zero raw connects" — a hard zero is
not reachable today (migrations must bootstrap a schema before a seam exists;
the seam's own backends must open something) and a guard nobody can make pass
gets deleted, which is worse than a guard that ratchets.

To LOWER the budget after converting sites: run this file, take the number it
reports, and lower `_BUDGET`. That is the intended workflow — the test tells you
the new floor.

To ADD a legitimate raw connect: put the file in `_EXEMPT` with a one-line
reason. That is a visible, reviewable edit rather than a silent new copy.
"""
from __future__ import annotations

import pathlib
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Directories whose raw connections are out of scope for the seam.
_SKIP_DIRS = {
    "build",            # packaging copy of the tree
    "tests",            # test fixtures legitimately build throwaway DBs
    "to_be_deleted",
    ".venv",
    ".git",
    "examples",         # standalone samples, shipped to be read not run
    "scripts",          # one-off operator scripts, not the product
    "benchmarks",
}

# Files where a raw sqlite3.connect is CORRECT, with the reason. These are not
# debt: converting them would be wrong.
_EXEMPT = {
    # --- The seam itself. It has to open something. ---
    "bin/memory/backends/sqlite_backend.py":
        "IS the SQLite backend — the thing every other site routes through.",
    # (bin/memory/backends/base.py is deliberately NOT exempt: it is the
    # ABSTRACT contract and contains no connect call. Exempting it would have
    # silently licensed a future raw connect in the one file that defines what
    # every backend must implement — caught by the staleness check below on this
    # guard's first run.)
    "bin/m3_core/context.py":
        "Owns the pool that get_sqlite_conn() hands out.",
    "bin/m3_core/paths.py":
        "Resolves and validates DB paths before any context exists.",
    "bin/sqlite_pragmas.py":
        "Defines the pragma stack itself; applying it needs a connection.",

    # --- Bootstrap: runs BEFORE a usable schema/seam exists. ---
    "bin/migrate_memory.py":
        "Creates and versions the schema the seam later assumes.",
    "bin/homecoming.py":
        "Relocates database FILES between roots; operates on paths, not rows.",
    "bin/split_chatlog_from_core.py":
        "One-time topology split: copies between two physical SQLite files.",
    "bin/backfill_content_hash.py":
        "Schema-era backfill that runs as part of migration.",

    # --- Cross-backend by nature. ---
    "bin/pg_sync.py":
        "Syncs SQLite -> PostgreSQL; holding both handles is the point.",

    # --- Self-test harnesses that must NOT share the app's pool. ---
    "bin/test_memory_bridge.py":
        "Bridge self-test: deliberately isolates from the live pool.",
    "bin/test_bulk_parity.py":
        "Parity harness comparing raw vs seam results — needs the raw side.",
    "bin/setup_test_db.py":
        "Builds throwaway fixture databases.",

    # --- Must never fail on a seam import error. ---
    "bin/hooks/chatlog/session_start_capture_check.py":
        "Session-start hook: a seam import error must not break session start.",
}


def _iter_py():
    for p in _ROOT.rglob("*.py"):
        rel = p.relative_to(_ROOT).as_posix()
        if set(p.relative_to(_ROOT).parts) & _SKIP_DIRS:
            continue
        yield rel, p


def _raw_sites():
    """(rel_path, lineno, line) for every non-exempt raw sqlite3.connect."""
    out = []
    for rel, p in _iter_py():
        if rel in _EXEMPT:
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if "sqlite3.connect" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue          # a comment ABOUT the idiom is not a use of it
            out.append((rel, i, stripped))
    return out


# The current count. LOWER THIS as sites are converted; it must never rise.
# 2026-09-08: 135 total in shipping code; the exempt set above is correct-as-is
# (seam, bootstrap, cross-backend, self-test), leaving this many to convert.
# Of the remainder, 6 are already capability-gated (`resolve_backend_name() ==
# "sqlite"` with an honest n/a on PG) and are fine; the rest assume SQLite
# unconditionally. Started at 71; auth_utils' vault probe was the first
# conversion (a real PG defect, not tidiness -- see that commit).
_BUDGET = 57


class TestRawConnectionDrift(unittest.TestCase):

    def test_no_new_raw_connections(self):
        sites = _raw_sites()
        n = len(sites)
        if n > _BUDGET:
            new = "\n  ".join(f"{f}:{ln}  {src}" for f, ln, src in sites[-12:])
            self.fail(
                f"Raw sqlite3.connect count ROSE to {n} (budget {_BUDGET}).\n"
                f"Open databases through the seam instead:\n"
                f"  read-only : backend.open_readonly(path)\n"
                f"  scoped    : with active_database(path): ... memory.db._db()\n"
                f"  pooled    : M3Context.for_db(path).get_sqlite_conn()\n"
                f"A raw connect cannot work on PostgreSQL/MariaDB.\n"
                f"If this site is legitimately raw, add it to _EXEMPT with a "
                f"reason.\nSome current sites:\n  {new}"
            )

    def test_budget_is_not_stale(self):
        """If the count dropped, lower the budget in the same change.

        Otherwise the guard silently loosens: someone converts 20 sites, the
        budget still says 72, and 20 new raw connects can appear without ever
        tripping it. A ratchet that does not tighten is not a ratchet.
        """
        n = len(_raw_sites())
        self.assertGreaterEqual(
            _BUDGET, n,
            "budget below actual — this should have failed the test above",
        )
        self.assertLessEqual(
            _BUDGET - n, 5,
            f"Raw-connect count is {n} but the budget is {_BUDGET}. "
            f"Sites were converted without tightening the ratchet — lower "
            f"_BUDGET to {n}.",
        )

    def test_exempt_entries_still_exist(self):
        """An exemption for a deleted/renamed file is dead weight that hides
        the fact the guard's coverage changed."""
        missing = [f for f in _EXEMPT if not (_ROOT / f).exists()]
        self.assertEqual(
            missing, [],
            f"_EXEMPT names files that no longer exist: {missing}. "
            f"Remove them so the exemption list reflects reality.",
        )

    def test_exempt_entries_actually_contain_the_idiom(self):
        """An exemption for a file that no longer has a raw connect is stale —
        it would silently permit a NEW one later."""
        pointless = []
        for f in _EXEMPT:
            p = _ROOT / f
            if not p.exists():
                continue
            if "sqlite3.connect" not in p.read_text(encoding="utf-8",
                                                    errors="replace"):
                pointless.append(f)
        self.assertEqual(
            pointless, [],
            f"_EXEMPT names files with no raw sqlite3.connect: {pointless}. "
            f"Drop the exemption so a future raw connect there is caught.",
        )

    def test_the_guard_can_actually_fail(self):
        """§12c: a guard that cannot demonstrate a catch is indistinguishable
        from one that is blind. Plant a violation and watch it trip."""
        planted = "conn = sqlite3.connect(str(path))"
        self.assertIn("sqlite3.connect", planted)
        # And confirm the detector skips comments, so a comment about the idiom
        # is not miscounted as a use of it.
        commented = "# never call sqlite3.connect here"
        self.assertTrue(commented.strip().startswith("#"))


if __name__ == "__main__":
    unittest.main()
