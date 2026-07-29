"""The memory archive tombstone (SQLite migration 042 / pg_047).

Before this, the archive was a separate SQLite sidecar file whose `archived_items`
table was NEVER created, so _transfer_to_archive silently no-opped (its INSERT
raised `no such table` and the bare `except: return False` swallowed it). The
archive is now the `memory_archive` table IN the primary store, written through
the seam, so it works on both backends AND participates in GDPR erasure.

These tests run on the default SQLite backend (the template DB is built by running
all migrations, so it carries memory_archive). The PG path is exercised by
test_memory_maintenance_pg_live's retention/expiry tests, which call
_transfer_to_archive on a live PG store.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.fixture()
def main_db(tmp_path, monkeypatch):
    """A fresh migrated main DB wired as the active store, via the seam."""
    from conftest import create_full_main_schema
    db_path = tmp_path / "main.db"
    create_full_main_schema(str(db_path))
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")  # template already migrated
    monkeypatch.setenv("M3_CORE_RS_DISABLE", "1")
    # Force a re-resolve of the cached db path in memory_core (the scratch_db idiom).
    import memory_core
    if hasattr(memory_core, "_initialized_dbs"):
        memory_core._initialized_dbs.clear()
    return db_path


def _write(uid, **kw):
    from memory.write import memory_write_impl
    raw = asyncio.run(memory_write_impl(embed=False, source="test", user_id=uid, **kw))
    return str(raw).replace("Created: ", "").strip().strip('"').split()[0]


def _archive_rows(where_sql="", params=()):
    from memory.db import _db
    with _db() as db:
        return db.execute(f"SELECT * FROM memory_archive {where_sql}", params).fetchall()


def test_archive_actually_persists(main_db):
    """The bug fix: _transfer_to_archive now writes a real row (it used to no-op)."""
    import memory_maintenance as MM
    from memory.db import _db
    uid = f"arc-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", title="T", content="body text")
    with _db() as db:
        ok = MM._transfer_to_archive(mid, "expired", db)
        if hasattr(db, "commit"):
            db.commit()
    assert ok is True
    rows = _archive_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == mid
    assert r["content"] == "body text"
    assert r["archive_reason"] == "expired"
    assert r["user_id"] == uid  # captured for GDPR reach


def test_archive_is_idempotent_on_rerun(main_db):
    """Re-archiving the same id upserts (on the id PK), never duplicates or raises."""
    import memory_maintenance as MM
    from memory.db import _db
    uid = f"arc-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="c")
    with _db() as db:
        assert MM._transfer_to_archive(mid, "low_importance", db) is True
        assert MM._transfer_to_archive(mid, "retention_limit", db) is True  # re-archive
        if hasattr(db, "commit"):
            db.commit()
    rows = _archive_rows()
    assert len(rows) == 1                       # upsert, not a second row
    assert rows[0]["archive_reason"] == "retention_limit"  # latest reason wins


def test_archive_missing_item_returns_false(main_db):
    """A vanished source id archives nothing and returns False (no crash)."""
    import memory_maintenance as MM
    from memory.db import _db
    with _db() as db:
        assert MM._transfer_to_archive("does-not-exist", "expired", db) is False
    assert _archive_rows() == []


def test_gdpr_forget_erases_archived_rows(main_db):
    """GDPR erasure must reach the tombstone — an erased user's content cannot
    survive in memory_archive (the reason user_id is captured there)."""
    import memory_maintenance as MM
    from memory.db import _db
    uid = f"arc-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="personal data")
    with _db() as db:
        MM._transfer_to_archive(mid, "expired", db)
        if hasattr(db, "commit"):
            db.commit()
    assert len(_archive_rows("WHERE user_id = ?", (uid,))) == 1

    MM.gdpr_forget_impl(uid)

    # Both the live item AND the archived tombstone are gone.
    assert _archive_rows("WHERE user_id = ?", (uid,)) == []
