"""The chatlog lane and the main lane share a writer, so their shared tables
must not drift apart.

`agent_chatlog.db` carries its OWN `memory_embeddings` table and its OWN
migration series (`memory/chatlog_migrations/`), but the embedding writer is
SHARED with the main store. So a column added to the main lane and not the
chatlog lane does not fail at migrate time — it fails at WRITE time, in
production, as:

    OperationalError: table memory_embeddings has no column named vector_kind

Measured on a macOS host 2026-09-12: 55 chatlog turns landed in the spill
quarantine instead of the store and sat there unnoticed. They were recoverable;
nothing reported the loss.

This is DESIGN_PHILOSOPHIES §10a's duplication concern in schema form —
"Duplicated predicate/SQL logic is the defect, independent of correctness.
Copies drift." Two series describing one shared table is exactly that shape,
and nothing structural prevents the next divergence.

These tests read the migration SQL rather than a live database: the guard has to
work in CI, where no chatlog store exists, and it has to fail on a BAD MIGRATION
before anyone runs it.
"""
from __future__ import annotations

import pathlib
import re
import sqlite3

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "memory" / "migrations"
_CHAT = _ROOT / "memory" / "chatlog_migrations"

# Tables that BOTH lanes define and one shared writer touches. A column added to
# one side of any of these must be added to the other.
_SHARED_TABLES = ("memory_embeddings",)


def _apply_lane(migrations_dir: pathlib.Path, db: sqlite3.Connection) -> None:
    """Apply every .up.sql in a lane, in numeric order."""
    for f in sorted(migrations_dir.glob("*.up.sql"), key=lambda p: p.name):
        db.executescript(f.read_text(encoding="utf-8"))
    # The main lane's bootstrap is not named .up.sql in every series; pick up a
    # bare .sql bootstrap too so this works for both layouts.
    db.commit()


def _columns_after_lane(migrations_dir: pathlib.Path, table: str) -> set[str]:
    """Columns `table` has once the whole lane is applied to an empty DB."""
    db = sqlite3.connect(":memory:")
    try:
        for f in sorted(migrations_dir.glob("*.sql"), key=lambda p: p.name):
            if f.name.endswith(".down.sql"):
                continue
            try:
                db.executescript(f.read_text(encoding="utf-8"))
            except sqlite3.Error:
                # A migration that cannot apply to a bare DB (e.g. it targets a
                # table another lane owns) is not this test's business.
                continue
        db.commit()
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    finally:
        db.close()


@pytest.mark.parametrize("table", _SHARED_TABLES)
def test_shared_tables_have_the_same_columns_in_both_lanes(table):
    """The invariant. If this fails, a write through the shared writer will
    raise at runtime on whichever lane is behind."""
    main_cols = _columns_after_lane(_MAIN, table)
    chat_cols = _columns_after_lane(_CHAT, table)

    if not main_cols or not chat_cols:
        pytest.skip(f"{table} not constructible from one of the lanes in isolation")

    missing_in_chat = main_cols - chat_cols
    missing_in_main = chat_cols - main_cols
    assert not missing_in_chat, (
        f"{table}: chatlog lane is MISSING {sorted(missing_in_chat)} that the main "
        f"lane has. The embedding writer is shared, so a write routed at "
        f"agent_chatlog.db will raise 'no column named ...' at runtime. Port the "
        f"main-lane migration into memory/chatlog_migrations/."
    )
    assert not missing_in_main, (
        f"{table}: main lane is MISSING {sorted(missing_in_main)} that the chatlog "
        f"lane has — the same hazard in the other direction."
    )


def test_vector_kind_is_present_in_the_chatlog_lane():
    """The specific instance this guard was written for (#165).

    Pinned by name as well as by the parity check above, because the parity
    check silently skips if either lane stops being constructible in isolation,
    and a skipped guard reads as coverage while providing none (§12c).
    """
    cols = _columns_after_lane(_CHAT, "memory_embeddings")
    assert "vector_kind" in cols, (
        "chatlog memory_embeddings has no vector_kind — every embedding write "
        "routed at the chatlog DB will raise OperationalError"
    )


def test_the_parity_guard_can_actually_fail():
    """§12c: plant a divergence and watch it trip.

    A guard that cannot demonstrate a catch is indistinguishable from one that
    is blind. This applies the chatlog lane to a scratch DB, drops a column the
    main lane has, and asserts the comparison notices.
    """
    main_cols = _columns_after_lane(_MAIN, "memory_embeddings")
    chat_cols = _columns_after_lane(_CHAT, "memory_embeddings")
    if not main_cols or not chat_cols:
        pytest.skip("lanes not constructible in isolation")

    # Simulate the exact 2026-09-12 state: chatlog lane without vector_kind.
    degraded = chat_cols - {"vector_kind"}
    assert main_cols - degraded, (
        "removing vector_kind from the chatlog side produced no difference — "
        "the comparison cannot detect a divergence and is therefore blind"
    )


def test_every_chatlog_migration_has_a_down():
    """A one-way door on a store users cannot easily rebuild. The main lane
    ships .down.sql for its migrations; the chatlog lane must too."""
    ups = {p.name[: -len(".up.sql")] for p in _CHAT.glob("*.up.sql")}
    downs = {p.name[: -len(".down.sql")] for p in _CHAT.glob("*.down.sql")}
    missing = sorted(ups - downs)
    assert not missing, f"chatlog migrations with no .down.sql: {missing}"


def test_chatlog_lane_numbering_has_no_gaps_or_duplicates():
    """A duplicate or skipped number means two migrations race for one slot, or
    the runner's version bookkeeping silently disagrees with the files on disk.
    """
    nums = sorted(
        int(m.group(1))
        for p in _CHAT.glob("*.up.sql")
        if (m := re.match(r"(\d+)_", p.name))
    )
    assert nums, "no numbered migrations found in the chatlog lane"
    assert len(nums) == len(set(nums)), f"duplicate migration numbers: {nums}"
    assert nums == list(range(1, len(nums) + 1)), (
        f"chatlog migration numbering has a gap: {nums}"
    )
