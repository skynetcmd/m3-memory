"""Phase 1 integration tests — first-class `confidence` on the write path.

Verifies that memory_write_impl derives and stores a `confidence` value from
provenance (and the Observer SLM's per-observation confidence), that an explicit
confidence overrides derivation, and that the whole thing is ABSENCE-TOLERANT —
on a DB without the column (pre-migration-035), writes still succeed and just
skip confidence. The pure math itself is covered exhaustively in
test_confidence_math.py; here we test the wiring.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory import confidence as C  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")


def _full_db(db_path):
    from conftest import create_full_main_schema
    create_full_main_schema(db_path)


async def _write(db_path, monkeypatch, **kw):
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    from memory.write import memory_write_impl
    res = await memory_write_impl(embed=False, **kw)
    # memory_write_impl returns "Created: <uuid>" (or a dict in some paths).
    if isinstance(res, str):
        return res.split(":", 1)[1].strip() if ":" in res else res.strip()
    return res.get("id")


def _confidence_of(db_path, item_id):
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT confidence FROM memory_items WHERE id = ?", (item_id,)
        ).fetchone()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_user_source_gets_high_confidence(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    item_id = await _write(db, monkeypatch, type="fact", content="user said X", source="user")
    assert _confidence_of(db, item_id) == pytest.approx(C.USER_PRIOR)


@pytest.mark.asyncio
async def test_agent_assertion_gets_agent_prior(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    item_id = await _write(db, monkeypatch, type="fact", content="claude asserted Y",
                       change_agent="claude")
    assert _confidence_of(db, item_id) == pytest.approx(C.AGENT_PRIOR)


@pytest.mark.asyncio
async def test_observer_confidence_flows_from_metadata(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    item_id = await _write(db, monkeypatch, type="observation", content="obs",
                       metadata='{"confidence": 0.77}')
    # Observer's own confidence wins over the provenance prior.
    assert _confidence_of(db, item_id) == pytest.approx(0.77)


@pytest.mark.asyncio
async def test_explicit_confidence_overrides(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    item_id = await _write(db, monkeypatch, type="fact", content="pinned", source="user",
                       confidence=0.33)
    assert _confidence_of(db, item_id) == pytest.approx(0.33)


@pytest.mark.asyncio
async def test_default_derivation_is_neutral(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    item_id = await _write(db, monkeypatch, type="note", content="plain")
    # No useful provenance → NEUTRAL_PRIOR (== importance default), so retrieval
    # is unaffected for ordinary writes.
    assert _confidence_of(db, item_id) == pytest.approx(C.NEUTRAL_PRIOR)


@pytest.mark.asyncio
async def test_absence_tolerant_when_column_missing(monkeypatch, tmp_path):
    """On a DB without the confidence column, the write still succeeds (the
    guarded UPDATE swallows 'no such column') — proving 035-independence."""
    db = tmp_path / "t.db"
    _full_db(db)
    # Drop the column to simulate a pre-035 DB.
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_memory_items_confidence")
        conn.execute("ALTER TABLE memory_items DROP COLUMN confidence")
        conn.commit()
    item_id = await _write(db, monkeypatch, type="fact", content="still works", source="user")
    # Row exists; no confidence column to read, but the write did not raise.
    with sqlite3.connect(str(db)) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM memory_items WHERE id = ?", (item_id,)).fetchone()[0]
    assert cnt == 1
