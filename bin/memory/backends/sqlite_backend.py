"""SQLite `StorageBackend` — delegates to the existing, proven machinery.

Phase 0 deliberately makes this a THIN adapter over `M3Context` and the current
`_db()` connection flow. It introduces no new SQLite behavior: the pool, WAL
pragmas, busy-timeout, transaction discipline, and lazy-init are all exactly
today's code, reached through the same `M3Context`. The point of Phase 0 is to
prove the seam's shape against the working backend with zero behavior change
before any PostgreSQL code exists.

Cycle-break (§2): resolve `M3Context` lazily; do not top-level-import
`memory_core`.
"""
from __future__ import annotations

from contextlib import AbstractContextManager

from .base import BackendName, Capabilities
from .dialect import SQLITE, Dialect


class SqliteBackend:
    """Adapter exposing the current SQLite path through the `StorageBackend` seam."""

    name: BackendName = "sqlite"

    def dialect(self) -> Dialect:
        """The SQLite SQL dialect (qmark placeholders, PRAGMA introspection)."""
        return SQLITE

    def capabilities(self) -> Capabilities:
        """Probe optional accelerators; baseline (FTS5 + Rust cosine) always holds.

        The sqlite-vec probe reuses the existing detector so behavior matches the
        current search path exactly. Absence of sqlite-vec is not an error — the
        baseline Rust BLOB cosine is always correct.
        """
        vector_accel = "none"
        try:
            # Lazy import: search_routing already owns the canonical probe
            # (SELECT vec_version()); reuse it rather than re-implementing.
            from .. import db as _db_mod
            from ..search_routing import _detect_sqlite_vec

            with _db_mod._db() as conn:
                if _detect_sqlite_vec(conn):
                    vector_accel = "sqlite_vec"
        except Exception:
            # Any probe failure -> stay on the add-on-free baseline. Never raise
            # from capability discovery; a missing accelerator is normal.
            vector_accel = "none"
        return Capabilities(
            backend="sqlite",
            keyword="fts5",
            vector_accelerator=vector_accel,  # type: ignore[arg-type]
        )

    def connection(self) -> AbstractContextManager:
        """The pooled SQLite connection context manager used everywhere today.

        Delegates to `memory.db._db()`, so this is byte-for-byte the current
        behavior: same pool, same pragmas, same commit/rollback discipline.
        """
        from .. import db as _db_mod

        return _db_mod._db()

    def placeholder(self, n: int = 1) -> str:
        """SQLite qmark placeholders: ``placeholder(3) -> "?, ?, ?"``."""
        if n < 1:
            raise ValueError(f"placeholder count must be >= 1, got {n}")
        return ", ".join(["?"] * n)
