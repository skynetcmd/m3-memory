"""Storage-backend abstraction (the pluggable SQL storage seam).

This package is a *capability seam* — a narrow protocol describing only the
storage operations the memory core genuinely needs to vary by backend. It does
NOT abstract SQL generically and it is NOT an ORM. The failure mode here is a fat
abstraction; the antidote is a narrow capability interface with native
per-backend implementations and accelerators chosen behind runtime probes.

Design of record: DESIGN_PHILOSOPHIES §1 (L1 SQLite is the only required store;
L2 PostgreSQL is opt-in — as a first-class primary backend for shared/high-
concurrency use, or as a warehouse sync tier; same code path laptop <->
air-gapped enclave) and §2 (modularity, shim-preserved identity). The m3 memory
directive c4e4a145 carries the full rationale.

Shipped backends: ``sqlite`` (default; delegates to the proven `M3Context`/
`_db()` machinery) and ``postgres`` (selected via `M3_DB_BACKEND=postgres`).
Both are registered via `@register_backend` and resolved through the registry —
adding another SQL backend (e.g. MariaDB) is one `<name>_backend.py` with a
co-located `Dialect` subclass plus an allow-list entry, no edits to the shared
modules. The seam is SQL/DB-API only; a document store (MongoDB) does not fit and
is deliberately out of scope. See docs/EXTENDING.md (Recipe 1).

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
# NOTE: this re-export binds the package attribute ``memory.backends.dialect`` to
# the ACCESSOR FUNCTION, shadowing the same-named submodule as a PACKAGE ATTRIBUTE
# only. The submodule stays fully reachable by its qualified name
# (``from memory.backends.dialect import ...``), which is how all module-level
# imports reach it — verified repo-wide. Do NOT write ``from memory.backends import
# dialect`` expecting the MODULE; you get the function. Import the accessor as
# ``from memory.backends import dialect`` (function) and the module symbols as
# ``from memory.backends.dialect import dialect_for, chatlog_table, ...``.
from .selector import (
    active_backend,
    dialect,
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
    "dialect",
    "require_sqlite_backend",
    "resolve_backend_name",
]
