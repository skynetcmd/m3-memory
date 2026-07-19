"""Unit tests for the warehouse consolidation tool and pg_sync timestamp
resilience — the session's sync-restore + migration work.

Pure-Python / no live PG needed: exercises the classification and validation
logic in isolation. A live-PG end-to-end test lives separately (requires_pg).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


# ── pg_sync timestamp resilience (_valid_ts semantics) ────────────────────────
# _valid_ts / _norm_ts are LOCAL functions inside sync_memory_items, so we test
# the exact predicate here (kept in lockstep with the source). The invariant:
# any value PostgreSQL timestamptz would reject must be coerced to NULL so one
# corrupt cell can't abort the whole batch.
def _valid_ts(v):
    from datetime import datetime
    if v is None:
        return True
    if v == "" or not isinstance(v, str):
        return v != ""
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


class TestTimestampResilience:
    def test_none_is_valid(self):
        assert _valid_ts(None) is True

    def test_empty_string_is_invalid(self):
        # The original bug: '' -> "invalid input syntax for timestamptz".
        assert _valid_ts("") is False

    def test_real_iso_timestamps_are_valid(self):
        for v in ("2026-07-19T04:05:51.509Z",
                  "2026-07-19 04:05:51+00:00",
                  "2026-07-19",
                  "2026-07-19T04:05:51"):
            assert _valid_ts(v) is True, v

    def test_out_of_range_date_is_invalid(self):
        # The real chat-data culprit: day 37 / month 13 don't exist. SQLite stored
        # them; PG's DatetimeFieldOverflow aborted the batch.
        assert _valid_ts("2024-07-37") is False
        assert _valid_ts("2024-13-01") is False

    def test_junk_is_invalid(self):
        assert _valid_ts("garbage") is False
        assert _valid_ts("not-a-date") is False

    def test_norm_positions_match_select_order(self):
        # Guard the position map: the SELECT column order in sync_memory_items
        # must keep expires_at/created_at/updated_at/valid_from/valid_to at the
        # positions _TS_POS assumes. If someone reorders the SELECT, this catches
        # it before a bad-timestamp row silently aborts sync again.
        import pathlib
        import re
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "bin" / "pg_sync.py").read_text(encoding="utf-8")
        m = re.search(r"_TS_POS\s*=\s*\(([^)]+)\)", src)
        assert m, "_TS_POS not found in pg_sync.py"
        positions = [int(x) for x in m.group(1).split(",") if x.strip()]
        assert positions == [12, 14, 15, 18, 19]


# ── warehouse migration tool: classification logic ────────────────────────────
class _FakeCursor:
    """Minimal cursor faking to_regclass + count() for the planner."""
    def __init__(self, present: dict):
        # present: {(schema, table): row_count} ; absent tables omitted
        self._present = present
        self._result = None

    def execute(self, sql, params=None):
        s = sql.strip()
        if s.startswith("SELECT to_regclass"):
            schema_table = params[0]
            schema, table = schema_table.split(".", 1)
            self._result = [(1 if (schema, table) in self._present else None,)]
        elif s.startswith("SELECT count(*)"):
            # "SELECT count(*) FROM {schema}.{table}"
            frm = s.split("FROM", 1)[1].strip()
            schema, table = frm.split(".", 1)
            self._result = [(self._present.get((schema, table), 0),)]
        else:
            self._result = [(None,)]

    def fetchone(self):
        return self._result[0]


class TestMigrationPlanner:
    def _plan(self, present, table):
        import migrate_warehouse_to_schema as mw
        return mw._plan_for(_FakeCursor(present), table)

    def test_warehouse_only_is_ok(self):
        p = self._plan({("m3_warehouse", "memory_items"): 110348}, "memory_items")
        assert p["action"] == "ok"
        assert p["public"] is None
        assert p["warehouse"] == 110348

    def test_public_only_is_move(self):
        p = self._plan({("public", "tasks"): 29}, "tasks")
        assert p["action"] == "move"
        assert p["public"] == 29
        assert p["warehouse"] is None

    def test_both_present_is_merge(self):
        p = self._plan(
            {("public", "synchronized_secrets"): 11,
             ("m3_warehouse", "synchronized_secrets"): 11},
            "synchronized_secrets")
        assert p["action"] == "merge"

    def test_neither_present_is_absent(self):
        p = self._plan({}, "gdpr_requests")
        assert p["action"] == "absent"

    def test_sync_watermarks_is_never_a_warehouse_table(self):
        # Watermarks are per-machine; the tool drops a warehouse-side copy and
        # never treats it as a table to keep.
        import migrate_warehouse_to_schema as mw
        assert "sync_watermarks" not in mw._WAREHOUSE_TABLES
        assert "sync_watermarks" in mw._DROP_IF_PRESENT


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
