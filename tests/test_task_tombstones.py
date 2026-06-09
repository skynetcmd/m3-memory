import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory_core import (
    _db,
    task_create_impl,
    task_delete_impl,
    task_get_impl,
    task_list_impl,
    task_update_impl,
)

# ── Isolation: route every _db() call to a fresh temp DB ─────────────────────
# Without this, task_create_impl targets the production DB (or whatever
# M3_DATABASE resolves to at test time), which may be write-locked by the
# dashboard server or another process — causing sqlite3.OperationalError:
# database is locked inside _ensure_sync_tables.

@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """Per-test isolated main DB.

    Copies a pre-migrated template (built once per session by
    create_full_main_schema) into tmp_path, routes M3_DATABASE there,
    and clears the _initialized_dbs cache so _lazy_init re-runs against
    the new path. M3_SKIP_MIGRATIONS=1 prevents a second migrate_memory
    subprocess since the template is already at the latest version.
    """
    import memory.db as _mdb

    from conftest import create_full_main_schema

    db_path = tmp_path / "tombstone_test.db"
    create_full_main_schema(db_path)

    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")

    # Clear the per-path init cache so _lazy_init runs against the new temp DB.
    # M3Context.for_db() reads M3_DATABASE env dynamically each call, so no
    # additional cache invalidation is needed beyond _initialized_dbs.
    _mdb._initialized_dbs.discard(str(db_path))

    yield db_path

    # Teardown: evict temp path from cache to avoid cross-test bleed
    _mdb._initialized_dbs.discard(str(db_path))


# ── Helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_task():
    msg = task_create_impl(title="tombstone test task", created_by="pytest")
    task_id = msg.split(": ", 1)[1].strip()
    yield task_id
    # cleanup: hard delete via direct SQL regardless of state
    with _db() as db:
        db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def _row(task_id):
    with _db() as db:
        return db.execute(
            "SELECT state, deleted_at, updated_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_soft_delete_sets_tombstone_and_hides_from_list(fresh_task):
    task_id = fresh_task
    short = task_id[:8]

    result = task_delete_impl(task_id)
    assert "soft-deleted" in result

    row = _row(task_id)
    assert row["deleted_at"] is not None
    assert row["state"] == "pending"  # state unchanged

    listing = task_list_impl(limit=200)
    assert short not in listing

    get_result = task_get_impl(task_id)
    assert "not found" in get_result


def test_include_deleted_surfaces_tombstoned_task(fresh_task):
    task_id = fresh_task
    short = task_id[:8]
    task_delete_impl(task_id)

    listing = task_list_impl(limit=200, include_deleted=True)
    assert short in listing

    get_result = task_get_impl(task_id, include_deleted=True)
    assert "Deleted At:" in get_result
    assert "not deleted" not in get_result


def test_double_soft_delete_is_idempotent(fresh_task):
    task_id = fresh_task
    task_delete_impl(task_id)
    second = task_delete_impl(task_id)
    assert "already soft-deleted" in second


def test_hard_delete_requires_prior_soft_delete(fresh_task):
    task_id = fresh_task

    err = task_delete_impl(task_id, hard=True)
    assert "must be soft-deleted" in err

    task_delete_impl(task_id)
    ok = task_delete_impl(task_id, hard=True)
    assert "hard-deleted" in ok

    with _db() as db:
        row = db.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is None


def test_task_update_rejects_tombstoned_task(fresh_task):
    task_id = fresh_task
    task_delete_impl(task_id)

    err = task_update_impl(task_id, state="in_progress")
    assert "not found" in err


def test_soft_delete_bumps_updated_at(fresh_task):
    task_id = fresh_task
    before = _row(task_id)["updated_at"]
    task_delete_impl(task_id)
    after = _row(task_id)["updated_at"]
    assert after >= before
