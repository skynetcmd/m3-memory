"""Storage-backend abstraction (Phase 0 of PostgreSQL-as-primary).

This package introduces a *capability seam* — a narrow protocol describing only
the storage operations the memory core genuinely needs to vary by backend. It
does NOT abstract SQL generically and it is NOT an ORM. The failure mode here is
a fat abstraction; the antidote is a narrow capability interface with native
per-backend implementations and accelerators chosen behind runtime probes.

Design of record: DESIGN_PHILOSOPHIES §1 (L1 SQLite is the only required store;
L2 PostgreSQL is opt-in; same code path laptop <-> air-gapped enclave) and §2
(modularity, shim-preserved identity). The m3 memory directive c4e4a145 carries
the full rationale.

Phase 0 scope (this commit): the protocol, the backend selector
(`M3_DB_BACKEND`), and a SQLite backend that DELEGATES to the existing, proven
`M3Context`/`_db()` machinery — i.e. a pure refactor with zero behavior change.
No PostgreSQL implementation exists yet; the hot path is untouched. Subsequent
phases add the PostgreSQL backend and progressively route call sites through the
seam, each guarded by the cross-backend parity suite.

Cycle-break rule (§2): modules in this package must NOT top-level-import
``memory_core``. Resolve any core callback lazily inside a function body.
"""
from __future__ import annotations

from .base import (
    BackendName,
    Capabilities,
    KeywordHit,
    StorageBackend,
    VectorHit,
)
from .dialect import (
    chatlog_table,
    chatlog_table_for,
)
from .selector import (
    active_backend,
    require_sqlite_backend,
    resolve_backend_name,
)

__all__ = [
    "BackendName",
    "Capabilities",
    "KeywordHit",
    "StorageBackend",
    "VectorHit",
    "active_backend",
    "chatlog_table",
    "chatlog_table_for",
    "require_sqlite_backend",
    "resolve_backend_name",
]
