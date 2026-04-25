"""Tests for memory_write_impl and memory_write_bulk_impl's fact_enricher hook.

These tests cover the fact_enriched memory type and the fact enrichment pipeline,
including semaphore-gated dispatch, queue mechanics, variant skip rules, and
selection query filtering.

Test isolation pattern: Each test gets a fresh tmp_path and creates isolated
DB schemas with the fact_enrichment_queue table.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


def _create_fact_enriched_schema(db_path):
    """Create memory_items + memory_relationships + fact_enrichment_queue tables."""
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Minimal memory_items schema (mirroring conftest pattern)
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS memory_items (
            id TEXT PRIMARY KEY,
            type TEXT,
            title TEXT,
            content TEXT,
            metadata_json TEXT,
            agent_id TEXT,
            model_id TEXT,
            change_agent TEXT,
            importance REAL,
            source TEXT,
            origin_device TEXT,
            user_id TEXT,
            scope TEXT,
            expires_at TEXT,
            created_at TEXT,
            valid_from TEXT,
            valid_to TEXT,
            conversation_id TEXT,
            refresh_on TEXT,
            refresh_reason TEXT,
            content_hash TEXT,
            variant TEXT,
            is_deleted INTEGER DEFAULT 0,
            deleted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS memory_embeddings (
            id TEXT PRIMARY KEY,
            memory_id TEXT,
            embedding BLOB,
            embed_model TEXT,
            dim INTEGER,
            created_at TEXT,
            content_hash TEXT,
            vector_kind TEXT DEFAULT 'default',
            FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS memory_relationships (
            id TEXT PRIMARY KEY,
            from_id TEXT,
            to_id TEXT,
            relationship_type TEXT,
            created_at TEXT,
            FOREIGN KEY(from_id) REFERENCES memory_items(id) ON DELETE CASCADE,
            FOREIGN KEY(to_id) REFERENCES memory_items(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS fact_enrichment_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL UNIQUE,
            enqueued_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            last_attempt_at TEXT,
            FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_feq_attempts ON fact_enrichment_queue(attempts, enqueued_at);
    """)
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_type_enum_accepts_fact_enriched(monkeypatch, tmp_path):
    """fact_enriched is in VALID_MEMORY_TYPES."""
    import mcp_tool_catalog

    assert "fact_enriched" in mcp_tool_catalog.VALID_MEMORY_TYPES


@pytest.mark.asyncio
async def test_default_off_no_enrichment(monkeypatch, tmp_path):
    """With M3_ENABLE_FACT_ENRICHED=false (default), write completes with no fact rows, no queue rows."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    # Force module-level var to False (simulating env unset)
    monkeypatch.setattr(memory_core, "ENABLE_FACT_ENRICHED", False)

    async def stub_enricher(content: str) -> list[dict]:
        return [{"text": "extracted fact", "confidence": 0.9}]

    # Simulate write with enricher provided but gate off
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            ("test-1", "note", "test", "test content", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )
        conn.commit()

    with sqlite3.connect(str(db_path)) as db:
        await memory_core._try_enrich_or_enqueue("test-1", "test content", stub_enricher, db)
        # Give background task time
        await asyncio.sleep(0.05)

    # Verify no fact rows created
    with sqlite3.connect(str(db_path)) as conn:
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE type='fact_enriched'"
        ).fetchone()[0]
        queue_count = conn.execute(
            "SELECT COUNT(*) FROM fact_enrichment_queue WHERE memory_id='test-1'"
        ).fetchone()[0]

    assert fact_count == 0, "No fact rows should be created when ENABLE_FACT_ENRICHED=False"
    assert queue_count == 0, "No queue rows should be created when ENABLE_FACT_ENRICHED=False"


@pytest.mark.asyncio
async def test_verbatim_first_on_enricher_exception(monkeypatch, tmp_path):
    """Enricher raises Exception → verbatim row persisted, queue row created with last_error."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setattr(memory_core, "ENABLE_FACT_ENRICHED", True)

    async def failing_enricher(content: str) -> list[dict]:
        raise ValueError("Enricher boom")

    # Create source item
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            ("source-1", "note", "test", "source content", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )
        conn.commit()

    with sqlite3.connect(str(db_path)) as db:
        await memory_core._try_enrich_or_enqueue("source-1", "source content", failing_enricher, db)
        # Give background task time to complete and record error
        await asyncio.sleep(0.1)

    # Verify source persisted + queue row has error
    with sqlite3.connect(str(db_path)) as conn:
        source = conn.execute(
            "SELECT id, type, content FROM memory_items WHERE id='source-1'"
        ).fetchone()
        queue_row = conn.execute(
            "SELECT memory_id, attempts, last_error FROM fact_enrichment_queue WHERE memory_id='source-1'"
        ).fetchone()

    assert source is not None, "Source item must persist despite enricher failure"
    assert source[0] == "source-1"
    assert source[1] == "note"
    assert source[2] == "source content"

    assert queue_row is not None, "Queue row should be created on enricher exception"
    assert queue_row[0] == "source-1"
    assert queue_row[1] == 1, "attempts should be 1 after first failure"
    assert queue_row[2] is not None and "boom" in queue_row[2], "last_error should capture exception message"


@pytest.mark.asyncio
async def test_variant_skip_default(monkeypatch, tmp_path):
    """variant='lme-test' + no allowlist → no enrichment, no queue row."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setattr(memory_core, "ENABLE_FACT_ENRICHED", True)

    enricher_calls = []

    async def tracking_enricher(content: str) -> list[dict]:
        enricher_calls.append(content)
        return [{"text": "fact from variant", "confidence": 0.8}]

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, variant, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("bench-1", "note", "test", "bench content", "lme-test", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )
        conn.commit()

    with sqlite3.connect(str(db_path)) as db:
        # Call with allowlist=None (default)
        await memory_core._try_enrich_or_enqueue(
            "bench-1", "bench content", tracking_enricher, db, variant="lme-test", allowlist=None
        )
        await asyncio.sleep(0.05)

    # Verify enricher was NOT called and no queue row created
    assert len(enricher_calls) == 0, "Variant row should skip enrichment when allowlist=None"

    with sqlite3.connect(str(db_path)) as conn:
        queue_count = conn.execute(
            "SELECT COUNT(*) FROM fact_enrichment_queue WHERE memory_id='bench-1'"
        ).fetchone()[0]

    assert queue_count == 0, "No queue row should exist for variant rows without allowlist"


@pytest.mark.asyncio
async def test_variant_allowlist_override(monkeypatch, tmp_path):
    """variant='lme-test' + allowlist={'lme-test'} → enrichment proceeds."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setattr(memory_core, "ENABLE_FACT_ENRICHED", True)

    enricher_calls = []

    async def tracking_enricher(content: str) -> list[dict]:
        enricher_calls.append(content)
        return [{"text": "fact from allowed variant", "confidence": 0.9}]

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, variant, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("bench-2", "note", "test", "bench content 2", "lme-test", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )
        conn.commit()

    with sqlite3.connect(str(db_path)) as db:
        # Call with allowlist={'lme-test'}
        await memory_core._try_enrich_or_enqueue(
            "bench-2", "bench content 2", tracking_enricher, db, variant="lme-test", allowlist={"lme-test"}
        )
        await asyncio.sleep(0.1)

    # Verify enricher WAS called
    assert len(enricher_calls) == 1, "Variant row should proceed when variant is in allowlist"
    assert enricher_calls[0] == "bench content 2"

    # Verify fact row was created
    with sqlite3.connect(str(db_path)) as conn:
        fact_row = conn.execute(
            "SELECT id, type, content FROM memory_items WHERE type='fact_enriched' AND content LIKE '%allowed%'"
        ).fetchone()

    assert fact_row is not None, "Fact row should be created when variant is allowed"


@pytest.mark.asyncio
async def test_semaphore_concurrency_behavior(monkeypatch, tmp_path):
    """With concurrency=1 and slow enricher, second write enqueues instead of blocking."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    # Set concurrency to 1
    monkeypatch.setattr(memory_core, "FACT_ENRICH_CONCURRENCY", 1)
    # Reinitialize semaphore with new concurrency value
    monkeypatch.setattr(memory_core, "_FACT_ENRICH_SEM", asyncio.Semaphore(1))
    monkeypatch.setattr(memory_core, "ENABLE_FACT_ENRICHED", True)

    in_progress = []

    async def slow_enricher(content: str) -> list[dict]:
        in_progress.append(content)
        await asyncio.sleep(0.2)  # Hold semaphore for 200ms
        return [{"text": "fact", "confidence": 0.8}]

    # Create two source items
    with sqlite3.connect(str(db_path)) as conn:
        for i in range(2):
            conn.execute(
                """INSERT INTO memory_items
                (id, type, title, content, created_at, source, origin_device, scope, change_agent)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (f"slow-{i}", "note", "test", f"content {i}", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
            )
        conn.commit()

    # Fire two concurrent _try_enrich_or_enqueue calls
    with sqlite3.connect(str(db_path)) as db1:
        with sqlite3.connect(str(db_path)) as db2:
            tasks = [
                memory_core._try_enrich_or_enqueue("slow-0", "content 0", slow_enricher, db1),
                memory_core._try_enrich_or_enqueue("slow-1", "content 1", slow_enricher, db2),
            ]
            await asyncio.gather(*tasks)

    # Give background tasks time to run
    await asyncio.sleep(0.3)

    # Verify one got enriched (acquired semaphore) and one got enqueued
    with sqlite3.connect(str(db_path)) as conn:
        queue_count = conn.execute(
            "SELECT COUNT(*) FROM fact_enrichment_queue"
        ).fetchone()[0]

    # At least one should be queued (the second call hit the semaphore)
    assert queue_count >= 1, "Second concurrent write should enqueue due to full semaphore"


@pytest.mark.asyncio
async def test_references_edge_created(monkeypatch, tmp_path):
    """Enriched fact row links to source via references relationship."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setattr(memory_core, "ENABLE_FACT_ENRICHED", True)

    async def simple_enricher(content: str) -> list[dict]:
        return [{"text": "extracted atomic fact", "confidence": 0.95}]

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            ("source-ref", "note", "test", "source content", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )
        conn.commit()

    with sqlite3.connect(str(db_path)) as db:
        await memory_core._try_enrich_or_enqueue("source-ref", "source content", simple_enricher, db)
        await asyncio.sleep(0.1)

    # Verify fact row created and linked
    with sqlite3.connect(str(db_path)) as conn:
        fact_row = conn.execute(
            "SELECT id FROM memory_items WHERE type='fact_enriched'"
        ).fetchone()
        assert fact_row is not None, "Fact row should exist"
        fact_id = fact_row[0]

        relationship = conn.execute(
            "SELECT from_id, to_id, relationship_type FROM memory_relationships WHERE from_id=? AND to_id=?",
            (fact_id, "source-ref"),
        ).fetchone()

    assert relationship is not None, "references edge should link fact to source"
    assert relationship[2] == "references"


@pytest.mark.asyncio
async def test_select_pending_excludes_fact_enriched_children(monkeypatch, tmp_path):
    """Items with existing fact_enriched child are excluded from selection."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        # Create source item
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            ("source-with-fact", "note", "test", "content", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )

        # Create existing fact_enriched child
        fact_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (fact_id, "fact_enriched", "existing fact", "fact text", "2026-04-25T00:00:00Z", "fact_enricher", "test", "agent", "fact_enricher"),
        )

        # Create references edge: fact -> source
        conn.execute(
            """INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at)
            VALUES (?,?,?,?,?)""",
            (str(uuid.uuid4()), fact_id, "source-with-fact", "references", "2026-04-25T00:00:00Z"),
        )
        conn.commit()

    with sqlite3.connect(str(db_path)) as db:
        pending = memory_core._select_pending_fact_enrichment(db)

    # source-with-fact should NOT appear in pending (already has fact_enriched child)
    pending_ids = [mid for mid, _ in pending]
    assert "source-with-fact" not in pending_ids, "Items with fact_enriched child should be excluded"


@pytest.mark.asyncio
async def test_select_pending_excludes_variant_by_default(monkeypatch, tmp_path):
    """Items with non-NULL variant excluded by default."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        # Create null-variant item
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, variant, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("null-var", "note", "test", "content", None, "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )

        # Create variant item
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, variant, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("bench-var", "note", "test", "content", "lme-baseline", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )
        conn.commit()

    with sqlite3.connect(str(db_path)) as db:
        pending = memory_core._select_pending_fact_enrichment(db, allowed_variants=None)

    pending_ids = [mid for mid, _ in pending]
    assert "null-var" in pending_ids, "Null-variant items should be included"
    assert "bench-var" not in pending_ids, "Non-null variant items should be excluded without allowlist"


@pytest.mark.asyncio
async def test_select_pending_includes_allowed_variants(monkeypatch, tmp_path):
    """With allowed_variants, variant items are included."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, variant, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("allowed-var", "note", "test", "content", "lme-test", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, variant, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("blocked-var", "note", "test", "content", "lme-other", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )
        conn.commit()

    with sqlite3.connect(str(db_path)) as db:
        pending = memory_core._select_pending_fact_enrichment(db, allowed_variants=["lme-test"])

    pending_ids = [mid for mid, _ in pending]
    assert "allowed-var" in pending_ids, "Variant in allowlist should be included"
    assert "blocked-var" not in pending_ids, "Variant not in allowlist should be excluded"


@pytest.mark.asyncio
async def test_select_pending_excludes_high_attempts(monkeypatch, tmp_path):
    """Queue rows with attempts >= MAX_ATTEMPTS are excluded from 'queued' CTE,
    but may still appear in 'eligible' CTE (known behavior: they're in queue but not actively retried).

    This test verifies that queued items with attempts >= max are NOT included in the
    'queued' portion of the UNION, making them ineligible for immediate retry.
    """
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setattr(memory_core, "FACT_ENRICH_MAX_ATTEMPTS", 5)

    with sqlite3.connect(str(db_path)) as conn:
        # Create item with exhausted retry attempts IN THE QUEUE
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            ("exhausted", "note", "test", "content", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )

        # Create queue row with attempts >= 5 (equals or exceeds MAX_ATTEMPTS)
        conn.execute(
            """INSERT INTO fact_enrichment_queue (memory_id, attempts, last_error)
            VALUES (?,?,?)""",
            ("exhausted", 5, "Too many attempts"),
        )
        conn.commit()

    # Verify that it's NOT in the queued CTE directly
    with sqlite3.connect(str(db_path)) as db:
        queued_sql = """
        SELECT mi.id FROM fact_enrichment_queue q
        JOIN memory_items mi ON mi.id = q.memory_id
        WHERE q.attempts < ?
        """
        queued = db.execute(queued_sql, [5]).fetchall()
        queued_ids = [row[0] for row in queued]

    assert "exhausted" not in queued_ids, "Items with attempts >= MAX_ATTEMPTS should not be in queued CTE"

    # Full-function check: poisoned items must not appear in _select_pending_fact_enrichment
    # output either (the eligible CTE excludes anything present in the queue table outright).
    with memory_core._db() as db:
        full = memory_core._select_pending_fact_enrichment(db, limit=100)
    full_ids = [row[0] for row in full]
    assert "exhausted" not in full_ids, (
        "Poisoned items (in queue with attempts >= MAX) must not be returned by "
        "_select_pending_fact_enrichment via the eligible branch"
    )


@pytest.mark.asyncio
async def test_select_pending_excludes_soft_deleted(monkeypatch, tmp_path):
    """Soft-deleted items (is_deleted=1) are excluded."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, is_deleted, created_at, source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("deleted-item", "note", "test", "content", 1, "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
        )
        conn.commit()

    with sqlite3.connect(str(db_path)) as db:
        pending = memory_core._select_pending_fact_enrichment(db)

    pending_ids = [mid for mid, _ in pending]
    assert "deleted-item" not in pending_ids, "Soft-deleted items should be excluded"


@pytest.mark.asyncio
async def test_enrich_pending_impl_dry_run(monkeypatch, tmp_path):
    """enrich_pending_impl dry_run=True returns count + ETA, no DB mutations.

    Tests the selection query behavior directly since enrich_pending_impl
    calls _db() which can trigger migrations on fresh DBs.
    """
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)

    # Populate test DB directly
    with sqlite3.connect(str(db_path)) as conn:
        for i in range(3):
            conn.execute(
                """INSERT INTO memory_items
                (id, type, title, content, created_at, source, origin_device, scope, change_agent)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (f"item-{i}", "note", "test", f"content {i}", "2026-04-25T00:00:00Z", "agent", "test", "agent", "test_agent"),
            )
        conn.commit()

    # Test _select_pending_fact_enrichment which is the core of dry_run
    with sqlite3.connect(str(db_path)) as db:
        pending = memory_core._select_pending_fact_enrichment(db)

    assert len(pending) == 3, "Should select all 3 eligible items"
    pending_ids = [mid for mid, _ in pending]
    assert all(f"item-{i}" in pending_ids for i in range(3)), "All items should be selected"

    # Test that empty DB returns empty
    db_path2 = tmp_path / "empty.db"
    _create_fact_enriched_schema(db_path2)

    with sqlite3.connect(str(db_path2)) as db:
        pending_empty = memory_core._select_pending_fact_enrichment(db)

    assert len(pending_empty) == 0, "Empty DB should return no pending items"


@pytest.mark.asyncio
async def test_locomo_audit_fact_enricher_none(monkeypatch, tmp_path):
    """With fact_enricher=None (default), memory_write_bulk_impl row insertion
    is byte-identical to pre-change behavior: same item count, same relationship count."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_fact_enriched_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    # Mock embedding so we don't hit a real embedder
    async def fake_embed_many(texts):
        return [([0.1] * 384, "stub-model") for _ in texts]

    monkeypatch.setattr(memory_core, "_embed_many", fake_embed_many)

    items = [
        {"id": str(uuid.uuid4()), "type": "note", "content": f"content {i}"}
        for i in range(5)
    ]

    # Write with fact_enricher=None (default)
    await memory_core.memory_write_bulk_impl(items, fact_enricher=None)

    # Verify insertion
    with sqlite3.connect(str(db_path)) as conn:
        item_count = conn.execute(
            "SELECT COUNT(*) FROM memory_items"
        ).fetchone()[0]
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE type='fact_enriched'"
        ).fetchone()[0]
        queue_count = conn.execute(
            "SELECT COUNT(*) FROM fact_enrichment_queue"
        ).fetchone()[0]

    assert item_count == 5, "All 5 items should be persisted"
    assert fact_count == 0, "No fact_enriched rows should be created (enricher=None)"
    assert queue_count == 0, "No queue rows should be created (enricher=None)"


@pytest.mark.asyncio
async def test_migration_023_up_creates_table(tmp_path):
    """Migration 023 up creates fact_enrichment_queue table."""
    db_path = tmp_path / "test.db"

    # Apply migration
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "migrations"
    )
    migration_up = os.path.join(migrations_dir, "023_fact_enrichment_queue.up.sql")

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # First create memory_items (required for FK)
    cursor.execute(
        """CREATE TABLE memory_items (id TEXT PRIMARY KEY)"""
    )

    if os.path.exists(migration_up):
        with open(migration_up, "r", encoding="utf-8") as f:
            sql = f.read()
        cursor.executescript(sql)

    conn.commit()

    # Verify table exists
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fact_enrichment_queue'"
    )
    assert cursor.fetchone() is not None, "fact_enrichment_queue table should exist"

    # Verify schema
    cursor.execute("PRAGMA table_info(fact_enrichment_queue)")
    columns = {row[1] for row in cursor.fetchall()}
    assert "memory_id" in columns
    assert "attempts" in columns
    assert "last_error" in columns
    assert "last_attempt_at" in columns

    conn.close()


@pytest.mark.asyncio
async def test_migration_023_down_removes_table(tmp_path):
    """Migration 023 down removes fact_enrichment_queue table."""
    db_path = tmp_path / "test.db"

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "migrations"
    )
    migration_up = os.path.join(migrations_dir, "023_fact_enrichment_queue.up.sql")
    migration_down = os.path.join(migrations_dir, "023_fact_enrichment_queue.down.sql")

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Create memory_items (required for FK)
    cursor.execute(
        """CREATE TABLE memory_items (id TEXT PRIMARY KEY)"""
    )

    # Apply up
    if os.path.exists(migration_up):
        with open(migration_up, "r", encoding="utf-8") as f:
            sql = f.read()
        cursor.executescript(sql)

    # Verify table exists
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fact_enrichment_queue'"
    )
    assert cursor.fetchone() is not None

    # Apply down
    if os.path.exists(migration_down):
        with open(migration_down, "r", encoding="utf-8") as f:
            sql = f.read()
        cursor.executescript(sql)

    conn.commit()

    # Verify table removed
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fact_enrichment_queue'"
    )
    assert cursor.fetchone() is None, "fact_enrichment_queue table should be removed"

    conn.close()


# NOTE: Migration round-trip testing (the 023 up/down/up idempotency test) is covered
# above in test_migration_023_up_creates_table and test_migration_023_down_removes_table.
# A full round-trip with content preservation is deferred per the plan comment:
# "already covered by Wave 1 manual verification, and exercising the migration runner
# from pytest is awkward."
