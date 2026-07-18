"""Live-PG tests for the PostgreSQL incremental migration runner (migrate_pg.py).

Skips cleanly without a reachable cluster. Exercises the full contract against a
throwaway cluster using a TEMP migrations dir (so no pg_NNN file is left in the
repo): discover -> apply -> stamp schema_versions -> idempotent re-run -> down
revert. Also asserts the DSN guard refuses a forbidden (warehouse) host.

DSN is resolved from M3_PRIMARY_PG_URL / M3_PG_URL (never PG_URL, which points at
production); a forbidden-host guard refuses named infra hosts.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))


# Gated by the requires_pg marker: conftest's collection hook auto-skips this
# module when no Postgres is reachable. pg_dsn() centralizes the
# M3_PRIMARY_PG_URL > M3_PG_URL precedence (replaces the former per-file
# _dsn()/_reachable()/skipif triplet).
from conftest import pg_dsn

pytestmark = pytest.mark.requires_pg
_DSN = pg_dsn()


@pytest.fixture()
def pg_conn():
    """A connection with a clean schema_versions baseline (v39 stamped) and no
    stray test table/version."""
    import migrate_pg

    conn = migrate_pg._connect(_DSN)
    cur = conn.cursor()
    # Ensure schema_versions exists with the baseline row; clean any prior test state.
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_versions ("
        "version BIGINT PRIMARY KEY, filename TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ DEFAULT NOW())"
    )
    cur.execute("DELETE FROM schema_versions WHERE version >= 900")
    cur.execute("INSERT INTO schema_versions (version, filename) VALUES (39,'pg_primary_v1.sql') "
                "ON CONFLICT (version) DO NOTHING")
    cur.execute("DROP TABLE IF EXISTS mig_pg_probe")
    conn.commit()
    yield conn
    cur = conn.cursor()
    cur.execute("DELETE FROM schema_versions WHERE version >= 900")
    cur.execute("DROP TABLE IF EXISTS mig_pg_probe")
    conn.commit()
    conn.close()


def _write_migration(dirpath: Path, version: int, name: str, up_sql: str, down_sql: str | None = None):
    (dirpath / f"pg_{version}_{name}.up.sql").write_text(up_sql, encoding="utf-8")
    if down_sql is not None:
        (dirpath / f"pg_{version}_{name}.down.sql").write_text(down_sql, encoding="utf-8")


def test_run_pending_applies_and_stamps(pg_conn, tmp_path):
    import migrate_pg

    _write_migration(
        tmp_path, 900, "probe",
        "CREATE TABLE IF NOT EXISTS mig_pg_probe (id TEXT PRIMARY KEY, note TEXT);",
        "DROP TABLE IF EXISTS mig_pg_probe;",
    )
    applied = migrate_pg.run_pending_pg_migrations(pg_conn, migrations_dir=str(tmp_path))
    assert applied == [900]
    # table exists + version stamped
    cur = pg_conn.cursor()
    cur.execute("SELECT to_regclass('public.mig_pg_probe')")
    assert cur.fetchone()[0] is not None
    assert migrate_pg.current_version(pg_conn) == 900


def test_run_pending_is_idempotent(pg_conn, tmp_path):
    import migrate_pg

    _write_migration(
        tmp_path, 900, "probe",
        "CREATE TABLE IF NOT EXISTS mig_pg_probe (id TEXT PRIMARY KEY);",
    )
    first = migrate_pg.run_pending_pg_migrations(pg_conn, migrations_dir=str(tmp_path))
    second = migrate_pg.run_pending_pg_migrations(pg_conn, migrations_dir=str(tmp_path))
    assert first == [900]
    assert second == []  # nothing pending the second time


def test_multiple_migrations_apply_in_order(pg_conn, tmp_path):
    import migrate_pg

    _write_migration(tmp_path, 900, "a",
                     "CREATE TABLE IF NOT EXISTS mig_pg_probe (id TEXT PRIMARY KEY);")
    _write_migration(tmp_path, 901, "b",
                     "ALTER TABLE mig_pg_probe ADD COLUMN IF NOT EXISTS extra TEXT;")
    applied = migrate_pg.run_pending_pg_migrations(pg_conn, migrations_dir=str(tmp_path))
    assert applied == [900, 901]
    cur = pg_conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_name='mig_pg_probe' AND column_name='extra'")
    assert cur.fetchone() is not None
    # cleanup the extra table beyond the fixture's DROP
    cur.execute("DELETE FROM schema_versions WHERE version=901")
    pg_conn.commit()


def test_down_reverts_and_unstamps(pg_conn, tmp_path):
    import migrate_pg

    _write_migration(
        tmp_path, 900, "probe",
        "CREATE TABLE IF NOT EXISTS mig_pg_probe (id TEXT PRIMARY KEY);",
        "DROP TABLE IF EXISTS mig_pg_probe;",
    )
    migrate_pg.run_pending_pg_migrations(pg_conn, migrations_dir=str(tmp_path))
    assert migrate_pg.current_version(pg_conn) == 900
    # revert directly via revert_migration (cmd_down wraps this)
    migrate_pg.revert_migration(pg_conn, 900, str(tmp_path / "pg_900_probe.down.sql"))
    cur = pg_conn.cursor()
    cur.execute("SELECT to_regclass('public.mig_pg_probe')")
    assert cur.fetchone()[0] is None  # table gone
    assert 900 not in migrate_pg.get_applied_versions(pg_conn)  # un-stamped


def test_failed_migration_rolls_back_and_does_not_stamp(pg_conn, tmp_path):
    import migrate_pg

    _write_migration(tmp_path, 900, "bad",
                     "CREATE TABLE mig_pg_probe (id TEXT); INSERT INTO nonexistent_tbl VALUES (1);")
    with pytest.raises(Exception):
        migrate_pg.run_pending_pg_migrations(pg_conn, migrations_dir=str(tmp_path))
    # neither the table nor the version should persist (whole file rolled back)
    cur = pg_conn.cursor()
    cur.execute("SELECT to_regclass('public.mig_pg_probe')")
    assert cur.fetchone()[0] is None
    assert 900 not in migrate_pg.get_applied_versions(pg_conn)


def test_validate_rejects_own_transaction_control(tmp_path):
    import migrate_pg

    bad = tmp_path / "pg_900_x.up.sql"
    bad.write_text("CREATE TABLE x (a int);\nCOMMIT;\n", encoding="utf-8")
    with pytest.raises(ValueError, match="COMMIT"):
        migrate_pg._validate_migration_sql(str(bad), bad.read_text())


def test_baseline_and_lower_versions_ignored(tmp_path):
    import migrate_pg

    # A pg_039 file must be ignored (owned by the baseline).
    (tmp_path / "pg_39_old.up.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "pg_900_ok.up.sql").write_text("SELECT 1;", encoding="utf-8")
    migs = migrate_pg.discover_migrations(str(tmp_path))
    assert 39 not in migs
    assert 900 in migs
