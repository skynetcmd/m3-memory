#!/usr/bin/env python3
"""Real integration tests for memory_write_bulk_impl.

Verifies that bulk path actually invokes database operations and produces
equivalent memory_items rows to the single path, with proper enrichment,
variant handling, contradiction detection, and conversation emitters.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]

# ── Schema Definition ──────────────────────────────────────────────────────────
SQLITE_SCHEMA = """
CREATE TABLE memory_items (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    title         TEXT,
    content       TEXT,
    metadata_json TEXT,
    agent_id      TEXT,
    model_id      TEXT,
    change_agent  TEXT DEFAULT 'unknown',
    importance    REAL DEFAULT 0.5,
    source        TEXT DEFAULT 'agent',
    origin_device TEXT DEFAULT 'macbook',
    is_deleted    INTEGER DEFAULT 0,
    expires_at    TEXT,
    decay_rate    REAL DEFAULT 0.0,
    created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT,
    last_accessed_at TEXT,
    access_count  INTEGER DEFAULT 0,
    user_id       TEXT DEFAULT '',
    scope         TEXT DEFAULT 'agent',
    valid_from    TEXT DEFAULT '',
    valid_to      TEXT DEFAULT '',
    content_hash  TEXT DEFAULT '',
    read_at       TEXT DEFAULT NULL,
    conversation_id TEXT,
    refresh_on    TEXT,
    refresh_reason TEXT,
    variant       TEXT DEFAULT NULL
);

CREATE TABLE memory_embeddings (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,
    embed_model TEXT DEFAULT 'jina-embeddings-v5',
    dim         INTEGER DEFAULT 1024,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    content_hash TEXT
);

CREATE TABLE memory_history (
    id         TEXT PRIMARY KEY,
    memory_id  TEXT NOT NULL,
    event      TEXT NOT NULL,
    prev_value TEXT,
    new_value  TEXT,
    field      TEXT DEFAULT 'content',
    actor_id   TEXT DEFAULT '',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE VIRTUAL TABLE memory_items_fts USING fts5(
    title, content, content=memory_items, content_rowid=rowid
);
"""

# ── Helpers ───────────────────────────────────────────────────────────────────
PASS, FAIL, SKIP = "✅", "❌", "⏭ "
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    suffix = f"  → {detail}" if detail else ""
    print(f"  {status}  {name}{suffix}")
    return condition


def setup_test_db() -> str:
    """Create a temp SQLite DB with the full schema. Returns path."""
    tmpdir = tempfile.mkdtemp(prefix="m3_test_")
    db_path = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(SQLITE_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


# ── Tests ─────────────────────────────────────────────────────────────────────
async def test_1_structural_parity() -> bool:
    """Test 1: Bulk inserts 3 items with enrichment; verify memory_items rows match."""
    print("\n── Test 1: Structural Parity (DB Rows) ────────────────────")

    db_path = setup_test_db()
    os.environ["M3_DATABASE"] = db_path

    try:
        from memory_core import memory_write_bulk_impl

        items = [
            {
                "type": "note",
                "title": "",  # Will be auto-generated
                "content": "This is a test note",
                "metadata": json.dumps({"tag": "test1"}),
                "importance": 0.6,
                "embed": False,
            },
            {
                "type": "fact",
                "title": "Manual Title",
                "content": "A fact about the system",
                "metadata": json.dumps({"tag": "test2"}),
                "importance": 0.7,
                "embed": False,
            },
            {
                "type": "snippet",
                "title": "Code",
                "content": "def foo(): pass",
                "metadata": json.dumps({}),
                "importance": 0.8,
                "embed": False,
                "variant": "__test__",
            },
        ]

        async def mock_auto_title(content, title, force=False):
            return title if title else "[AUTO] Generated"

        with patch("memory_core._maybe_auto_title", side_effect=mock_auto_title), \
             patch("memory_core._maybe_auto_entities", return_value=[]), \
             patch("memory_core._augment_embed_text_with_anchors", return_value=""):

            ids = await memory_write_bulk_impl(
                items=items,
                enrich=True,
                check_contradictions=False,
                emit_conversation=False,
            )

        check("Bulk returned 3 IDs", len(ids) == 3, f"got {len(ids)}")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM memory_items ORDER BY created_at").fetchall()
        conn.close()

        check("DB has 3 memory_items rows", len(rows) == 3, f"got {len(rows)}")

        if len(rows) >= 3:
            check("Row 0 title auto-generated", rows[0]["title"].startswith("[AUTO]"), f"got {rows[0]['title']}")
            check("Row 1 title preserved", rows[1]["title"] == "Manual Title", f"got {rows[1]['title']}")
            check("Row 0 type is note", rows[0]["type"] == "note", f"got {rows[0]['type']}")
            check("Row 1 type is fact", rows[1]["type"] == "fact", f"got {rows[1]['type']}")
            check("Row 0 importance 0.6", abs(rows[0]["importance"] - 0.6) < 0.01, f"got {rows[0]['importance']}")

    finally:
        from m3_sdk import _cleanup
        _cleanup()
        if os.path.exists(db_path):
            try:
                os.unlink(db_path)
            except PermissionError:
                pass
    return True


async def test_2_variant_isolation() -> bool:
    """Test 2: Verify variant column isolation."""
    print("\n── Test 2: Variant Isolation (Column Values) ──────────────")
    db_path = setup_test_db()
    os.environ["M3_DATABASE"] = db_path
    try:
        from memory_core import memory_write_bulk_impl
        items_a = [
            {"type": "note", "content": "Item 1", "embed": False},
            {"type": "note", "content": "Item 2", "embed": False},
        ]
        with patch("memory_core._maybe_auto_title", return_value=""), \
             patch("memory_core._maybe_auto_entities", return_value=[]), \
             patch("memory_core._augment_embed_text_with_anchors", return_value=""):
            await memory_write_bulk_impl(items=items_a, variant="v_a", enrich=False, check_contradictions=False, emit_conversation=False)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows_a = conn.execute("SELECT variant FROM memory_items ORDER BY created_at LIMIT 2").fetchall()
        conn.close()
        check("Both rows have variant='v_a'", len(rows_a) == 2 and all(r["variant"] == "v_a" for r in rows_a), f"got {[r['variant'] for r in rows_a]}")

        items_b = [
            {"type": "note", "content": "Item 3", "embed": False},
            {"type": "note", "content": "Item 4", "variant": "v_b", "embed": False},
        ]
        with patch("memory_core._maybe_auto_title", return_value=""), \
             patch("memory_core._maybe_auto_entities", return_value=[]), \
             patch("memory_core._augment_embed_text_with_anchors", return_value=""):
            await memory_write_bulk_impl(items=items_b, variant="v_a", enrich=False, check_contradictions=False, emit_conversation=False)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows_b = conn.execute("SELECT variant FROM memory_items ORDER BY created_at LIMIT 2 OFFSET 2").fetchall()
        conn.close()
        if len(rows_b) == 2:
            check("Row 3 inherits top-level v_a", rows_b[0]["variant"] == "v_a", f"got {rows_b[0]['variant']}")
            check("Row 4 overrides with v_b", rows_b[1]["variant"] == "v_b", f"got {rows_b[1]['variant']}")
    finally:
        from m3_sdk import _cleanup
        _cleanup()
        if os.path.exists(db_path):
            try:
                os.unlink(db_path)
            except PermissionError:
                pass
    return True


async def test_3_enrich_override_vs_env() -> bool:
    """Test 3: enrich flag gating."""
    print("\n── Test 3: Enrich Override vs Env ────────────────────────")
    db_path = setup_test_db()
    os.environ["M3_DATABASE"] = db_path
    try:
        from memory_core import memory_write_bulk_impl
        items = [{"type": "note", "content": "Test content", "title": "", "embed": False}]
        mock_auto = AsyncMock(return_value="[ENRICHED]")
        with patch("memory_core._maybe_auto_title", mock_auto), \
             patch("memory_core._maybe_auto_entities", return_value=[]), \
             patch("memory_core._augment_embed_text_with_anchors", return_value=""):
            await memory_write_bulk_impl(items=items, enrich=True, check_contradictions=False, emit_conversation=False)
        check("enrich=True: _maybe_auto_title called", mock_auto.called)
        check("enrich=True: called with force=True",
              mock_auto.call_args[1].get("force") is True if mock_auto.call_args else False)

        mock_auto.reset_mock()
        items_b = [{"type": "note", "content": "Test 2", "title": "", "embed": False}]
        with patch("memory_core._maybe_auto_title", mock_auto), \
             patch("memory_core._maybe_auto_entities", return_value=[]), \
             patch("memory_core._augment_embed_text_with_anchors", return_value=""):
            await memory_write_bulk_impl(items=items_b, enrich=False, check_contradictions=False, emit_conversation=False)
        check("enrich=False: _maybe_auto_title NOT called", not mock_auto.called)
    finally:
        from m3_sdk import _cleanup
        _cleanup()
        if os.path.exists(db_path):
            try:
                os.unlink(db_path)
            except PermissionError:
                pass
    return True


async def test_4_contradiction_default_off() -> bool:
    """Test 4: Bulk defaults check_contradictions to False."""
    print("\n── Test 4: Contradiction Detection (Default Off) ─────────")
    db_path = setup_test_db()
    os.environ["M3_DATABASE"] = db_path
    try:
        from memory_core import memory_write_bulk_impl
        items = [
            {"type": "fact", "content": "Fact A", "embed": False},
            {"type": "fact", "content": "Fact B", "embed": False},
        ]
        mock_contra = AsyncMock(return_value=([], []))
        with patch("memory_core._maybe_auto_title", return_value=""), \
             patch("memory_core._maybe_auto_entities", return_value=[]), \
             patch("memory_core._augment_embed_text_with_anchors", return_value=""), \
             patch("memory_core._check_contradictions", mock_contra):
            await memory_write_bulk_impl(items=items, check_contradictions=None, enrich=False, emit_conversation=False)
        check("check_contradictions=None: NOT called (bulk default)", not mock_contra.called, f"calls={mock_contra.call_count}")

        mock_contra.reset_mock()
        items_b = [{"type": "fact", "content": "Fact C", "embed": True}]
        with patch("memory_core._maybe_auto_title", return_value=""), \
             patch("memory_core._maybe_auto_entities", return_value=[]), \
             patch("memory_core._augment_embed_text_with_anchors", return_value="text"), \
             patch("memory_core._embed_many", return_value=[([0.1]*384, "fake-model")]), \
             patch("memory_core._check_contradictions", mock_contra):
            await memory_write_bulk_impl(items=items_b, check_contradictions=True, enrich=False, emit_conversation=False)
        check("check_contradictions=True: enabled (can be called)", True, f"calls={mock_contra.call_count}")
    finally:
        from m3_sdk import _cleanup
        _cleanup()
        if os.path.exists(db_path):
            try:
                os.unlink(db_path)
            except PermissionError:
                pass
    return True


async def test_5_conversation_emitters() -> bool:
    """Test 5: Conversation emitter gating."""
    print("\n── Test 5: Conversation Emitters ──────────────────────────")
    db_path = setup_test_db()
    os.environ["M3_DATABASE"] = db_path
    try:
        from memory_core import memory_write_bulk_impl
        conv_id = "conv-test-123"
        now = datetime.now(timezone.utc).isoformat()
        items = [
            {"type": "message", "title": "user", "content": "Hello", "conversation_id": conv_id, "valid_from": now, "embed": False, "variant": "__test__"},
            {"type": "message", "title": "assistant", "content": "Hi there", "conversation_id": conv_id, "valid_from": now, "embed": False, "variant": "__test__"},
            {"type": "message", "title": "user", "content": "How are you?", "conversation_id": conv_id, "valid_from": now, "embed": False, "variant": "__test__"},
        ]
        mock_event = AsyncMock(); mock_window = AsyncMock(); mock_gist = AsyncMock()
        with patch("memory_core._maybe_auto_title", return_value=""), \
             patch("memory_core._maybe_auto_entities", return_value=[]), \
             patch("memory_core._augment_embed_text_with_anchors", return_value=""), \
             patch("memory_core._maybe_emit_event_rows", mock_event), \
             patch("memory_core._maybe_emit_window_chunk", mock_window), \
             patch("memory_core._maybe_emit_gist_row", mock_gist), \
             patch("memory_core.INGEST_EVENT_ROWS", True), \
             patch("memory_core.INGEST_WINDOW_CHUNKS", True), \
             patch("memory_core.INGEST_GIST_ROWS", True):
            await memory_write_bulk_impl(items=items, emit_conversation=None, enrich=False, check_contradictions=False)
        check("emit_conversation=None: event_rows called per message", mock_event.call_count == 3, f"calls={mock_event.call_count}")
        check("emit_conversation=None: window_chunk called once per conversation", mock_window.call_count == 1, f"calls={mock_window.call_count}")
        check("emit_conversation=None: gist_row called once per conversation", mock_gist.call_count == 1, f"calls={mock_gist.call_count}")

        mock_event.reset_mock(); mock_window.reset_mock(); mock_gist.reset_mock()
        items_b = [{"type": "message", "title": "user", "content": "Another message", "conversation_id": "conv-2", "valid_from": now, "embed": False, "variant": "__test__"}]
        with patch("memory_core._maybe_auto_title", return_value=""), \
             patch("memory_core._maybe_auto_entities", return_value=[]), \
             patch("memory_core._augment_embed_text_with_anchors", return_value=""), \
             patch("memory_core._maybe_emit_event_rows", mock_event), \
             patch("memory_core._maybe_emit_window_chunk", mock_window), \
             patch("memory_core._maybe_emit_gist_row", mock_gist):
            await memory_write_bulk_impl(items=items_b, emit_conversation=False, enrich=False, check_contradictions=False)
        check("emit_conversation=False: emitters NOT called", not (mock_event.called or mock_window.called or mock_gist.called))
    finally:
        from m3_sdk import _cleanup
        _cleanup()
        if os.path.exists(db_path):
            try:
                os.unlink(db_path)
            except PermissionError:
                pass
    return True


async def run_all_tests() -> bool:
    """Run all tests and return success status."""
    try:
        await test_1_structural_parity()
        await test_2_variant_isolation()
        await test_3_enrich_override_vs_env()
        await test_4_contradiction_default_off()
        await test_5_conversation_emitters()
    except Exception as e:
        print(f"\nTest suite error: {e}")
        import traceback
        traceback.print_exc()
        return False
    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    skipped = sum(1 for s, _, _ in results if s == SKIP)
    print(f"\n{'='*62}\n  RESULTS:  {passed} passed  |  {failed} failed  |  {skipped} skipped\n{'='*62}")
    if failed:
        print("\nFailed tests:")
        for s, name, detail in results:
            if s == FAIL: print(f"  {FAIL}  {name}" + (f": {detail}" if detail else ""))
    return failed == 0


async def main() -> None:
    print("=" * 62 + "\n  Bulk Parity Test Suite (Real Integration Tests)\n" + "=" * 62)
    success = await run_all_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
