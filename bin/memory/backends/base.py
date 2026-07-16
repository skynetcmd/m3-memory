"""The `StorageBackend` protocol and capability model.

This is the *narrow* seam: only the operations that genuinely differ between
SQLite and PostgreSQL live here. Everything else (all `m3_core_rs` pure-compute:
cosine, rank, MMR, embedding, governor, circuit-breaker, hashing) stays
backend-blind and is not represented in this protocol.

Invariant (directive c4e4a145): `keyword_search` and `vector_search` MUST return
the same shape — ``list[tuple[str, float]]`` of ``(memory_id, score)`` — on every
backend, regardless of which engine or accelerator produced it. The accelerator
is an implementation detail chosen behind a capability probe; it is never exposed
upstream. This is what lets a search caller be identical on both backends.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

# The set of backend identities Phase 0 knows about. "postgres" is declared here
# so the selector and config can name it, but no implementation ships until a
# later phase — `active_backend()` raises a clear error if it is selected now.
BackendName = Literal["sqlite", "postgres"]


@dataclass(frozen=True)
class Capabilities:
    """What a *connected* backend can do, discovered at connect time.

    Capabilities gate the *accelerator* chosen for a query; they never change the
    result shape. Every backend has an add-on-free baseline (Rust cosine for
    vectors, native full-text for keyword) that is always correct when an
    accelerator is absent — so an empty accelerator set still yields a fully
    working store. Generalizes the proven ``_detect_sqlite_vec`` probe.
    """

    backend: BackendName
    # Keyword-search engine that is natively available on this backend.
    keyword: Literal["fts5", "tsvector"] = "fts5"
    # Optional vector accelerator, if probed present (else baseline Rust cosine).
    vector_accelerator: Literal["none", "sqlite_vec", "pgvector"] = "none"
    # Optional keyword accelerator, if probed present (else the native `keyword`).
    keyword_accelerator: Literal["none", "pg_search"] = "none"
    # Free-form probe results for accelerators not yet modelled (pg_trgm, ...).
    extra: frozenset[str] = field(default_factory=frozenset)

    def has(self, name: str) -> bool:
        """True if `name` is an available accelerator/feature on this backend."""
        return (
            name in self.extra
            or name == self.vector_accelerator
            or name == self.keyword_accelerator
            or name == self.keyword
        )


@runtime_checkable
class StorageBackend(Protocol):
    """The capability seam every storage engine implements.

    Deliberately small. If an operation does not differ between SQLite and
    PostgreSQL, it does NOT belong here — keep it in the shared code above the
    seam. Phase 0 defines only the connection + introspection + capability
    surface, which the SQLite backend satisfies by delegating to the existing
    `M3Context`. `keyword_search` / `vector_search` are declared as the seam's
    intended shape but are routed through in a later phase, not this one, so the
    hot path stays byte-identical while the scaffold lands.
    """

    name: BackendName

    def capabilities(self) -> Capabilities:
        """Return the capabilities discovered for this backend's connection."""
        ...

    def connection(self) -> AbstractContextManager:
        """A read/write connection context manager.

        On SQLite this is the pooled `sqlite3.Connection` used today; on
        PostgreSQL a pooled psycopg connection. Callers use it exactly as they
        use `_db()` today: ``with backend.connection() as conn: ...``.
        """
        ...

    def placeholder(self, n: int = 1) -> str:
        """Render `n` positional bind placeholders for this backend's driver.

        SQLite uses ``?``; psycopg uses ``%s``. Returns a comma-joined run, e.g.
        ``placeholder(3) -> "?, ?, ?"``. This replaces the scattered
        ``",".join("?" * n)`` idioms with one backend-aware helper (§6 port).
        """
        ...
