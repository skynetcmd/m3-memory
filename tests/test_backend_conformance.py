"""Behavioral backend-conformance suite (plan A4).

Every REGISTERED backend must satisfy the same contract, discovered the way the
runtime discovers backends (via the registry — I1), not a hand-maintained list.
Three layers:

  1. STRUCTURAL — the backend satisfies the ``StorageBackend`` Protocol and its
     dialect overrides every DIVERGENT method (derived programmatically, RH6:
     a base method whose body raises ``NotImplementedError``). A backend that
     forgets an override is caught here, not at a random call site in production.
  2. BEHAVIORAL — on the CPU-only floor (no sqlite-vec / pgvector), a written row
     is retrievable via BOTH ``keyword_search`` AND ``vector_search`` with the
     backend-identical result shape. This is the "universal floor never regresses"
     guarantee (§1) as an executable test.
  3. FAIL-LOUD — a bare ``Dialect`` base instance raises ``NotImplementedError``
     for each divergent method (§3: a new backend never silently inherits another
     backend's SQL).

SQLite runs always (in-memory). Postgres runs only when ``M3_PRIMARY_PG_URL`` is
set (a throwaway cluster) — the same gate the other live-PG tests use.
"""
from __future__ import annotations

import os
import struct

import pytest
from memory.backends import selector as _selector
from memory.backends.base import StorageBackend
from memory.backends.dialect import Dialect


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    _selector._reset_for_tests()
    yield
    _selector._reset_for_tests()


def _all_registered_names():
    """Every backend name the registry knows, discovered like the runtime (I1).

    Ensures both allow-listed names are imported+registered first. New backends
    are picked up with no edit here.
    """
    from memory.backends import registry

    for name in _selector._VALID:
        registry._ensure_registered(name)
    return list(registry.registered_names())


def _try_instance(name):
    """Instantiate the backend, or None if it needs external resources absent here.

    A backend like Postgres fails loud when instantiated without a DSN (correct
    §3 behavior) — that is NOT a conformance failure, so callers that only need a
    live instance skip a name that returns None. The DIALECT is checked separately
    via the registry (needs no instance).
    """
    from memory.backends import registry

    try:
        return registry.backend_factory_for(name)()
    except Exception:
        return None


def _registered_dialect(name):
    from memory.backends import registry

    return registry.dialect_singleton_for(name)


def _divergent_methods() -> set[str]:
    """Public Dialect methods whose BASE body raises NotImplementedError (RH6).

    Derived by actually CALLING each on a bare base instance and seeing which
    raise — not a hand-maintained list, so a newly-added abstract helper is
    automatically in-scope. Methods needing args are probed with representative
    trusted identifiers; a method that raises ValueError on bad args (validation
    wrappers) is invoked with valid args so only a genuine NotImplementedError
    counts.
    """
    base = Dialect(backend="sqlite", param_style="qmark")  # type: ignore[arg-type]
    probes = {
        "insert_or_ignore": (),
        "on_conflict_ignore": (),
        "now": (),
        "now_minus_days": ("?",),
        "empty_json_default": (),
        "returning_id_clause": (),
        "last_insert_id": (object(),),
        "json_extract_text": ("metadata_json", "k"),
        "json_extract_int": ("metadata_json", "k"),
        "coalesce_open_timestamp": ("valid_to", "?"),
        "temporal_open_clause": ("mi.valid_from", "<="),
        "table_exists": ("memory_items",),
        "columns_of": ("memory_items",),
    }
    divergent: set[str] = set()
    for name, args in probes.items():
        meth = getattr(base, name)
        try:
            meth(*args)
        except NotImplementedError:
            divergent.add(name)
        except Exception:
            # A non-NotImplementedError means the base gave a concrete result (or
            # validated) — not divergent-abstract. Skip.
            pass
    return divergent


# ── 1. STRUCTURAL ────────────────────────────────────────────────────────────


def test_every_backend_satisfies_protocol():
    names = _all_registered_names()
    assert names, "no backends registered — registry wiring broke"
    checked = 0
    for name in names:
        backend = _try_instance(name)
        if backend is None:
            # Not instantiable in this environment (e.g. PG with no DSN) — its
            # dialect conformance is still checked below; skip the live-instance
            # Protocol check here rather than failing on a correct fail-loud.
            continue
        assert isinstance(backend, StorageBackend), (
            f"{name} backend does not satisfy the StorageBackend Protocol"
        )
        assert backend.name == name
        checked += 1
    assert checked >= 1, "no backend was instantiable — expected at least sqlite"


def test_every_dialect_overrides_every_divergent_method():
    divergent = _divergent_methods()
    # sanity: we actually found the abstract surface (guards a broken probe)
    assert len(divergent) >= 10, f"divergent-method probe found only {divergent}"
    names = _all_registered_names()
    assert names, "no backends registered"
    for name in names:
        d = _registered_dialect(name)  # registry — no live instance needed
        for meth in divergent:
            # The concrete dialect's method object must NOT be the base's — i.e.
            # it is actually overridden. For validation-wrapper methods
            # (json_extract_*, temporal_open_clause, table_exists, columns_of) the
            # PUBLIC method stays on the base; its divergence lives in the private
            # _*_expr / _*_query fragment, so check that instead.
            wrapper_to_fragment = {
                "json_extract_text": "_json_extract_text_expr",
                "json_extract_int": "_json_extract_int_expr",
                "temporal_open_clause": "_temporal_open_clause_expr",
                "table_exists": "_table_exists_query",
                "columns_of": "_columns_of_query",
            }
            check = wrapper_to_fragment.get(meth, meth)
            assert getattr(type(d), check) is not getattr(Dialect, check), (
                f"{name} dialect does not override {check} (divergent method)"
            )


# ── 2. BEHAVIORAL — CPU-only floor ───────────────────────────────────────────

_DIM = 4


def _blob(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _sqlite_floor_conn():
    """An in-memory SQLite store with the minimal schema keyword+vector need.

    No sqlite-vec loaded — this IS the CPU-only floor. Mirrors the isolated
    fixtures in test_keyword_search_seam so the conformance run is hermetic.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memory_items(
            id TEXT PRIMARY KEY, title TEXT, content TEXT,
            is_deleted INTEGER DEFAULT 0, user_id TEXT DEFAULT '');
        CREATE VIRTUAL TABLE memory_items_fts USING fts5(
            title, content, content='memory_items', content_rowid='rowid');
        CREATE TABLE memory_embeddings(
            memory_id TEXT, embedding BLOB, dim INTEGER, embed_model TEXT);
        """
    )
    return conn


def test_sqlite_floor_write_then_retrieve_both_ways():
    """Write a row; retrieve it via keyword AND vector on the add-on-free floor."""
    from memory.backends.sqlite_backend import SqliteBackend

    backend = SqliteBackend()
    conn = _sqlite_floor_conn()
    cur = conn.execute(
        "INSERT INTO memory_items(id, title, content) VALUES (?,?,?)",
        ("m1", "postgres tuning", "shared buffers guide"),
    )
    conn.execute(
        "INSERT INTO memory_items_fts(rowid, title, content) VALUES (?,?,?)",
        (cur.lastrowid, "postgres tuning", "shared buffers guide"),
    )
    conn.execute(
        "INSERT INTO memory_embeddings(memory_id, embedding, dim, embed_model) "
        "VALUES (?,?,?,?)",
        ("m1", _blob([1.0, 0.0, 0.0, 0.0]), _DIM, "test-model"),
    )
    conn.commit()

    # keyword floor (FTS5, no accelerator)
    khits = backend.keyword_search(conn, "postgres", limit=10)
    assert [h.memory_id for h in khits] == ["m1"]

    # vector floor (Rust cosine over BLOB, no sqlite-vec)
    vhits = backend.vector_search(
        conn, [1.0, 0.0, 0.0, 0.0], limit=10, dim=_DIM, embed_models=("test-model",)
    )
    assert [h.memory_id for h in vhits] == ["m1"]
    # shape identical across search kinds
    assert all(hasattr(h, "memory_id") and hasattr(h, "score") for h in khits + vhits)


@pytest.mark.skipif(
    not os.environ.get("M3_PRIMARY_PG_URL"),
    reason="requires M3_PRIMARY_PG_URL (throwaway cluster)",
)
def test_postgres_floor_write_then_retrieve_both_ways():
    """Same behavioral floor on live Postgres when a throwaway cluster is set.

    Uses ensure_schema() so a fresh DB has the search_vector column keyword_search
    needs (RH7 prerequisite), inserts a dim/model-compatible embedding, and
    asserts non-empty retrieval both ways.
    """
    import uuid

    from memory.backends.postgres_backend import PostgresBackend

    backend = PostgresBackend()
    backend.ensure_schema()
    mid = f"conf-{uuid.uuid4().hex[:12]}"
    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO memory_items(id, title, content, is_deleted) "
            "VALUES (%s,%s,%s,0) ON CONFLICT (id) DO NOTHING",
            (mid, "postgres tuning", "shared buffers guide"),
        )
        cur.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, dim, embed_model) "
            "VALUES (%s,%s,%s,%s)",
            (mid, _blob([1.0, 0.0, 0.0, 0.0]), _DIM, "test-model"),
        )
        conn.commit()
        khits = backend.keyword_search(conn, "postgres", limit=10)
        vhits = backend.vector_search(
            conn, [1.0, 0.0, 0.0, 0.0], limit=10, dim=_DIM,
            embed_models=("test-model",),
        )
    assert mid in {h.memory_id for h in khits}
    assert mid in {h.memory_id for h in vhits}


# ── 3. FAIL-LOUD ─────────────────────────────────────────────────────────────


def test_bare_dialect_raises_for_every_divergent_method():
    base = Dialect(backend="sqlite", param_style="qmark")  # type: ignore[arg-type]
    # The public wrappers delegate to a private _*_expr fragment that raises; the
    # direct-abstract methods raise themselves. Both must fail loud.
    with pytest.raises(NotImplementedError):
        base.insert_or_ignore()
    with pytest.raises(NotImplementedError):
        base.now()
    with pytest.raises(NotImplementedError):
        base.returning_id_clause()
    with pytest.raises(NotImplementedError):
        base.json_extract_text("metadata_json", "k")  # -> _json_extract_text_expr
    with pytest.raises(NotImplementedError):
        base.table_exists("memory_items")  # -> _table_exists_query


def test_unregistered_backend_fails_loud():
    from memory.backends.dialect import dialect_for

    with pytest.raises(ValueError):
        dialect_for("mysql")  # type: ignore[arg-type]
