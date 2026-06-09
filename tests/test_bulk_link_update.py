"""Regression tests for memory_link_bulk and memory_update_bulk.

Both were introduced to give curation agents the same single→bulk speedup
that memory_delete_bulk delivers (memory 4090f663). Without these, an agent
that wants to add 50 LINK edges or update retention on 178 rows has to loop
the single-id variant, which adds ~2 sec of LLM round-trip latency per call.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid

import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)


# ── Test fixture: a tiny scratch DB with three memory items ──────────────────


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    """Build a minimal SQLite DB with the schema the bulk impls touch."""
    db_path = tmp_path / "scratch.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            type TEXT,
            title TEXT,
            content TEXT,
            metadata_json TEXT,
            importance REAL DEFAULT 0.5,
            refresh_on TEXT,
            refresh_reason TEXT,
            conversation_id TEXT,
            created_at TEXT,
            updated_at TEXT,
            is_deleted INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE memory_relationships (
            id TEXT PRIMARY KEY,
            from_id TEXT,
            to_id TEXT,
            relationship_type TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE memory_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT,
            action TEXT,
            old_value TEXT,
            new_value TEXT,
            field TEXT,
            change_agent TEXT,
            agent_id TEXT,
            timestamp TEXT
        )
    """)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for i, mid in enumerate(ids):
        conn.execute(
            "INSERT INTO memory_items (id, type, title, content, importance) VALUES (?,?,?,?,?)",
            (mid, "note", f"T{i}", f"content {i}", 0.5),
        )
    conn.commit()
    conn.close()

    # Point M3_DATABASE at the scratch DB so memory_core._db() uses it.
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    # Force a re-resolve of the cached db path in memory_core.
    import memory_core
    if hasattr(memory_core, "_initialized_dbs"):
        memory_core._initialized_dbs.clear()

    return {"path": str(db_path), "ids": ids}


# ── memory_link_bulk ─────────────────────────────────────────────────────────


def test_link_bulk_creates_valid_links(scratch_db):
    import memory_core
    a, b, c = scratch_db["ids"]
    r = memory_core.memory_link_bulk_impl([
        {"from_id": a, "to_id": b, "relationship_type": "related"},
        {"from_id": b, "to_id": c, "relationship_type": "supports"},
    ])
    assert len(r["created"]) == 2
    assert r["skipped_missing"] == []
    assert r["skipped_duplicate"] == []
    assert r["total"] == 2
    # Confirm the rows hit the DB
    conn = sqlite3.connect(scratch_db["path"])
    rows = conn.execute("SELECT COUNT(*) FROM memory_relationships").fetchone()[0]
    assert rows == 2


def test_link_bulk_skips_missing_target(scratch_db):
    import memory_core
    a = scratch_db["ids"][0]
    bogus = "00000000-0000-0000-0000-000000000000"
    r = memory_core.memory_link_bulk_impl([
        {"from_id": a, "to_id": bogus, "relationship_type": "related"},
    ])
    assert r["created"] == []
    assert len(r["skipped_missing"]) == 1
    assert bogus in r["skipped_missing"][0]["missing"]


def test_link_bulk_skips_duplicates(scratch_db):
    import memory_core
    a, b, _ = scratch_db["ids"]
    # First call creates one.
    memory_core.memory_link_bulk_impl([{"from_id": a, "to_id": b}])
    # Second call should report it as skipped_duplicate, not create another.
    r = memory_core.memory_link_bulk_impl([{"from_id": a, "to_id": b}])
    assert r["created"] == []
    assert len(r["skipped_duplicate"]) == 1
    assert r["skipped_duplicate"][0]["from_id"] == a


def test_link_bulk_dedupes_input(scratch_db):
    """Same (from, to, rel) appearing twice in the input list collapses
    to one DB row + one `created` entry."""
    import memory_core
    a, b, _ = scratch_db["ids"]
    r = memory_core.memory_link_bulk_impl([
        {"from_id": a, "to_id": b, "relationship_type": "related"},
        {"from_id": a, "to_id": b, "relationship_type": "related"},
    ])
    assert len(r["created"]) == 1
    assert r["total"] == 1, "input dedupe should reflect in `total`"


def test_link_bulk_invalid_relationship_type_surfaces(scratch_db):
    import memory_core
    a, b, _ = scratch_db["ids"]
    r = memory_core.memory_link_bulk_impl([
        {"from_id": a, "to_id": b, "relationship_type": "definitely-not-real"},
    ])
    assert r["created"] == []
    assert len(r["skipped_missing"]) == 1
    assert "invalid relationship_type" in r["skipped_missing"][0]["reason"]


# ── memory_update_bulk ──────────────────────────────────────────────────────


def test_update_bulk_sets_importance(scratch_db):
    import memory_core
    ids = scratch_db["ids"]
    r = memory_core.memory_update_bulk_impl([
        {"id": ids[0], "importance": 0.9},
        {"id": ids[1], "importance": 0.1},
    ])
    assert sorted(r["succeeded"]) == sorted([ids[0], ids[1]])
    assert r["not_found"] == []
    assert r["no_change"] == []

    conn = sqlite3.connect(scratch_db["path"])
    by_id = {row[0]: row[1] for row in conn.execute("SELECT id, importance FROM memory_items")}
    assert by_id[ids[0]] == 0.9
    assert by_id[ids[1]] == 0.1


def test_update_bulk_handles_missing_id(scratch_db):
    import memory_core
    bogus = "00000000-0000-0000-0000-000000000000"
    r = memory_core.memory_update_bulk_impl([
        {"id": bogus, "importance": 0.5},
    ])
    assert r["not_found"] == [bogus]
    assert r["succeeded"] == []


def test_update_bulk_no_change_when_all_fields_empty(scratch_db):
    """An update with `id` only (no fields to change) reports no_change."""
    import memory_core
    ids = scratch_db["ids"]
    r = memory_core.memory_update_bulk_impl([
        {"id": ids[0]},  # no fields → no change
    ])
    assert r["no_change"] == [ids[0]]
    assert r["succeeded"] == []


def test_update_bulk_dedupes_on_id_last_wins(scratch_db):
    """Two entries for the same id: last one wins (matches memory_update
    looped semantics if you called it twice — final state == last call)."""
    import memory_core
    ids = scratch_db["ids"]
    r = memory_core.memory_update_bulk_impl([
        {"id": ids[0], "importance": 0.3},
        {"id": ids[0], "importance": 0.7},
    ])
    assert r["total"] == 1
    assert ids[0] in r["succeeded"]
    conn = sqlite3.connect(scratch_db["path"])
    val = conn.execute("SELECT importance FROM memory_items WHERE id=?", (ids[0],)).fetchone()[0]
    assert val == 0.7


def test_update_bulk_metadata_string_accepted(scratch_db):
    import memory_core
    ids = scratch_db["ids"]
    md = json.dumps({"foo": "bar"})
    r = memory_core.memory_update_bulk_impl([
        {"id": ids[0], "metadata": md},
    ])
    assert ids[0] in r["succeeded"]
    conn = sqlite3.connect(scratch_db["path"])
    val = conn.execute("SELECT metadata_json FROM memory_items WHERE id=?", (ids[0],)).fetchone()[0]
    assert json.loads(val)["foo"] == "bar"


def test_update_bulk_metadata_dict_is_json_encoded(scratch_db):
    """If a caller passes metadata as a dict (Python-side), the impl JSON-
    encodes before writing — matches memory_update_impl behavior."""
    import memory_core
    ids = scratch_db["ids"]
    r = memory_core.memory_update_bulk_impl([
        {"id": ids[0], "metadata": {"foo": "baz"}},
    ])
    assert ids[0] in r["succeeded"]
    conn = sqlite3.connect(scratch_db["path"])
    val = conn.execute("SELECT metadata_json FROM memory_items WHERE id=?", (ids[0],)).fetchone()[0]
    assert json.loads(val)["foo"] == "baz"


# ── Catalog registration ─────────────────────────────────────────────────────


def test_both_tools_registered_in_catalog():
    import mcp_tool_catalog
    names = {t.name for t in mcp_tool_catalog.TOOLS}
    assert "memory_link_bulk" in names
    assert "memory_update_bulk" in names


def test_bulk_tools_have_required_args_declared():
    """Schema misconfig would let the agent call with no args and the impl
    would dispatch with an empty list — catch it at the schema level."""
    import mcp_tool_catalog
    by_name = {t.name: t for t in mcp_tool_catalog.TOOLS}
    assert "links" in by_name["memory_link_bulk"].parameters.get("required", [])
    assert "updates" in by_name["memory_update_bulk"].parameters.get("required", [])
