"""sqlite_pragmas.py — centralised SQLite pragma stack for all M3 databases.

WHY: Long-running write workloads can grow the WAL file without bound when
wal_autocheckpoint and journal_size_limit are unset (SQLite defaults: 1000
pages autocheckpoint in PASSIVE mode, no journal size cap). Under concurrent
readers the passive checkpoint is busy-failed; without journal_size_limit
the WAL is never truncated even after a successful checkpoint. This module
provides a single source of truth so every DB connection gets the same
WAL-bounding pragma stack regardless of which tool opens it.

Usage::

    import sqlite3
    from sqlite_pragmas import apply_pragmas, profile_for_db

    conn = sqlite3.connect(db_path)
    apply_pragmas(conn, profile_for_db(db_path))

Supports an optional ``overrides`` dict for workload-specific tuning::

    apply_pragmas(conn, "production", overrides={"cache_size": -131072})

Checkpoint helpers::

    from sqlite_pragmas import checkpoint_passive, checkpoint_truncate
    checkpoint_passive(conn)   # periodic, yields to readers
    checkpoint_truncate(conn)  # at job end — flushes WAL to zero
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Universal pragmas applied to every connection regardless of profile.
# Order is load-bearing: journal_mode must precede wal_autocheckpoint.
# ---------------------------------------------------------------------------
_COMMON_PRAGMAS: list[str] = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 30000",
]

# ---------------------------------------------------------------------------
# Per-profile tuning knobs.  All values are in the units SQLite expects.
#   cache_size   — negative = KiB  (e.g. -65536 = 64 MiB)
#   mmap_size    — bytes
#   wal_autocheckpoint — pages (~4 KiB each)
#   journal_size_limit — bytes
# ---------------------------------------------------------------------------
PROFILES: dict[str, dict[str, int]] = {
    # production — agent_memory.db and any general-purpose DB.
    # Conservative wal_autocheckpoint (2000 pages ≈ 8 MiB) keeps WAL tight
    # for online workloads. journal_size_limit=64 MiB is the hard ceiling.
    # cache_size and mmap_size match existing m3_sdk.py values exactly —
    # no steady-state regression.
    "production": {
        "wal_autocheckpoint": 2000,
        "journal_size_limit": 67108864,    # 64 MiB
        "cache_size": -65536,              # 64 MiB (kept at m3_sdk default)
        "mmap_size": 536870912,            # 512 MiB (kept at m3_sdk default)
    },
    # chatlog — agent_chatlog.db.
    # Values are bit-for-bit identical to the chatlog_config.py pragma block
    # that shipped before this module existed; behaviour is unchanged.
    "chatlog": {
        "wal_autocheckpoint": 2000,
        "journal_size_limit": 67108864,    # 64 MiB
        "cache_size": -131072,             # 128 MiB  (matches chatlog_config.py)
        "mmap_size": 1073741824,           # 1 GiB    (matches chatlog_config.py)
    },
    # bench — large write-heavy DBs (lme_m.db, agent_test_bench.db, *_bench.db).
    # Higher wal_autocheckpoint (10000 pages ≈ 40 MiB) amortises checkpoint
    # cost across bulk-insert commits. journal_size_limit=256 MiB prevents the
    # runaway-WAL scenario. Large mmap (8 GiB) lets the OS file cache do the
    # heavy lifting on sequential scans.
    "bench": {
        "wal_autocheckpoint": 10000,
        "journal_size_limit": 268435456,   # 256 MiB
        "cache_size": -65536,              # 64 MiB  (small; OS cache dominates)
        "mmap_size": 8589934592,           # 8 GiB
    },
}

# Allow runtime override of mmap_size via environment variable (same pattern
# as M3_CONTEXT_CACHE_SIZE in m3_sdk.py).
_ENV_MMAP = os.environ.get("M3_SQLITE_MMAP_SIZE")


def profile_for_db(db_path: str | Path) -> str:
    """Return the pragma profile name for a given DB path.

    Matching rules (checked in order against the basename):

    - ``*_chatlog.db`` or ``agent_chatlog.db`` → ``"chatlog"``
    - ``lme_m.db``, ``agent_test_bench.db``, or ``*_bench.db`` → ``"bench"``
    - Anything else → ``"production"``
    """
    name = Path(db_path).name.lower()
    if name.endswith("_chatlog.db") or name == "agent_chatlog.db":
        return "chatlog"
    if name in ("lme_m.db", "agent_test_bench.db") or name.endswith("_bench.db"):
        return "bench"
    return "production"


def apply_pragmas(
    conn: sqlite3.Connection,
    profile: str = "production",
    overrides: Optional[dict[str, Any]] = None,
) -> None:
    """Apply the common pragma stack plus the named profile to *conn*.

    Args:
        conn:      An open ``sqlite3.Connection``.
        profile:   One of ``"production"``, ``"chatlog"``, or ``"bench"``.
                   Defaults to ``"production"``.
        overrides: Optional dict of pragma name → value that supersedes the
                   profile values.  Useful for workload-specific tuning
                   without adding a new named profile.

    Raises:
        KeyError:   Unknown profile name.
        RuntimeError: ``journal_mode`` did not land as WAL (network filesystem
                      or other platform limitation — crash early rather than
                      silently letting the WAL grow on a DELETE-mode journal).
    """
    if profile not in PROFILES:
        raise KeyError(f"Unknown pragma profile {profile!r}; choose from {list(PROFILES)}")

    # Apply universal pragmas first; journal_mode must come before wal_autocheckpoint.
    for stmt in _COMMON_PRAGMAS:
        conn.execute(stmt)

    # Verify journal_mode actually landed — some network filesystems silently
    # downgrade WAL → DELETE.
    jm = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    if jm != "wal":
        raise RuntimeError(
            f"journal_mode is {jm!r} after PRAGMA journal_mode=WAL — "
            "is the database on a network filesystem that does not support WAL?"
        )

    # Merge profile values with optional overrides.
    knobs: dict[str, Any] = dict(PROFILES[profile])
    if overrides:
        knobs.update(overrides)

    # Honour the environment-level mmap override (M3_SQLITE_MMAP_SIZE).
    if _ENV_MMAP is not None:
        try:
            knobs["mmap_size"] = int(_ENV_MMAP)
        except ValueError:
            pass

    # Apply per-profile knobs.
    for pragma, value in knobs.items():
        conn.execute(f"PRAGMA {pragma} = {value}")

    # Try to load sqlite-vec dynamically if available
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (ImportError, sqlite3.OperationalError, AttributeError):
        pass


def checkpoint_passive(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Run a PASSIVE checkpoint.

    PASSIVE yields to active readers — it never blocks. Use this inside
    long-running writers (every N rows or every N seconds) to keep the WAL
    from accumulating between commits.

    Returns:
        ``(busy, log_pages, checkpointed)`` as returned by SQLite.
    """
    row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    return (row[0], row[1], row[2])


def checkpoint_truncate(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Run a TRUNCATE checkpoint at job end.

    Flushes all WAL frames to the main database file and truncates the WAL
    file to zero bytes.  This is the legitimate replacement for manually
    deleting a ``*-wal`` file — safe because SQLite has flushed everything
    before truncating.

    TRUNCATE blocks if other readers hold an open read transaction.  Only
    call at clean job end when you know no concurrent readers are active.

    Returns:
        ``(busy, log_pages, checkpointed)`` as returned by SQLite.
    """
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return (row[0], row[1], row[2])
