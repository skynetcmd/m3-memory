"""The `StorageBackend` protocol and capability model.

This is the *narrow* seam: only the operations that genuinely differ between
SQLite and PostgreSQL live here. Everything else (all `m3_core_rs` pure-compute:
cosine, rank, MMR, embedding, governor, circuit-breaker, hashing) stays
backend-blind and is not represented in this protocol.

SCOPE / BOUNDARY (important — do not overstate this seam):
This is a **SQL / DB-API seam**, not a universal storage seam. It targets
relational engines reached through a DB-API-2.0-style ``connection().execute(sql,
params)`` surface — SQLite and PostgreSQL today, and **MariaDB** would fit as a
new backend by adding a `Dialect` subclass + engine plumbing (small). The seam
deliberately assumes SQL: the `Dialect` helpers emit SQL fragments (placeholders,
ON CONFLICT, json_extract, NOW()), `connection()` yields a cursor-bearing DB-API
connection, and `keyword_search`/`vector_search` accept a `tenancy_sql` string
fragment. A **document store (e.g. MongoDB) does NOT fit this Protocol** and must
NOT be forced into it — that would be the fat-abstraction failure mode
(DESIGN_PHILOSOPHIES §1). A document backend would require lifting the seam above
SQL (methods like ``upsert_item(dict)`` / ``search(query) -> hits`` with no SQL in
any signature) — a separate, larger design, explicitly out of scope here.

SCALING is likewise a layer ABOVE this seam, not a backend variant: m3 scales by
running one self-contained store PER TENANT (per clinic/facility) with a tenant
router (clinic -> DSN) and a cross-tenant fan-out layer for the rare cross-clinic
query — engine-agnostic orchestration that sits over independent per-tenant
backends. It is NOT distributed sharding of a single logical store, and it does
not constrain (or belong in) this seam. See memory m3-scale-architecture-per-clinic.

Invariant (directive c4e4a145): `keyword_search` and `vector_search` MUST return
the same shape — ``list[tuple[str, float]]`` of ``(memory_id, score)`` — on every
backend, regardless of which engine or accelerator produced it. The accelerator
is an implementation detail chosen behind a capability probe; it is never exposed
upstream. This is what lets a search caller be identical on both backends.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .dialect import Dialect

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


@dataclass(frozen=True)
class KeywordHit:
    """One keyword-search result. IDENTICAL shape on every backend.

    ``memory_id`` is the item id; ``score`` is a relevance score where LOWER is
    better (SQLite bm25 convention — the seam preserves it so callers that sort
    ascending are backend-blind). Backends that natively rank higher-is-better
    (Postgres ``ts_rank``) negate their score to honor this convention. The score
    is opaque and NOT comparable across backends — only its ordering within one
    result set is meaningful (bm25 and ts_rank are different scales; the plan
    accepts this and documents it, §8.2).
    """

    memory_id: str
    score: float


@dataclass(frozen=True)
class VectorHit:
    """One vector-search result. IDENTICAL shape on every backend.

    ``score`` is cosine similarity where HIGHER is better (the natural cosine
    convention, matching the existing ``vec_score = 1.0 - distance`` path that
    sorts DESC). This is the OPPOSITE direction from ``KeywordHit.score`` (bm25,
    lower-is-better) — deliberately, because the two signals are genuinely on
    different scales; the hybrid fusion step consumes both directions explicitly.
    Cosine is computed by the DB-blind Rust core over packed float32 blobs, so
    the score IS comparable across backends (same math, same embeddings) — unlike
    keyword scores.
    """

    memory_id: str
    score: float


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

    def dialect(self) -> "Dialect":
        """Return this backend's SQL dialect helper (see `dialect.py`)."""
        ...

    def ensure_schema(self) -> None:
        """Idempotently create the primary schema if absent.

        SQLite does this lazily on first `_db()` touch (`_lazy_init`), so its
        implementation is a no-op here. PostgreSQL had no equivalent — this
        applies ``pg_primary_v1.sql`` once so a fresh PG deployment gets its
        tables without a manual psql step. Safe to call repeatedly.
        """
        ...

    def schema_version(self) -> "int | None":
        """The highest applied schema version, or None if unknown/uninitialized.

        Reads ``MAX(version)`` from ``schema_versions``. On both backends the same
        table records applied migrations; the PG baseline (``pg_primary_v1.sql``)
        stamps version 39 (the SQLite migration level it was translated from).
        Returns None when the table is absent (schema not yet initialized).
        """
        ...

    def connection(self) -> AbstractContextManager:
        """A read/write connection context manager.

        On SQLite this is the pooled `sqlite3.Connection` used today; on
        PostgreSQL a pooled psycopg connection. Callers use it exactly as they
        use `_db()` today: ``with backend.connection() as conn: ...``.
        """
        ...

    def open_readonly(self, db_path: str) -> AbstractContextManager:
        """A read-only-intent connection to a SPECIFIC store, backend-blind.

        Some tools read a PARTICULAR SQLite db file (``file:...?mode=ro`` honoring
        ``db_path``). On file backends this opens that file read-only; on pooled
        backends (PostgreSQL) there is ONE store, so ``db_path`` is ignored and a
        normal pooled connection is yielded. Lets a caller do
        ``with backend.open_readonly(db_path) as conn:`` without branching on the
        backend name. The caller performs reads only.
        """
        ...

    def placeholder(self, n: int = 1) -> str:
        """Render `n` positional bind placeholders for this backend's driver.

        SQLite uses ``?``; psycopg uses ``%s``. Returns a comma-joined run, e.g.
        ``placeholder(3) -> "?, ?, ?"``. This replaces the scattered
        ``",".join("?" * n)`` idioms with one backend-aware helper (§6 port).
        """
        ...

    def keyword_search(
        self,
        conn: object,
        query: str,
        *,
        limit: int,
        tenancy_sql: str = "",
        tenancy_params: "tuple[object, ...]" = (),
    ) -> "list[KeywordHit]":
        """Native keyword search returning a backend-identical ranked list.

        SQLite uses the FTS5 ``memory_items_fts`` virtual table + ``bm25()``;
        Postgres uses a ``tsvector`` column + ``@@ tsquery`` + ``ts_rank``. Both
        return ``list[KeywordHit]`` (memory_id, score) with LOWER score = more
        relevant, ordered best-first — so the caller is identical on both. The
        raw query is compiled to the backend's match syntax internally
        (``_compile_fts_query`` / tsquery); an empty compile yields ``[]``.

        ``tenancy_sql`` is an optional pre-built ``AND ...`` fragment (already in
        this backend's placeholder style) with its ``tenancy_params``, appended
        to the WHERE so tenant scoping composes without this method knowing the
        tenancy model. Runs on the caller-supplied ``conn`` (same transaction).
        """
        ...

    def vector_search(
        self,
        conn: object,
        query_vector: "list[float]",
        *,
        limit: int,
        dim: int,
        embed_models: "tuple[str, ...]" = (),
        tenancy_sql: str = "",
        tenancy_params: "tuple[object, ...]" = (),
    ) -> "list[VectorHit]":
        """Backend-agnostic vector search: BYTEA/BLOB embeddings + Rust cosine.

        The add-on-free baseline that works on BOTH backends with no extension
        (no sqlite-vec, no pgvector): fetch candidate ``(id, embedding)`` rows
        for the compatible embed identity, then score every packed float32 blob
        against ``query_vector`` with the DB-blind Rust core
        (``_cosine_batch_packed``). Returns ``list[VectorHit]`` (memory_id,
        cosine) HIGHER-is-better, best-first, capped at ``limit``.

        ``embed_models`` restricts the join to compatible embedder identities
        (a blob from an incomparable vector space or wrong dim must not be
        scored — the plan's identity guard). ``dim`` is the expected embedding
        dimension. ``tenancy_sql``/``tenancy_params`` compose tenant scoping in
        this backend's placeholder style. Runs on the caller's ``conn``.

        NOTE: the baseline scores all candidates (bounded by tenancy + identity
        + is_deleted); an ANN accelerator (pgvector HNSW / sqlite-vec) is a
        Phase-4 opt-in chosen behind the capability probe, never required here.
        """
        ...
