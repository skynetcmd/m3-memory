"""Unit tests for migrate_pg.py — pure logic, NO database.

Covers discovery/ordering/validation and the DSN forbidden-host guard, which run
without a cluster (the live apply/revert contract is in test_migrate_pg_live.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

import migrate_pg  # noqa: E402


class TestDiscovery:
    def test_parses_up_and_down_and_orders(self, tmp_path):
        (tmp_path / "pg_40_a.up.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "pg_40_a.down.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "pg_41_b.up.sql").write_text("SELECT 1;", encoding="utf-8")
        migs = migrate_pg.discover_migrations(str(tmp_path))
        assert sorted(migs) == [40, 41]
        assert migs[40]["down"] is not None
        assert migs[41]["down"] is None
        assert migs[40]["name"] == "a"

    def test_ignores_baseline_and_non_matching(self, tmp_path):
        (tmp_path / "pg_primary_v1.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "pg_warehouse_chatlog_v1.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "pg_39_baseline.up.sql").write_text("SELECT 1;", encoding="utf-8")  # <= baseline
        (tmp_path / "040_sqlite_style.up.sql").write_text("SELECT 1;", encoding="utf-8")  # no pg_ prefix
        (tmp_path / "pg_40_ok.up.sql").write_text("SELECT 1;", encoding="utf-8")
        migs = migrate_pg.discover_migrations(str(tmp_path))
        assert list(migs) == [40]  # only the valid, > baseline, pg_-prefixed one

    def test_empty_dir(self, tmp_path):
        assert migrate_pg.discover_migrations(str(tmp_path)) == {}


class TestValidation:
    @pytest.mark.parametrize("bad", ["COMMIT;", "ROLLBACK;", "BEGIN;", "START TRANSACTION;"])
    def test_rejects_transaction_control(self, tmp_path, bad):
        f = tmp_path / "pg_40_x.up.sql"
        sql = f"CREATE TABLE x (a int);\n{bad}\n"
        f.write_text(sql, encoding="utf-8")
        with pytest.raises(ValueError):
            migrate_pg._validate_migration_sql(str(f), sql)

    def test_allows_those_words_in_comments(self, tmp_path):
        sql = "-- this migration does not COMMIT anything\nCREATE TABLE x (a int);\n"
        migrate_pg._validate_migration_sql("pg_40_x.up.sql", sql)  # must not raise

    def test_allows_clean_ddl(self):
        migrate_pg._validate_migration_sql(
            "pg_40_x.up.sql",
            "ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS foo TEXT;",
        )


class TestDsnGuard:
    def test_forbidden_host_refused(self, monkeypatch):
        monkeypatch.setenv("M3_PRIMARY_PG_URL", "postgresql://u:p@198.51.100.7:5432/db")
        monkeypatch.setenv("M3_PG_FORBIDDEN_HOSTS", "198.51.100.7")
        with pytest.raises(RuntimeError, match="forbidden host"):
            migrate_pg._resolve_dsn()

    def test_no_dsn_exits(self, monkeypatch):
        for v in ("M3_PRIMARY_PG_URL", "M3_PG_URL", "PG_URL"):
            monkeypatch.delenv(v, raising=False)
        with pytest.raises(SystemExit):
            migrate_pg._resolve_dsn()

    def test_never_reads_pg_url(self, monkeypatch):
        # PG_URL (the warehouse var) must not satisfy the primary runner.
        for v in ("M3_PRIMARY_PG_URL", "M3_PG_URL"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("PG_URL", "postgresql://warehouse/cdw")
        with pytest.raises(SystemExit):
            migrate_pg._resolve_dsn()

    def test_explicit_dsn_wins(self, monkeypatch):
        monkeypatch.delenv("M3_PG_FORBIDDEN_HOSTS", raising=False)
        assert migrate_pg._resolve_dsn("postgresql://x/y") == "postgresql://x/y"
