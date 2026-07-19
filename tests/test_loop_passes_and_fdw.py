"""Unit tests for the governor-paced loop passes (_due gate) and the pg_fdw_sync
column contract. Pure-Python; no live PG or GPU.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


# ── _due interval gate (time-driven pass scheduling) ──────────────────────────
class TestDueGate:
    def setup_method(self):
        import m3_cognitive_loop as L
        self.L = L
        # Redirect the run-timestamp store to a temp file so tests don't touch the
        # real ~/.m3/config/.loop_pass_runs.json.
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w")
        self._tmp.write("{}")
        self._tmp.close()
        self._orig = L._loop_pass_runs_path
        L._loop_pass_runs_path = lambda: self._tmp.name

    def teardown_method(self):
        self.L._loop_pass_runs_path = self._orig
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_never_run_is_due(self):
        assert self.L._due("sync", 3600) is True

    def test_just_ran_is_not_due(self):
        self.L._record_pass_run("sync")
        assert self.L._due("sync", 3600) is False

    def test_zero_interval_is_always_due(self):
        self.L._record_pass_run("sync")
        assert self.L._due("sync", 0) is True

    def test_corrupt_timestamp_fails_open(self):
        # A garbage stored timestamp must read as due (fail-open), never crash.
        import json
        with open(self._tmp.name, "w") as f:
            json.dump({"sync": "not-a-timestamp"}, f)
        assert self.L._due("sync", 3600) is True

    def test_missing_file_fails_open(self):
        os.unlink(self._tmp.name)
        assert self.L._due("audit", 7 * 86400) is True
        # and recording re-creates the file
        self.L._record_pass_run("audit")
        assert self.L._due("audit", 7 * 86400) is False

    def test_distinct_passes_independent(self):
        self.L._record_pass_run("sync")
        assert self.L._due("sync", 3600) is False
        assert self.L._due("maintenance", 3600) is True  # never ran


# ── pg_fdw_sync column contract (schema-verified shared columns) ──────────────
class TestFdwContract:
    def test_table_specs_use_verified_columns(self):
        import pg_fdw_sync as F
        specs = {t: (cols, pk, ts) for t, cols, pk, ts in F._TABLE_SPECS}
        # memory_relationships uses the REAL column names (from_id/to_id), not the
        # guessed source_id/target_id that broke the first prototype.
        rel_cols = specs["memory_relationships"][0]
        assert "from_id" in rel_cols and "to_id" in rel_cols
        assert "source_id" not in rel_cols and "target_id" not in rel_cols
        # memory_embeddings includes vector_kind (was missing in the first draft).
        assert "vector_kind" in specs["memory_embeddings"][0]
        # memory_items has updated_at as its delta/last-writer timestamp.
        assert specs["memory_items"][2] == "updated_at"
        # embeddings/relationships have no shared updated_at -> ts_col None.
        assert specs["memory_embeddings"][2] is None
        assert specs["memory_relationships"][2] is None

    def test_upsert_sql_is_column_explicit_with_conflict_guard(self):
        import pg_fdw_sync as F

        captured = {}

        class _Cur:
            def execute(self, sql, params=None):
                captured["sql"] = sql
                captured["params"] = params
            rowcount = 3

        # ts_col present -> delta WHERE + last-writer-wins guard
        F._upsert(_Cur(), "cdw_fdw.memory_items", "public.memory_items",
                  ["id", "content", "updated_at"], "id", "updated_at", "2026-01-01")
        sql = captured["sql"]
        assert "INSERT INTO cdw_fdw.memory_items" in sql
        assert "SELECT id, content, updated_at FROM public.memory_items" in sql
        assert "updated_at > %s" in sql  # delta
        assert "ON CONFLICT (id) DO UPDATE" in sql
        assert "cdw_fdw.memory_items.updated_at < EXCLUDED.updated_at" in sql

    def test_upsert_without_ts_has_no_delta_or_guard(self):
        import pg_fdw_sync as F

        captured = {}

        class _Cur:
            def execute(self, sql, params=None):
                captured["sql"] = sql
            rowcount = 0

        F._upsert(_Cur(), "cdw_fdw.memory_embeddings", "public.memory_embeddings",
                  ["id", "embedding"], "id", None, None)
        sql = captured["sql"]
        assert "WHERE" not in sql.split("ON CONFLICT")[0]  # no delta filter
        assert "DO UPDATE SET" in sql
        # no last-writer guard clause after the SET (nothing to compare)
        assert "EXCLUDED.updated_at" not in sql


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
