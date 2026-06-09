"""Regression tests for the curator_apply module.

Goal: one deterministic apply call per store, no LLM in the loop. Replaces
the agent-driven APPLY-mode loop that killed two background curator runs on
2026-05-17 — once for looping single-id memory_delete (16-min budget),
once for inventing a Bash-file-write strategy past its budget.

Test strategy: build a scratch SQLite DB with the schema the bulk impls
touch, feed a plan with every section populated, assert that exactly the
expected rows get modified and the result dict has the expected shape.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import uuid

import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)


# ── Fixture: scratch DB matching the schema bulk impls + apply needs ─────────


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
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
    conn.execute("""
        CREATE TABLE memory_embeddings (
            memory_id TEXT PRIMARY KEY,
            embedding BLOB,
            embed_model TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE chroma_sync_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT,
            operation TEXT
        )
    """)
    ids = [str(uuid.uuid4()) for _ in range(5)]
    for i, mid in enumerate(ids):
        conn.execute(
            "INSERT INTO memory_items (id, type, title, content, importance) VALUES (?,?,?,?,?)",
            (mid, "note", f"T{i}", f"content {i}", 0.5),
        )
    conn.commit()
    conn.close()

    monkeypatch.setenv("M3_DATABASE", str(db_path))
    import memory_core
    if hasattr(memory_core, "_initialized_dbs"):
        memory_core._initialized_dbs.clear()
    return {"path": str(db_path), "ids": ids}


# ── apply_memory_plan ────────────────────────────────────────────────────────


def test_memory_plan_empty_is_noop():
    """An empty plan returns zero counts and no errors."""
    import curator_apply
    out = curator_apply.apply_memory_plan({})
    assert out["store"] == "memory"
    assert out["summary"] == {"deleted_soft": 0, "deleted_hard": 0, "linked": 0, "updated": 0}
    assert out["errors"] == []
    assert out["wall_seconds"] >= 0


def test_memory_plan_combined_sections(scratch_db):
    """A plan with delete + link + update sections all populated runs every
    section, returns per-section results, and physically writes to the DB."""
    import curator_apply
    ids = scratch_db["ids"]
    plan = {
        "delete": [ids[0]],
        "link": [{"from_id": ids[1], "to_id": ids[2], "relationship_type": "related"}],
        "update": [{"id": ids[3], "importance": 0.9}],
    }
    out = curator_apply.apply_memory_plan(plan)

    assert out["errors"] == []
    assert out["delete"]["succeeded"] == [ids[0]]
    assert len(out["link"]["created"]) == 1
    assert ids[3] in out["update"]["succeeded"]
    assert out["summary"] == {
        "deleted_soft": 1, "deleted_hard": 0, "linked": 1, "updated": 1
    }

    # Verify the DB physically changed.
    conn = sqlite3.connect(scratch_db["path"])
    assert conn.execute(
        "SELECT is_deleted FROM memory_items WHERE id=?", (ids[0],)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_relationships WHERE from_id=? AND to_id=?",
        (ids[1], ids[2]),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT importance FROM memory_items WHERE id=?", (ids[3],)
    ).fetchone()[0] == 0.9


def test_memory_plan_hard_delete(scratch_db):
    """delete_hard cascades to memory_embeddings + memory_relationships."""
    import curator_apply
    ids = scratch_db["ids"]
    conn = sqlite3.connect(scratch_db["path"])
    conn.execute(
        "INSERT INTO memory_embeddings (memory_id, embedding, embed_model) VALUES (?,?,?)",
        (ids[0], b"\x00" * 16, "test"),
    )
    conn.commit()
    conn.close()

    plan = {"delete_hard": [ids[0]]}
    out = curator_apply.apply_memory_plan(plan)
    assert out["errors"] == []
    assert out["delete_hard"]["succeeded"] == [ids[0]]

    conn = sqlite3.connect(scratch_db["path"])
    # Item row gone (cascade), not just soft-deleted.
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_items WHERE id=?", (ids[0],)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id=?", (ids[0],)
    ).fetchone()[0] == 0


def test_memory_plan_string_ids_in_delete_section(scratch_db):
    """Plan with comma-separated string for `delete` is accepted (lenient
    parser; some callers serialize this way)."""
    import curator_apply
    ids = scratch_db["ids"]
    plan = {"delete": f"{ids[0]},{ids[1]}"}
    out = curator_apply.apply_memory_plan(plan)
    assert sorted(out["delete"]["succeeded"]) == sorted([ids[0], ids[1]])


def test_memory_plan_section_error_does_not_block_others(scratch_db, monkeypatch):
    """If one section's impl raises, the plan continues with the others.
    Error is reported in `errors`, not propagated."""
    import curator_apply
    import memory_core
    ids = scratch_db["ids"]

    def _broken(*a, **kw):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(memory_core, "memory_link_bulk_impl", _broken)

    plan = {
        "delete": [ids[0]],
        "link":   [{"from_id": ids[1], "to_id": ids[2]}],
    }
    out = curator_apply.apply_memory_plan(plan)

    # Delete still succeeded.
    assert out["delete"]["succeeded"] == [ids[0]]
    # Link failed but error is captured, not propagated.
    assert out["link"] is None
    assert any(e["section"] == "link" for e in out["errors"])


# ── apply_chatlog_plan ───────────────────────────────────────────────────────


def test_chatlog_plan_dedup_section(scratch_db):
    """A chatlog plan with one DEDUP group deletes every drop_id and
    surfaces a structured group-level result."""
    import curator_apply

    # Add chatlog-typed rows to the scratch DB.
    conn = sqlite3.connect(scratch_db["path"])
    chat_ids = [str(uuid.uuid4()) for _ in range(4)]
    for cid in chat_ids:
        conn.execute(
            "INSERT INTO memory_items (id, type, title, content) VALUES (?,?,?,?)",
            (cid, "chat_log", f"user@host:{cid[:6]}", "duplicate content"),
        )
    conn.commit()
    conn.close()

    plan = {
        "dedup": [
            {"keep_id": chat_ids[0], "drop_ids": [chat_ids[1], chat_ids[2], chat_ids[3]]},
        ],
    }
    out = curator_apply.apply_chatlog_plan(plan, db_path=scratch_db["path"])

    assert out["store"] == "chatlog"
    assert out["dedup"]["total_succeeded"] == 3
    assert out["dedup"]["total_not_found"] == 0
    assert out["summary"]["dedup_deleted"] == 3
    assert out["errors"] == []


def test_chatlog_plan_prune_section(scratch_db):
    """A PRUNE spec soft-deletes every chat_log row in the conversation."""
    import curator_apply

    conv_id = str(uuid.uuid4())
    conn = sqlite3.connect(scratch_db["path"])
    chat_ids = [str(uuid.uuid4()) for _ in range(3)]
    for cid in chat_ids:
        conn.execute(
            "INSERT INTO memory_items (id, type, title, conversation_id) VALUES (?,?,?,?)",
            (cid, "chat_log", "x", conv_id),
        )
    # Also add a non-chat_log row in the same conversation — must NOT be touched.
    other_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO memory_items (id, type, title, conversation_id) VALUES (?,?,?,?)",
        (other_id, "decision", "important", conv_id),
    )
    conn.commit()
    conn.close()

    plan = {"prune": [{"conversation_id": conv_id, "reason": "abandoned"}]}
    out = curator_apply.apply_chatlog_plan(plan, db_path=scratch_db["path"])

    assert out["summary"]["pruned"] == 3
    conn = sqlite3.connect(scratch_db["path"])
    # All 3 chat_log rows soft-deleted.
    n_deleted = conn.execute(
        "SELECT COUNT(*) FROM memory_items "
        "WHERE conversation_id=? AND type='chat_log' AND is_deleted=1",
        (conv_id,)
    ).fetchone()[0]
    assert n_deleted == 3
    # Non-chat_log row in same conversation NOT touched (type filter).
    other_deleted = conn.execute(
        "SELECT is_deleted FROM memory_items WHERE id=?", (other_id,)
    ).fetchone()[0]
    assert other_deleted == 0


def test_chatlog_plan_empty_is_noop():
    import curator_apply
    out = curator_apply.apply_chatlog_plan({})
    assert out["store"] == "chatlog"
    assert out["summary"] == {
        "decay_applied_writes": 0, "dedup_deleted": 0, "promoted": 0, "pruned": 0
    }
    assert out["errors"] == []


def test_chatlog_dedup_uses_active_database_for_routing():
    """Regression for the 2026-05-17 routing bug.

    When a chatlog plan's `dedup` section ran, it called
    memory_delete_bulk_impl WITHOUT first activating the chatlog DB
    path. `memory_core._db()` then resolved to the main memory.db
    (because M3_DATABASE points there), found none of the chatlog rows,
    and returned every drop_id as `not_found`. 486-id apply against the
    real chatlog DB silently no-op'd.

    Fix: wrap the bulk call in `with active_database(resolved_db): ...`.

    A full end-to-end test against a separate-layout (chatlog DB ≠ main
    DB) requires standing up the full m3 schema in two DBs and is gated
    by the template-DB conftest machinery — not worth duplicating here.
    Instead this is a source-level guard: assert curator_apply imports
    active_database and uses it inside the dedup branch.
    """
    import inspect

    import curator_apply

    src = inspect.getsource(curator_apply.apply_chatlog_plan)
    assert "active_database" in src, (
        "apply_chatlog_plan must import + use active_database to route "
        "bulk-delete to the chatlog DB. See the 2026-05-17 regression "
        "where every drop_id came back not_found because the bulk call "
        "ran against the main DB instead."
    )
    # And confirm it's specifically wrapped around the dedup branch, not
    # somewhere irrelevant. A loose check: `active_database(resolved_db)`
    # must appear between `dedup_groups` and the result-summary section.
    dedup_section_start = src.find("dedup_groups")
    summary_section_start = src.find("PROMOTE")
    assert dedup_section_start >= 0 and summary_section_start > dedup_section_start
    dedup_section = src[dedup_section_start:summary_section_start]
    assert "active_database(resolved_db)" in dedup_section, (
        "active_database(resolved_db) must wrap the dedup bulk-delete call, "
        "not just appear somewhere in the function."
    )


# ── catalog registration ─────────────────────────────────────────────────────


def test_curate_apply_tools_in_catalog():
    import mcp_tool_catalog
    names = {t.name for t in mcp_tool_catalog.TOOLS}
    assert "curate_memory_apply" in names
    assert "curate_chatlog_apply" in names


def test_curate_apply_tools_are_destructive_gated():
    """These do bulk writes / deletes; must be destructive-gated so the MCP
    proxy doesn't expose them by default."""
    import mcp_tool_catalog
    by_name = {t.name: t for t in mcp_tool_catalog.TOOLS}
    assert by_name["curate_memory_apply"].default_allowed is False
    assert by_name["curate_chatlog_apply"].default_allowed is False
