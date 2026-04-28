"""Tests for pg_sync.py manifest-driven sync refactor.

Coverage:
- Manifest YAML parses correctly (all three DBs)
- Composite PK ON CONFLICT clause builds correctly
- Tombstone with custom column/value (analysis_findings status='retracted')
- --dry-run doesn't write to SQLite or PG
- skip=true excludes table from sync loop
- _sync_table_generic pushes new rows (happy path)
- _sync_table_generic pulls remote rows into SQLite

Both SQLite in-memory databases stand in for both sides (no real PG needed).
The PG cursor is a lightweight fake that accepts execute_values-style calls.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))

# ── Stub psycopg2 (mirrors test_pg_sync_fk_safety.py) ─────────────────────────
if "psycopg2" not in sys.modules:
    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.Binary = bytes

    extras = types.ModuleType("psycopg2.extras")

    def _execute_values(cur, sql, values):
        """Minimal shim: pass the values list to cur.execute() as-is.

        This matches the original FK-safety test shim exactly, so that
        FakePgCursor.inserted_batches stores list-of-tuples and row[1]
        correctly returns the second column value.

        _SQLitePgCursor.execute() handles the list-of-tuples case by
        converting to executemany calls for real SQLite.
        """
        cur.execute(sql, values)

    extras.execute_values = _execute_values
    psycopg2.extras = extras
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras

import pg_sync

# ── Manifest file paths ────────────────────────────────────────────────────────
MANIFEST_DIR = REPO_ROOT / "config" / "sync_manifests"
AGENT_MEMORY_MANIFEST  = MANIFEST_DIR / "agent_memory.yaml"
AGENT_BENCH_MANIFEST   = MANIFEST_DIR / "agent_bench.yaml"
AGENT_BENCH_A_MANIFEST = MANIFEST_DIR / "agent_bench_analysis.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# 1. Manifest YAML parses correctly
# ─────────────────────────────────────────────────────────────────────────────

def test_agent_memory_manifest_parses():
    m = pg_sync._load_manifest(str(AGENT_MEMORY_MANIFEST))
    assert "tables" in m
    assert "sync_order" in m
    assert "_table_map" in m
    # All sync_order entries should appear in table_map
    for t in m["sync_order"]:
        assert t in m["_table_map"], f"sync_order entry '{t}' missing from tables"


def test_agent_bench_manifest_parses():
    m = pg_sync._load_manifest(str(AGENT_BENCH_MANIFEST))
    assert m["db"] == "agent_bench.db"
    assert "bench_hits" in m["_table_map"]
    assert m["_table_map"]["bench_hits"]["skip"] is True
    # bench_hits should NOT be in sync_order (it's skip-only, not ordered)
    assert "bench_hits" not in m["sync_order"]


def test_agent_bench_analysis_manifest_parses():
    m = pg_sync._load_manifest(str(AGENT_BENCH_A_MANIFEST))
    assert m["db"] == "agent_bench_analysis.db"
    tf = m["_table_map"]["analysis_findings"]
    assert tf["tombstone_column"] == "status"
    assert tf.get("tombstone_value") == "retracted"
    assert m["_table_map"]["bench_corpus_rows"]["skip"] is True


def test_all_manifests_have_required_keys():
    for path in [AGENT_MEMORY_MANIFEST, AGENT_BENCH_MANIFEST, AGENT_BENCH_A_MANIFEST]:
        m = pg_sync._load_manifest(str(path))
        assert "db" in m, f"{path.name} missing 'db'"
        assert "description" in m, f"{path.name} missing 'description'"
        for entry in m["tables"]:
            assert "name" in entry, f"{path.name}: table entry missing 'name'"
            assert "pk_columns" in entry, f"{path.name}: table {entry.get('name')} missing 'pk_columns'"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Composite PK ON CONFLICT clause
# ─────────────────────────────────────────────────────────────────────────────

def test_single_pk_conflict_clause():
    clause = pg_sync._build_conflict_clause(["id"])
    assert clause == "(id)"


def test_composite_pk_conflict_clause():
    clause = pg_sync._build_conflict_clause(["run_id", "qid"])
    assert clause == "(run_id, qid)"


def test_triple_pk_conflict_clause():
    clause = pg_sync._build_conflict_clause(["run_id", "qid", "config"])
    assert clause == "(run_id, qid, config)"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tombstone with custom column/value (analysis_findings)
# ─────────────────────────────────────────────────────────────────────────────

def test_analysis_findings_tombstone_config():
    """Manifest correctly describes status='retracted' as the soft-delete signal."""
    m = pg_sync._load_manifest(str(AGENT_BENCH_A_MANIFEST))
    tf = m["_table_map"]["analysis_findings"]
    assert tf["tombstone_column"] == "status"
    assert tf["tombstone_value"] == "retracted"
    # pk is finding_id
    assert tf["pk_columns"] == ["finding_id"]


# ─────────────────────────────────────────────────────────────────────────────
# 4. --dry-run doesn't write
# ─────────────────────────────────────────────────────────────────────────────

class _RecordingCursor:
    """Cursor that records all execute/executemany calls and never modifies state."""
    def __init__(self):
        self.calls = []
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.calls.append(("execute", sql, params))
        # Simulate SELECT * returning empty
        self.description = None
        self._rows = []

    def executemany(self, sql, params=None):
        self.calls.append(("executemany", sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return self._rows


def _make_sqlite_with_bench_tables() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE bench_runs (
            run_id TEXT PRIMARY KEY,
            updated_at TEXT,
            is_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE bench_questions (
            run_id TEXT,
            qid TEXT,
            updated_at TEXT,
            is_deleted INTEGER DEFAULT 0,
            PRIMARY KEY (run_id, qid)
        );
        CREATE TABLE bench_attempts (
            run_id TEXT,
            qid TEXT,
            config TEXT,
            updated_at TEXT,
            is_deleted INTEGER DEFAULT 0,
            PRIMARY KEY (run_id, qid, config)
        );
        CREATE TABLE sync_watermarks (
            direction TEXT PRIMARY KEY,
            last_synced_at TEXT NOT NULL
        );
    """)
    conn.execute("INSERT INTO bench_runs VALUES ('r1', '2026-04-01T00:00:00Z', 0)")
    conn.commit()
    return conn


def test_dry_run_does_not_write_to_sqlite():
    """In dry_run mode, _sync_table_generic must not write to SQLite."""
    conn = _make_sqlite_with_bench_tables()
    cur = conn.cursor()
    pg_cur = _RecordingCursor()

    m = pg_sync._load_manifest(str(AGENT_BENCH_MANIFEST))
    table_cfg = m["_table_map"]["bench_runs"]

    # Capture watermarks table state before
    cur.execute("SELECT COUNT(*) FROM sync_watermarks")
    wm_before = cur.fetchone()[0]

    pg_sync._sync_table_generic(cur, pg_cur, conn, "bench", table_cfg, dry_run=True)

    # Watermarks must NOT have been updated
    cur.execute("SELECT COUNT(*) FROM sync_watermarks")
    wm_after = cur.fetchone()[0]
    assert wm_after == wm_before, "dry_run must not update sync_watermarks"

    # No executemany (writes) to SQLite via the real cursor
    write_calls = [c for c in pg_cur.calls if c[0] == "executemany"]
    assert write_calls == [], f"dry_run must not call executemany on PG cursor: {write_calls}"

    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. skip=true excludes table from sync loop
# ─────────────────────────────────────────────────────────────────────────────

class _TrackingCursorWrapper:
    """Wraps a sqlite3.Cursor and records all SQL statements executed.

    sqlite3.Cursor.execute is read-only in Python 3.14+, so we cannot
    monkey-patch it. Instead we delegate via a wrapper object.
    """
    def __init__(self, real_cur: sqlite3.Cursor):
        self._cur = real_cur
        self.executed_sqls: list[str] = []
        self.description = None

    def execute(self, sql, params=None):
        self.executed_sqls.append(sql)
        if params is None:
            result = self._cur.execute(sql)
        else:
            result = self._cur.execute(sql, params)
        self.description = self._cur.description
        return result

    def executemany(self, sql, params=None):
        self.executed_sqls.append(sql)
        return self._cur.executemany(sql, params or [])

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


def test_skip_flag_excludes_table():
    """_sync_generic_db must not touch bench_hits (skip=true)."""
    conn = _make_sqlite_with_bench_tables()
    # Add bench_hits table to confirm it's never queried
    conn.execute("""
        CREATE TABLE bench_hits (
            hit_id TEXT PRIMARY KEY,
            run_id TEXT,
            created_at TEXT
        )
    """)
    conn.execute("INSERT INTO bench_hits VALUES ('h1', 'r1', '2026-04-01T00:00:00Z')")
    conn.commit()

    tracking_cur = _TrackingCursorWrapper(conn.cursor())

    class _NullPgCur:
        description = None
        def execute(self, sql, params=None): pass
        def executemany(self, sql, params=None): pass
        def fetchone(self): return None
        def fetchall(self): return []

    m = pg_sync._load_manifest(str(AGENT_BENCH_MANIFEST))

    # Use dry_run=True to avoid needing real PG; we just care about what tables are touched
    pg_sync._sync_generic_db(tracking_cur, _NullPgCur(), conn, m, "bench", dry_run=True)

    # No SQL should reference bench_hits
    bench_hits_sqls = [s for s in tracking_cur.executed_sqls if "bench_hits" in s.lower()]
    assert bench_hits_sqls == [], (
        f"bench_hits (skip=true) must never be queried; got: {bench_hits_sqls}"
    )
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 6. _sync_table_generic pushes new rows (happy path, SQLite→SQLite stand-in)
# ─────────────────────────────────────────────────────────────────────────────

class _SQLitePgCursor:
    """Wraps a real SQLite cursor pretending to be a PG cursor.

    execute_values calls arrive as execute(sql, list_of_tuples).  We detect
    this case and route through executemany with a %s→? substitution and
    simplified ON CONFLICT clause.
    """
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._cur = conn.cursor()
        self.description = None

    def execute(self, sql: str, params=None):
        import re
        # Skip PG-only DDL that has no SQLite equivalent
        if any(kw in sql.upper() for kw in ("INFORMATION_SCHEMA", "ALTER TABLE", "CREATE INDEX IF NOT EXISTS")):
            return

        # Detect execute_values call: params is a list of tuples
        if isinstance(params, list) and params and isinstance(params[0], tuple):
            ncols = len(params[0])
            placeholders = "(" + ", ".join(["?"] * ncols) + ")"
            fixed_sql = re.sub(r"VALUES\s+%s", f"VALUES {placeholders}", sql, flags=re.IGNORECASE)
            # Simplify ON CONFLICT: strip PG-specific WHERE guard
            oc_match = re.search(
                r"ON CONFLICT\s*\(([^)]+)\)\s*DO\s+UPDATE\s+SET\s+(.+?)(?:WHERE.*)?$",
                fixed_sql, re.IGNORECASE | re.DOTALL,
            )
            if oc_match:
                conflict_cols = oc_match.group(1).strip()
                set_part = oc_match.group(2).strip()
                set_part = re.split(r"\bWHERE\b", set_part, maxsplit=1, flags=re.IGNORECASE)[0].strip()
                insert_part = fixed_sql[:fixed_sql.upper().index("ON CONFLICT")].strip()
                fixed_sql = f"{insert_part} ON CONFLICT ({conflict_cols}) DO UPDATE SET {set_part}"
            self._cur.executemany(fixed_sql, params)
            self.description = self._cur.description
            return

        # Standard single-row execute — replace PG %s placeholders with ?
        fixed_sql = sql.replace("%s", "?")
        self._cur.execute(fixed_sql, params or ())
        self.description = self._cur.description

    def executemany(self, sql: str, params=None):
        self._cur.executemany(sql.replace("%s", "?"), params or [])

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


def _make_pg_sqlite() -> sqlite3.Connection:
    """Create a 'PG-side' in-memory SQLite DB with bench_runs."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE bench_runs (
            run_id TEXT PRIMARY KEY,
            updated_at TEXT,
            is_deleted INTEGER DEFAULT 0
        );
    """)
    return conn


def test_generic_sync_pushes_rows():
    """Verify rows in SQLite are pushed to 'PG' (second SQLite DB) via _sync_table_generic."""
    sl_conn = _make_sqlite_with_bench_tables()
    pg_conn = _make_pg_sqlite()

    sl_cur = sl_conn.cursor()
    pg_cur = _SQLitePgCursor(pg_conn)

    m = pg_sync._load_manifest(str(AGENT_BENCH_MANIFEST))
    table_cfg = m["_table_map"]["bench_runs"]

    pg_sync._sync_table_generic(sl_cur, pg_cur, sl_conn, "bench", table_cfg, dry_run=False)

    # Check row landed in 'PG'
    pg_conn.row_factory = sqlite3.Row
    pg_check = pg_conn.cursor()
    pg_check.execute("SELECT run_id FROM bench_runs")
    rows = pg_check.fetchall()
    run_ids = {r[0] for r in rows}
    assert "r1" in run_ids, f"Expected r1 in PG bench_runs; got {run_ids}"

    sl_conn.close()
    pg_conn.close()


def test_generic_sync_pulls_rows():
    """Verify rows in 'PG' are pulled into SQLite via _sync_table_generic."""
    sl_conn = _make_sqlite_with_bench_tables()
    pg_conn = _make_pg_sqlite()

    # Seed PG with a row not in local SQLite
    pg_conn.execute("INSERT INTO bench_runs VALUES ('r-remote', '2026-04-25T00:00:00Z', 0)")
    pg_conn.commit()

    sl_cur = sl_conn.cursor()
    pg_cur = _SQLitePgCursor(pg_conn)

    m = pg_sync._load_manifest(str(AGENT_BENCH_MANIFEST))
    table_cfg = m["_table_map"]["bench_runs"]

    pg_sync._sync_table_generic(sl_cur, pg_cur, sl_conn, "bench", table_cfg, dry_run=False)

    # Check row landed in local SQLite
    sl_cur.execute("SELECT run_id FROM bench_runs")
    run_ids = {r[0] for r in sl_cur.fetchall()}
    assert "r-remote" in run_ids, f"Expected r-remote in local bench_runs; got {run_ids}"

    sl_conn.close()
    pg_conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 7. Infer manifest path from DB basename
# ─────────────────────────────────────────────────────────────────────────────

def test_infer_manifest_path_agent_memory():
    path = pg_sync._infer_manifest_path("/some/dir/agent_memory.db")
    assert path.endswith("agent_memory.yaml"), path


def test_infer_manifest_path_agent_bench():
    path = pg_sync._infer_manifest_path("/some/dir/agent_bench.db")
    assert path.endswith("agent_bench.yaml"), path


def test_infer_manifest_path_agent_bench_analysis():
    path = pg_sync._infer_manifest_path("/some/dir/agent_bench_analysis.db")
    assert path.endswith("agent_bench_analysis.yaml"), path
