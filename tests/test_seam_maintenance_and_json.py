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
