"""Seam primitives added for the wiki-synthesis compile writer.

Both exist so FEATURE code never branches on the backend (§1): a batch writer
checkpoints without knowing it is on SQLite, and a JSON-column read works
whether the driver hands back TEXT (SQLite) or an already-parsed dict
(Postgres JSONB / MariaDB JSON).
"""
import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends.dialect import dialect_for  # noqa: E402
from memory.backends.sqlite_backend import SqliteBackend  # noqa: E402

SQLITE = dialect_for("sqlite")
POSTGRES = dialect_for("postgres")


# ── json_column_to_dict ──────────────────────────────────────────────────────
# The read-side complement to empty_json_default(). A bare json.loads() works on
# SQLite and raises TypeError on Postgres; this is why callers must not roll
# their own shim.

@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_json_column_to_dict_handles_both_driver_shapes(dialect):
    """TEXT (SQLite) and pre-parsed dict (Postgres JSONB) both normalize."""
    assert dialect.json_column_to_dict('{"authority": "canonical"}') == {
        "authority": "canonical"
    }
    # Postgres JSONB: psycopg returns a dict already. json.loads() on this
    # raises TypeError — the trap this helper exists to absorb.
    assert dialect.json_column_to_dict({"authority": "canonical"}) == {
        "authority": "canonical"
    }


@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
@pytest.mark.parametrize(
    "value",
    [None, "", b"", "not json at all", "[1, 2, 3]", '"a string"', "123", b"\xff\xfe"],
    ids=["none", "empty-str", "empty-bytes", "garbage", "json-list",
         "json-str", "json-int", "bad-utf8"],
)
def test_json_column_to_dict_fails_safe(dialect, value):
    """Anything not a JSON OBJECT yields {} — never raises.

    Fail-safe matters at the render gate: a caller checking
    `authority == "canonical"` must treat an unreadable row as NOT
    authoritative, and one malformed row must not abort a batch render.
    """
    assert dialect.json_column_to_dict(value) == {}


def test_json_column_to_dict_round_trips_the_empty_default():
    """Whatever empty_json_default() writes must read back as {} on that same
    backend — the write and read sides of the seam agree."""
    for dialect in (SQLITE, POSTGRES):
        assert dialect.json_column_to_dict(dialect.empty_json_default()) == {}


def test_json_column_to_dict_preserves_nested_structure():
    """compiler.member_ids et al. survive intact — no flattening/coercion."""
    meta = {
        "synthesis_kind": "compiled",
        "compiler": {"member_ids": ["a", "b"], "member_count": 2},
    }
    import json
    assert SQLITE.json_column_to_dict(json.dumps(meta)) == meta


# ── maintenance_checkpoint ───────────────────────────────────────────────────

def test_sqlite_maintenance_checkpoint_runs_and_is_idempotent(tmp_path):
    """PASSIVE mid-batch and TRUNCATE at exit both succeed on a real WAL db."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(200)])
    conn.commit()

    be = SqliteBackend()
    be.maintenance_checkpoint(conn)                 # mid-batch
    be.maintenance_checkpoint(conn)                 # idempotent
    be.maintenance_checkpoint(conn, final=True)     # clean exit

    # Data survives the checkpoints (TRUNCATE flushes WAL into the main file).
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 200
    conn.close()


def test_sqlite_maintenance_checkpoint_never_raises_on_a_dead_conn():
    """Best-effort by contract: housekeeping must not abort the caller's batch.

    A closed connection is the realistic failure (a writer checkpointing on a
    cadence after cleanup); TRUNCATE blocking on a concurrent reader is the
    other. Neither may propagate.
    """
    conn = sqlite3.connect(":memory:")
    conn.close()
    be = SqliteBackend()
    be.maintenance_checkpoint(conn)               # must not raise
    be.maintenance_checkpoint(conn, final=True)   # must not raise


def test_backends_expose_maintenance_checkpoint():
    """Every registered backend implements it, so a caller never branches.

    Postgres's is a documented no-op (the server runs its own checkpointer);
    what matters is that the METHOD exists on every backend, so adding a third
    one cannot silently reintroduce `if backend == "sqlite"` at call sites.
    """
    assert callable(getattr(SqliteBackend(), "maintenance_checkpoint", None))
    from memory.backends import postgres_backend
    assert callable(getattr(postgres_backend.PostgresBackend, "maintenance_checkpoint", None))


# ── json_bind_value ──────────────────────────────────────────────────────────
# The write-side complement to json_column_to_dict. Closes the export→import
# round-trip trap: a PG JSONB whole-column read yields a dict, and re-binding a
# bare dict raises psycopg2 "can't adapt type dict". Serializing to a JSON string
# binds on BOTH backends. Callers must not roll their own json.dumps-if-dict shim.

@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_json_bind_value_serializes_dict_and_list(dialect):
    """dict / list → a JSON string (the shape that binds on both backends)."""
    import json
    assert dialect.json_bind_value({"a": 1, "b": ["x", "y"]}) == json.dumps({"a": 1, "b": ["x", "y"]})
    assert dialect.json_bind_value([1, 2, 3]) == "[1, 2, 3]"


@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_json_bind_value_passes_string_through_verbatim(dialect):
    """An already-serialized str is NOT re-dumped (that would double-encode)."""
    assert dialect.json_bind_value('{"already": "json"}') == '{"already": "json"}'
    # Even a non-JSON string passes through — the seam serializes, it does not validate.
    assert dialect.json_bind_value("") == ""


@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_json_bind_value_none_is_sql_null(dialect):
    """None → None (SQL NULL), NOT the string 'null' — the column is nullable."""
    assert dialect.json_bind_value(None) is None


def test_json_bind_value_round_trips_with_json_column_to_dict():
    """The two seam sides agree: bind a dict, read it back, get the same dict.

    This is the exact export→import path that raised 'can't adapt type dict' —
    modelled here without a live driver by composing the two primitives.
    """
    meta = {"provider": "anthropic", "nested": {"k": 1}, "list": [1, 2]}
    for dialect in (SQLITE, POSTGRES):
        bound = dialect.json_bind_value(meta)          # dict -> json string
        assert isinstance(bound, str)
        assert dialect.json_column_to_dict(bound) == meta  # string -> dict


# ── compact_storage ──────────────────────────────────────────────────────────
# Replaces the `resolve_backend_name() != "sqlite"` VACUUM branch. The dialect
# owns the compaction decision so a future backend with a real compaction
# (MariaDB OPTIMIZE TABLE) cannot fall into a call-site no-op silently.

def test_compact_storage_sqlite_vacuums_a_real_db(tmp_path):
    """SQLite VACUUMs the active file and reports success."""
    db = tmp_path / "c.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"r{i}",) for i in range(100)])
    conn.commit()
    conn.close()
    out = SQLITE.compact_storage(sqlite_path=str(db))
    assert "Space reclaimed (VACUUM)" == out


def test_compact_storage_sqlite_size_gated(tmp_path):
    """A DB over max_bytes is skipped with a clear reason (issue #46), not hung."""
    db = tmp_path / "big.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    # Force the gate with a tiny ceiling rather than building a huge file.
    out = SQLITE.compact_storage(sqlite_path=str(db), max_bytes=0)
    assert "too large" in out


def test_compact_storage_sqlite_missing_path_is_reported_not_raised():
    """No active path → a skip line, never an exception."""
    out = SQLITE.compact_storage(sqlite_path=None)
    assert "skipped" in out.lower()


def test_compact_storage_postgres_is_a_documented_noop():
    """PG returns the autovacuum no-op line and ignores sqlite_path entirely."""
    out = POSTGRES.compact_storage(sqlite_path="/nonexistent/ignored.db")
    assert "not applicable on PostgreSQL" in out
    assert "autovacuum" in out


def test_compact_storage_base_default_is_a_safe_noop():
    """A backend that overrides nothing still works — the base is a no-op, so a
    future dialect cannot accidentally crash the maintenance pass by omission."""
    from memory.backends.dialect import Dialect
    # Construct the base directly (a stand-in for a future backend that inherits
    # compact_storage without overriding it) — args mirror the concrete dialects.
    # param_style is a Literal["qmark","format"], so pass the bare string.
    base = Dialect(backend="sqlite", param_style="qmark")
    out = base.compact_storage(sqlite_path=None)
    assert "skipped" in out.lower()


# ── qualified_table (schema namespacing for a separate logical store) ─────────
# SQLite keeps a separate-store table BARE (separate DB file); PG qualifies it
# with the schema (namespace in the one primary DB). The mechanism the files
# store uses instead of `search_path`, so a pooled connection is leak-safe.

def test_qualified_table_sqlite_is_bare():
    """SQLite ignores the schema — the store is a separate file, name is bare."""
    assert SQLITE.qualified_table("leaves", schema="files") == "leaves"


def test_qualified_table_postgres_prepends_schema():
    """PG qualifies with the schema so the name resolves regardless of search_path."""
    assert POSTGRES.qualified_table("leaves", schema="files") == "files.leaves"
    assert POSTGRES.qualified_table("file_nodes", schema="files") == "files.file_nodes"


@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
@pytest.mark.parametrize("bad", ["a.b", "a b", "a;b", "", "1x-y", "drop table"])
def test_qualified_table_rejects_injection(dialect, bad):
    """name/schema are trusted identifiers — a non-identifier raises, never emits."""
    with pytest.raises(ValueError):
        dialect.qualified_table(bad, schema="files")
    with pytest.raises(ValueError):
        dialect.qualified_table("leaves", schema=bad)


def test_files_table_helper_resolves_per_backend(monkeypatch):
    """files_memory.config.files_table() threads through the ACTIVE dialect."""
    import sys as _sys
    _bin = os.path.normpath(os.path.join(_HERE, "..", "bin"))
    if _bin not in _sys.path:
        _sys.path.insert(0, _bin)
    from files_memory.config import files_table

    # Default active backend is SQLite -> bare.
    assert files_table("leaves") == "leaves"

    # Point the seam at the postgres dialect and re-resolve -> qualified.
    import memory.backends as _backends
    monkeypatch.setattr(_backends, "dialect", lambda: POSTGRES)
    assert files_table("leaves") == "files.leaves"
