"""Pins the receipt/consumption split on `notifications` (issue #151).

The agent-to-agent SLA is "acknowledge RECEIPT within 30 seconds". Receipt does
not need an agent turn -- a plain subprocess round-trips in ~430 ms -- so a
transport waiter can satisfy it. But `notifications` carried ONE timestamp,
``read_at``, and acking from the transport marks a message read that no agent
has read. The unread flag was the only record that the work was still pending.

The usual reassurance, "the task state machine covers it", was measured rather
than assumed and is false for real traffic: 29 of 30 recent notifications
carried no ``task_id``, so nothing else recorded the message as outstanding.

Migrations 045 (SQLite) / pg_054 (PostgreSQL) add ``received_at`` so the two
events are recorded separately. The invariant every test here defends:

    stamping receipt must NEVER consume the unread flag.

``_db()`` is the seam; these tests never open a database file directly, and
never hardcode a placeholder -- ``dialect().param()`` renders it, so the same
test body exercises SQLite and PostgreSQL (DESIGN_PHILOSOPHIES 10a).
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS = _ROOT / "memory" / "migrations"

_AGENT = "pytest-received-at-probe"


# ── the migration itself, against a scratch DB ───────────────────────────────

def _apply(db: sqlite3.Connection, name: str) -> None:
    db.executescript((_MIGRATIONS / name).read_text(encoding="utf-8"))


@pytest.fixture
def scratch_db() -> sqlite3.Connection:
    """A throwaway in-memory DB with the 012 baseline applied.

    Deliberately NOT the engine root. An uncommitted migration in the working
    tree auto-applies to the live store on the next m3 CLI call, so migration
    tests that point at real state are how a development-time ALTER reaches
    production data.
    """
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    _apply(db, "012_orchestration.sql")
    return db


def test_up_adds_the_column(scratch_db):
    _apply(scratch_db, "045_notification_received_at.up.sql")
    cols = [r[1] for r in scratch_db.execute("PRAGMA table_info(notifications)")]
    assert "received_at" in cols
    assert "read_at" in cols, "the existing column must survive"


def test_down_removes_it_again(scratch_db):
    _apply(scratch_db, "045_notification_received_at.up.sql")
    _apply(scratch_db, "045_notification_received_at.down.sql")
    cols = [r[1] for r in scratch_db.execute("PRAGMA table_info(notifications)")]
    assert "received_at" not in cols
    assert "read_at" in cols, "rollback must not take read_at with it"


def test_existing_rows_are_not_backfilled(scratch_db):
    """A pre-existing row's receipt time is genuinely unknown.

    Writing ``created_at`` into it would be a guess that later reads could not
    distinguish from a real measurement (DESIGN_PHILOSOPHIES 12c). NULL means
    "we do not know", which is true.
    """
    scratch_db.execute(
        "INSERT INTO notifications (agent_id, kind, payload_json, created_at) "
        "VALUES ('a','k','{}','2026-09-12T00:00:00Z')"
    )
    _apply(scratch_db, "045_notification_received_at.up.sql")
    row = scratch_db.execute("SELECT received_at FROM notifications").fetchone()
    assert row["received_at"] is None


def test_migration_carries_a_down_file():
    """044 and 014 both ship one; a column add with no rollback is a one-way
    door on a store users cannot easily rebuild."""
    assert (_MIGRATIONS / "045_notification_received_at.down.sql").is_file()


# ── the PostgreSQL half ──────────────────────────────────────────────────────

_PG_UP = _MIGRATIONS / "postgres" / "pg_054_notification_received_at.up.sql"
_PG_DOWN = _MIGRATIONS / "postgres" / "pg_054_notification_received_at.down.sql"


def test_postgres_variant_exists():
    """m3 supports >=2 backends by contract. A SQLite-only migration silently
    leaves PG deployments without the column, and the impl would then fail at
    runtime rather than at migrate time."""
    assert _PG_UP.is_file(), "no PostgreSQL counterpart to migration 045"
    assert _PG_DOWN.is_file(), "no PostgreSQL rollback"


def test_postgres_variant_uses_native_types_not_sqlite_ones():
    """read_at is TIMESTAMPTZ in pg_primary_v1.sql, so received_at must match.
    Declaring it TEXT would "work" and then sort and compare wrongly."""
    src = _PG_UP.read_text(encoding="utf-8")
    assert "TIMESTAMPTZ" in src
    for sqlite_ism in ("AUTOINCREMENT", "strftime(", "INTEGER PRIMARY KEY"):
        assert sqlite_ism not in src, f"SQLite-ism {sqlite_ism!r} in a PG migration"


def test_postgres_baseline_declares_the_column():
    """pg_primary_v1.sql is applied ONCE to a fresh PG deployment, so a fresh
    install must get the column from the baseline rather than relying on the
    migration having run."""
    base = (_MIGRATIONS / "postgres" / "pg_primary_v1.sql").read_text(encoding="utf-8")
    notif = base[base.index("CREATE TABLE IF NOT EXISTS notifications"):]
    notif = notif[:notif.index(");")]
    assert "received_at" in notif


def test_sqlite_baseline_does_NOT_declare_the_column():
    """The mirror-image of the test above, and NOT an oversight.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``. If 012 declared received_at,
    a fresh install would create it there and then 045's ALTER would abort with
    "duplicate column name", breaking every new deployment. PG can declare it in
    both places precisely because its ADD COLUMN is guarded. Verified by
    execution, not assumed.
    """
    base = (_MIGRATIONS / "012_orchestration.sql").read_text(encoding="utf-8")
    notif = base[base.index("CREATE TABLE IF NOT EXISTS notifications"):]
    notif = notif[:notif.index(");")]
    assert "received_at" not in notif, (
        "012 must not declare received_at -- 045's ALTER would then fail with "
        "'duplicate column name' on every fresh SQLite install"
    )


def test_fresh_sqlite_install_applies_both_in_order(scratch_db):
    """The regression the test above describes, actually executed."""
    _apply(scratch_db, "045_notification_received_at.up.sql")  # must not raise
    cols = [r[1] for r in scratch_db.execute("PRAGMA table_info(notifications)")]
    assert cols.count("received_at") == 1


# ── the invariant: receipt must not consume the unread flag ──────────────────

@pytest.fixture
def live_notifications():
    """Rows in the real store, cleaned up afterwards.

    Goes through the seam (``_db``/``dialect``) rather than sqlite3 directly, so
    this same body runs against PostgreSQL when M3_PRIMARY_PG_URL points at a
    throwaway cluster.
    """
    from memory.backends import dialect
    from memory.orchestration import _db

    def _cleanup():
        with _db() as db:
            p = dialect().param()
            db.execute(f"DELETE FROM notifications WHERE agent_id = {p}", (_AGENT,))

    _cleanup()
    yield _db
    _cleanup()


def test_mark_received_sets_received_at_and_leaves_read_at_alone(live_notifications):
    """THE invariant. Receipt is delivery confirmation, not consumption."""
    from memory.backends import dialect
    from memory.orchestration import (
        notifications_mark_received_impl,
        notify_impl,
    )

    notify_impl(_AGENT, "ping", "receipt probe")
    notifications_mark_received_impl(_AGENT)

    with live_notifications() as db:
        p = dialect().param()
        row = db.execute(
            f"SELECT read_at, received_at FROM notifications WHERE agent_id = {p}",
            (_AGENT,),
        ).fetchall()[0]

    assert row["received_at"] is not None, "receipt was not recorded"
    assert row["read_at"] is None, (
        "read_at was consumed by a transport-level receipt -- this is exactly "
        "the data loss issue #151 exists to prevent"
    )


def test_a_received_notification_is_still_unread(live_notifications):
    """The user-visible consequence: --unread_only must still return it."""
    from memory.orchestration import (
        notifications_mark_received_impl,
        notifications_poll_impl,
        notify_impl,
    )

    notify_impl(_AGENT, "ping", "still-unread probe")
    notifications_mark_received_impl(_AGENT)

    out = notifications_poll_impl(_AGENT, unread_only=True, limit=10)
    assert "(empty)" not in out, (
        "a received notification vanished from the unread inbox -- the inbox "
        "must stay the record of UNPROCESSED work"
    )
    assert "1 unread" in out, out


def test_mark_received_is_idempotent_and_keeps_the_first_receipt(live_notifications):
    """A waiter can fire repeatedly on one WAL change. Receipt must record the
    FIRST delivery, not the most recent poll -- that is the number a receipt SLA
    is measured against."""
    from memory.backends import dialect
    from memory.orchestration import notifications_mark_received_impl, notify_impl

    notify_impl(_AGENT, "ping", "idempotence probe")
    notifications_mark_received_impl(_AGENT)

    with live_notifications() as db:
        p = dialect().param()
        first = db.execute(
            f"SELECT received_at FROM notifications WHERE agent_id = {p}",
            (_AGENT,),
        ).fetchall()[0]["received_at"]

    out = notifications_mark_received_impl(_AGENT)
    assert "Marked 0" in out, f"second call re-stamped an already-received row: {out}"

    with live_notifications() as db:
        p = dialect().param()
        second = db.execute(
            f"SELECT received_at FROM notifications WHERE agent_id = {p}",
            (_AGENT,),
        ).fetchall()[0]["received_at"]

    assert first == second, "the original receipt time was overwritten"


def test_ack_still_sets_read_at(live_notifications):
    """The split must not break the path it is protecting: an explicit ack still
    marks the message read."""
    from memory.orchestration import (
        notifications_ack_all_impl,
        notifications_poll_impl,
        notify_impl,
    )

    notify_impl(_AGENT, "ping", "ack probe")
    notifications_ack_all_impl(_AGENT)

    out = notifications_poll_impl(_AGENT, unread_only=True, limit=10)
    assert "(empty)" in out, f"ack no longer clears the unread flag: {out}"


# ── the tool surface ─────────────────────────────────────────────────────────

def test_tool_is_registered_and_injects_the_agent_id():
    """`inject_agent_id=True` is the anti-spoofing guard: without it one agent
    could stamp receipt on another agent's inbox, falsely recording delivery of
    messages it never saw."""
    import mcp_tool_catalog

    spec = next(
        (t for t in mcp_tool_catalog.TOOLS if t.name == "notifications_mark_received"),
        None,
    )
    assert spec is not None, "notifications_mark_received is not in the catalog"
    assert spec.inject_agent_id is True, (
        "without inject_agent_id a caller could stamp another agent's inbox"
    )


def test_waiter_records_receipt_by_default_and_acks_only_on_opt_in():
    """Receipt is now safe, so it is the default. Acking still is not, so it
    stays behind --ack."""
    src = (_ROOT / "bin" / "m3_notification_waiter.py").read_text(encoding="utf-8")
    assert "notifications_mark_received" in src, (
        "the waiter must record receipt -- that is how the <30s SLA is met "
        "without an agent turn"
    )
    mark = src.index("notifications_mark_received")
    assert "if args.ack:" in src[mark:], (
        "receipt must be unconditional and the ack must remain gated after it"
    )
