"""Regression guard: dashboard panels must survive a SUBSET-schema store.

Reported as issue #141 (m3-memory 2026.09.09, Ubuntu 24, Python 3.12, SQLite):
switching the dashboard's DB selector to the chatlog store broke two panels --

    KB Browser           -> Error scanning DB: no such column: mi.confidence
    Conflict & Audit Log -> History table memory_history does not exist ...

Cause: the chatlog schema is a DELIBERATE SUBSET of main (see
``migrate_memory.py``: "same memory_items / memory_embeddings / FTS", none of
the trust or history machinery). It has no ``confidence``,
``corroboration_count`` or ``contradiction_count`` column and no
``memory_history`` table. The dashboard hardcoded the main store's full
provenance column list into its browse/search SELECT, so pointing it at any
subset store was a hard SQL error -- while the RENDER layer had already been
hardened for exactly this case (``_row_get``: "a store that predates a column
must render a card, not a 500"). Only the SELECT layer was missing.

Two backend traps this pins down, both of which the obvious fix walks into:

  - ``PRAGMA table_info`` is SQLite-only; on PostgreSQL the probe must use
    ``information_schema``. Hence the seam (``dialect().columns_of()``), which
    normalises the row shape so the name is at ``row[0]`` on both.
  - select-then-catch is NOT a portable fallback: on PostgreSQL an
    ``UndefinedColumn`` aborts the transaction, so the retry dies with
    ``InFailedSqlTransaction`` (documented on ``Dialect.column_exists``). The
    probe must happen UP FRONT.

DESIGN_PHILOSOPHIES 10a: the column list is derived from the store, never
hand-maintained at the call site, so the next provenance column added to main
cannot silently break the chatlog view the way this one did.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_BIN = os.path.join(_ROOT, "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends.sqlite_backend import SqliteDialect  # noqa: E402

# Mirrors dashboard_server.get_kb_cards. Core = every store has these; prov =
# main-only trust/provenance columns that a subset store may lack.
_CORE_COLS = ("id", "type", "title", "content", "importance")
_PROV_COLS = (
    "metadata_json", "origin_device", "change_agent", "created_at",
    "updated_at", "confidence", "pinned", "source", "valid_from",
    "valid_to", "corroboration_count", "contradiction_count",
)
# Present on main, absent on the chatlog subset -- the exact trio from #141.
_MAIN_ONLY = ("confidence", "corroboration_count", "contradiction_count")


def _make_store(path: str, *, subset: bool) -> None:
    """Create a memory_items table shaped like main (full) or chatlog (subset)."""
    cols = [
        "id TEXT PRIMARY KEY", "type TEXT", "title TEXT", "content TEXT",
        "metadata_json TEXT", "importance REAL", "origin_device TEXT",
        "change_agent TEXT", "created_at TEXT", "updated_at TEXT",
        "pinned INTEGER", "source TEXT", "valid_from TEXT", "valid_to TEXT",
        "is_deleted INTEGER DEFAULT 0",
    ]
    if not subset:
        cols += ["confidence REAL", "corroboration_count INTEGER",
                 "contradiction_count INTEGER"]
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memory_items (" + ", ".join(cols) + ")")
    if not subset:
        conn.execute("CREATE TABLE memory_history (event TEXT, memory_id TEXT, "
                     "prev_value TEXT, new_value TEXT, created_at TEXT)")
    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, importance, is_deleted) "
        "VALUES ('m1', 'chat_log', 'T', 'C', 0.5, 0)"
    )
    conn.commit()
    conn.close()


def _probe(conn) -> set:
    """What available_columns() does, via the same seam call."""
    sql, params = SqliteDialect().columns_of("memory_items")
    return {r[0] for r in conn.execute(sql, params).fetchall()}


def _browse_sql(present: set) -> str:
    prov = tuple(c for c in _PROV_COLS if c in present) if present else ()
    sel = ", ".join("mi." + c for c in (*_CORE_COLS, *prov))
    return "SELECT " + sel + " FROM memory_items AS mi WHERE mi.is_deleted = 0"


@pytest.mark.parametrize("subset", [True, False], ids=["chatlog_subset", "main_full"])
def test_browse_query_runs_on_both_schemas(tmp_path, subset):
    """The #141 repro: the browse SELECT must execute on a subset store."""
    db = str(tmp_path / "s.db")
    _make_store(db, subset=subset)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(_browse_sql(_probe(conn))).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == "m1"


def test_main_store_keeps_every_provenance_column(tmp_path):
    """No regression on main: nothing is dropped when the store has it all."""
    db = str(tmp_path / "main.db")
    _make_store(db, subset=False)
    conn = sqlite3.connect(db)
    sql = _browse_sql(_probe(conn))
    for col in _PROV_COLS:
        assert "mi." + col in sql, col + " must still be selected on main"


def test_subset_store_drops_only_absent_columns(tmp_path):
    """Absent columns are omitted; every column the store HAS is still read."""
    db = str(tmp_path / "chat.db")
    _make_store(db, subset=True)
    conn = sqlite3.connect(db)
    present = _probe(conn)
    sql = _browse_sql(present)
    for col in _MAIN_ONLY:
        assert "mi." + col not in sql, col + " is absent here; must not be selected"
    for col in _PROV_COLS:
        if col in present:
            assert "mi." + col in sql, col + " exists here and must still be selected"


def test_failed_probe_falls_back_to_core_columns():
    """An unreadable catalog yields core-only SQL, never a crash or empty SELECT."""
    sql = _browse_sql(set())
    for col in _CORE_COLS:
        assert "mi." + col in sql
    for col in _PROV_COLS:
        assert "mi." + col not in sql


def test_available_columns_returns_empty_on_probe_failure():
    """available_columns() degrades to an empty set rather than raising."""
    import dashboard_server

    class _Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("catalog unavailable")

    assert dashboard_server.available_columns(_Broken(), "memory_items") == set()


def test_probe_rejects_non_identifier_table():
    """The seam validates identifiers on both backends (injection guard)."""
    with pytest.raises(ValueError):
        SqliteDialect().columns_of("memory_items; DROP TABLE memory_items")


def test_postgres_probe_uses_information_schema_not_pragma():
    """Cross-backend: the PG probe must not emit SQLite-only PRAGMA syntax."""
    from memory.backends.postgres_backend import PostgresDialect

    sql, params = PostgresDialect().columns_of("memory_items")
    assert "information_schema.columns" in sql
    assert "pragma" not in sql.lower()
    assert params == ("memory_items",)


def test_dashboard_does_not_hardcode_main_only_columns_in_sql():
    """10a drift guard: no literal main-only column inside a dashboard SELECT.

    The column list must stay derived from the store. A future edit that inlines
    ``mi.confidence`` into a SELECT reintroduces #141 for every subset store, so
    fail here rather than in a user's browser.
    """
    with open(os.path.join(_BIN, "dashboard_server.py"), encoding="utf-8") as fh:
        src = fh.read()
    for stmt in re.findall(r"SELECT[\s\S]{0,800}?FROM\s+memory_items", src, re.I):
        for col in _MAIN_ONLY:
            assert not re.search(r"\bmi\." + col + r"\b", stmt), (
                col + " is hardcoded in a memory_items SELECT -- build the column "
                "list from available_columns() instead (issue #141)"
            )

def test_core_columns_are_in_every_bootstrap_schema():
    """The core set must be guaranteed by EVERY schema, not just observed today.

    ``_CORE_COLS`` plus ``is_deleted`` are selected/filtered unconditionally, so
    they are only safe if every store creates them at bootstrap. ``confidence``
    is the counter-example that caused #141: it arrives via a later main-only
    migration, which is why it must be probed. If a future schema edit drops one
    of the core columns from a bootstrap, this fails instead of the dashboard.
    """
    schemas = [
        os.path.join(_ROOT, "memory", "chatlog_migrations", "001_bootstrap.up.sql"),
        os.path.join(_ROOT, "memory", "migrations", "001_initial_schema.sql"),
        os.path.join(_ROOT, "memory", "migrations", "postgres", "pg_primary_v1.sql"),
    ]
    for path in schemas:
        if not os.path.exists(path):
            pytest.skip("schema not present in this checkout: " + path)
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
        m = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?memory_items\s*\((.*?)\n\);",
                      sql, re.S | re.I)
        assert m, "memory_items DDL not found in " + os.path.basename(path)
        body = m.group(1)
        for col in (*_CORE_COLS, "is_deleted"):
            assert re.search(r"^\s*" + col + r"\s", body, re.M), (
                col + " must be in the bootstrap of " + os.path.basename(path) +
                " -- the dashboard selects/filters it unconditionally"
            )
