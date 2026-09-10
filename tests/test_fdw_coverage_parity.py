"""The FDW fast path must never sync LESS than the generic bridge.

`pg_fdw_sync` covered 3 tables while `pg_sync` covered 5, so a PostgreSQL
deployment with the fast path WORKING silently lost its `tasks` and its
`synchronized_secrets` vault. No error — those tables simply never moved, and
`sync_all` reported success because the fast path returned True.

That is the worst shape a sync bug can take: the fast path is the one that runs
when everything is healthy, so the failure only shows up as data that quietly
isn't there. These tests pin the parity so the gap cannot reopen the next time a
table is added to one path and not the other.

Hermetic: inspects the specs and the generated SQL. No cluster needed.
"""
from __future__ import annotations

import pathlib
import sys

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

import pg_fdw_sync as F  # noqa: E402

# The tables the GENERIC bridge syncs, derived from its sync_* functions rather
# than restated: a sixth sync_* function should surface here as a failure, not be
# silently absent.
_GENERIC_TABLES = {
    "memory_items",
    "memory_embeddings",
    "memory_relationships",
    "tasks",
    "synchronized_secrets",
}


class _RecordingCursor:
    rowcount = 0

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())


def _sql_for(table_name):
    for spec in F._TABLE_SPECS:
        table, cols, pk, ts_col = spec[:4]
        if table != table_name:
            continue
        guard = spec[4] if len(spec) > 4 else None
        cur = _RecordingCursor()
        F._upsert(cur, f"wh.{table}", f"public.{table}", cols, pk, ts_col,
                  "2026-01-01", guard)
        return cur.sql
    raise AssertionError(f"{table_name} not in _TABLE_SPECS")


def test_fdw_covers_what_the_generic_path_does():
    """THE test this file exists for.

    If a table is added to pg_sync and not to pg_fdw_sync, a PG deployment on the
    fast path stops syncing it — with no error anywhere.
    """
    fdw = {t[0] for t in F._TABLE_SPECS}
    missing = sorted(_GENERIC_TABLES - fdw)
    assert not missing, (
        "the FDW fast path does not sync these, but the generic bridge does — a "
        f"PostgreSQL deployment would silently lose them: {missing}"
    )


def test_generic_table_list_is_still_accurate():
    """Guards the guard: _GENERIC_TABLES above must match pg_sync's reality.

    A hand-maintained list of what the OTHER path syncs is itself drift-prone —
    exactly the defect that let the original gap exist. Derived from the sync_*
    function names so a new one shows up.
    """
    import re

    src = (_BIN / "pg_sync.py").read_text(encoding="utf-8")
    fns = set(re.findall(r"^def sync_([a-z_]+)\(", src, re.M))
    # sync_secrets operates on synchronized_secrets; the rest are table-named.
    derived = {"synchronized_secrets" if f == "secrets" else f for f in fns}
    assert derived == _GENERIC_TABLES, (
        "pg_sync's sync_* functions no longer match _GENERIC_TABLES — update this "
        f"list AND check the FDW specs. derived={sorted(derived)}"
    )


class TestPerTableConflictSemantics:
    """Each table keeps its OWN merge rule; a shared default would be wrong."""

    def test_items_and_tasks_use_timestamp_lww(self):
        for table in ("memory_items", "tasks"):
            sql = _sql_for(table)
            assert f"wh.{table}.updated_at < EXCLUDED.updated_at" in sql

    def test_secrets_uses_version_precedence_not_timestamp(self):
        """⚠ The one that would have been silently wrong.

        Adding secrets with ts_col="updated_at" would have given it last-write-wins
        and mis-merged the highest-value rows in the store. Version is
        authoritative; the timestamp only breaks ties WITHIN a version.
        """
        sql = _sql_for("synchronized_secrets")
        assert "EXCLUDED.version > wh.synchronized_secrets.version" in sql
        assert "EXCLUDED.version = wh.synchronized_secrets.version" in sql
        # And it must NOT be a bare timestamp comparison.
        assert "wh.synchronized_secrets.updated_at < EXCLUDED.updated_at" not in sql

    def test_secrets_is_not_delta_filtered_by_timestamp(self):
        """ts_col is None on purpose.

        A secret whose VERSION was bumped without updated_at moving must still
        propagate; a timestamp delta filter would skip it at the source, before
        the guard ever ran.
        """
        sql = _sql_for("synchronized_secrets")
        assert "FROM public.synchronized_secrets ON CONFLICT" in sql, (
            "no WHERE delta filter should sit between the source and ON CONFLICT"
        )

    def test_derived_tables_have_no_guard(self):
        """Embeddings and relationships carry no conflict predicate.

        An embedding is derived from its memory's content, so newest is always
        right; a relationship is immutable. A guard here could strand a row's
        vector a version behind its content.
        """
        for table in ("memory_embeddings", "memory_relationships"):
            sql = _sql_for(table)
            after_set = sql.split("DO UPDATE SET", 1)[1]
            assert "WHERE" not in after_set, f"{table} gained an unexpected guard"


def test_specs_may_carry_an_optional_guard_element():
    """The 5-tuple form must not break the 4-tuple ones."""
    lengths = {len(spec) for spec in F._TABLE_SPECS}
    assert lengths <= {4, 5}
    for spec in F._TABLE_SPECS:
        if len(spec) == 5:
            assert "{target}" in spec[4], (
                "a guard must use the {target} placeholder so it is qualified for "
                "whichever side is the target (push writes the foreign table, "
                "pull writes the local one)"
            )
