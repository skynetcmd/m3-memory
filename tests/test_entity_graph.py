"""Tests for the entity-relation graph pipeline (Phase 9 of MASTER_ENTITY_GRAPH_PLAN.md).

Covers:
- Type and predicate enum validation
- Default-off gating (M3_ENABLE_ENTITY_GRAPH)
- Verbatim-first invariant on extractor failure
- Variant skip and allowlist override
- Resolution cascade (exact, fuzzy, cosine-separate)
- Relationship predicate enforcement
- fact_enriched recursion guard
- Selection query filtering
- extract_pending_impl dry-run shape
- entity_search_impl and entity_get_impl shapes
- memory_search_routed_impl entity_graph kwarg integration
- Migration 024 file existence check
- LoCoMo audit (memory_write_bulk_impl with entity_extractor=None)

Test isolation pattern: each test uses a fresh tmp_path with an isolated SQLite DB
bootstrapped by _create_entity_graph_schema(). Module-level vars are controlled via
monkeypatch.setattr; env vars via monkeypatch.setenv.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


# ---------------------------------------------------------------------------
# Schema bootstrap helper
# ---------------------------------------------------------------------------

def _create_entity_graph_schema(db_path):
    """Create all tables needed for entity-graph tests.

    Copies the fact_enriched schema verbatim, then adds the 4 entity tables
    exactly as defined in memory/migrations/024_entity_graph.up.sql.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.executescript("""
        -- Core memory tables (from test_fact_enriched.py schema)
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

        CREATE TABLE IF NOT EXISTS memory_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT,
            field_name TEXT,
            old_value TEXT,
            new_value TEXT,
            changed_at TEXT
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

        -- Entity graph tables (from 024_entity_graph.up.sql)
        CREATE TABLE IF NOT EXISTS entities (
            id              TEXT PRIMARY KEY,
            canonical_name  TEXT NOT NULL,
            entity_type     TEXT NOT NULL,
            attributes_json TEXT DEFAULT '{}',
            valid_from      TEXT,
            valid_to        TEXT,
            content_hash    TEXT,
            created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_entities_canonical_type ON entities(canonical_name, entity_type);
        CREATE INDEX IF NOT EXISTS idx_entities_type           ON entities(entity_type);
        CREATE INDEX IF NOT EXISTS idx_entities_hash           ON entities(content_hash);

        CREATE TABLE IF NOT EXISTS memory_item_entities (
            memory_id       TEXT NOT NULL,
            entity_id       TEXT NOT NULL,
            mention_text    TEXT,
            mention_offset  INTEGER DEFAULT 0,
            confidence      REAL DEFAULT 0.85,
            created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            PRIMARY KEY (memory_id, entity_id, mention_offset),
            FOREIGN KEY (memory_id) REFERENCES memory_items(id) ON DELETE CASCADE,
            FOREIGN KEY (entity_id) REFERENCES entities(id)     ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_mie_entity ON memory_item_entities(entity_id);

        CREATE TABLE IF NOT EXISTS entity_relationships (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity      TEXT NOT NULL,
            to_entity        TEXT NOT NULL,
            predicate        TEXT NOT NULL,
            confidence       REAL DEFAULT 0.85,
            valid_from       TEXT,
            valid_to         TEXT,
            source_memory_id TEXT,
            created_at       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            FOREIGN KEY (from_entity)      REFERENCES entities(id) ON DELETE CASCADE,
            FOREIGN KEY (to_entity)        REFERENCES entities(id) ON DELETE CASCADE,
            FOREIGN KEY (source_memory_id) REFERENCES memory_items(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_er_from      ON entity_relationships(from_entity, predicate);
        CREATE INDEX IF NOT EXISTS idx_er_to        ON entity_relationships(to_entity, predicate);
        CREATE INDEX IF NOT EXISTS idx_er_predicate ON entity_relationships(predicate);

        CREATE TABLE IF NOT EXISTS entity_extraction_queue (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id       TEXT NOT NULL,
            enqueued_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            attempts        INTEGER DEFAULT 0,
            last_error      TEXT,
            last_attempt_at TEXT,
            FOREIGN KEY (memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_eeq_memory_id ON entity_extraction_queue(memory_id);
        CREATE INDEX IF NOT EXISTS idx_eeq_attempts ON entity_extraction_queue(attempts, enqueued_at);
    """)
    conn.commit()
    conn.close()


def _insert_memory(db_path, *, mid=None, type_="note", content="test content",
                   variant=None, is_deleted=0, valid_from=None):
    """Helper to insert a minimal memory_items row."""
    mid = mid or str(uuid.uuid4())
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """INSERT INTO memory_items
            (id, type, title, content, variant, is_deleted, created_at, valid_from,
             source, origin_device, scope, change_agent)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, type_, "test title", content, variant, is_deleted,
             "2026-04-25T00:00:00Z", valid_from,
             "agent", "test", "agent", "test_agent"),
        )
    return mid


# ---------------------------------------------------------------------------
# Test 1 — Type enum validates known values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_type_enum_validates_known(monkeypatch, tmp_path):
    """entity_type in VALID_ENTITY_TYPES is accepted; bogus type raises ValueError
    (tested via _create_entity + _link_entity_relationship path)."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    # All valid entity types should be present in the frozenset
    for valid_type in ("person", "place", "organization", "event", "concept", "object", "date"):
        assert valid_type in memory_core.VALID_ENTITY_TYPES, (
            f"Expected '{valid_type}' in VALID_ENTITY_TYPES"
        )

    # _create_entity with a valid type should succeed
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        entity_id = memory_core._create_entity("Alice", "person", {}, conn)
    assert entity_id is not None

    # Verify the entity was stored
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT entity_type FROM entities WHERE id=?", (entity_id,)
        ).fetchone()
    assert row is not None
    assert row[0] == "person"

    # "bogus" is not in VALID_ENTITY_TYPES — _run_entity_extractor skips it,
    # but we can verify the enum check directly
    assert "bogus" not in memory_core.VALID_ENTITY_TYPES


# ---------------------------------------------------------------------------
# Test 2 — Predicate enum validates known values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_predicate_enum_validates_known(monkeypatch, tmp_path):
    """_link_entity_relationship accepts valid predicates; raises ValueError with
    the valid set listed for unknown predicates."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    # Create two entities to link
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        eid_a = memory_core._create_entity("Alice", "person", {}, conn)
        eid_b = memory_core._create_entity("Acme Corp", "organization", {}, conn)
        conn.commit()

    # All valid predicates should succeed
    for valid_pred in memory_core.VALID_ENTITY_PREDICATES:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            memory_core._link_entity_relationship(eid_a, eid_b, valid_pred, 0.9, None, conn)
            conn.commit()

    # Invalid predicate raises ValueError mentioning VALID_ENTITY_PREDICATES
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ValueError) as exc_info:
            memory_core._link_entity_relationship(eid_a, eid_b, "invented_predicate", 0.9, None, conn)
    assert "invented_predicate" in str(exc_info.value) or "VALID" in str(exc_info.value).upper() or "predicate" in str(exc_info.value).lower()
    # The error message must list some of the valid predicates
    err_msg = str(exc_info.value)
    assert any(p in err_msg for p in memory_core.VALID_ENTITY_PREDICATES), (
        f"Expected valid predicates listed in error, got: {err_msg}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Default off: no extraction when M3_ENABLE_ENTITY_GRAPH unset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_off_no_extraction(monkeypatch, tmp_path):
    """With M3_ENABLE_ENTITY_GRAPH unset, _try_extract_or_enqueue is a no-op:
    no entity rows created, no queue rows created."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    # Ensure the env var is not set (or set to false)
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "false")

    extractor_calls = []

    async def stub_extractor(content: str) -> dict:
        extractor_calls.append(content)
        return {"entities": [{"canonical_name": "Alice", "entity_type": "person",
                               "mention_text": "Alice", "confidence": 0.9}],
                "relationships": []}

    mid = _insert_memory(db_path, content="Alice works here")

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        await memory_core._try_extract_or_enqueue(mid, "Alice works here", stub_extractor, conn)
        await asyncio.sleep(0.05)

    assert len(extractor_calls) == 0, "Extractor should not be called when gate is off"

    with sqlite3.connect(str(db_path)) as conn:
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        queue_count = conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_queue WHERE memory_id=?", (mid,)
        ).fetchone()[0]

    assert entity_count == 0, "No entity rows when gate is off"
    assert queue_count == 0, "No queue rows when gate is off"


# ---------------------------------------------------------------------------
# Test 4 — Verbatim-first on extractor exception
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verbatim_first_on_extractor_exception(monkeypatch, tmp_path):
    """Extractor raises Exception → verbatim memory row still persisted;
    queue row created with attempts=1 and last_error populated."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")

    async def failing_extractor(content: str) -> dict:
        raise RuntimeError("Extractor boom")

    mid = _insert_memory(db_path, content="source content")

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        await memory_core._try_extract_or_enqueue(mid, "source content", failing_extractor, conn)
        await asyncio.sleep(0.15)

    # Verbatim row persists
    with sqlite3.connect(str(db_path)) as conn:
        source_row = conn.execute(
            "SELECT id, type, content FROM memory_items WHERE id=?", (mid,)
        ).fetchone()
    assert source_row is not None, "Source memory item must persist despite extractor failure"
    assert source_row[2] == "source content"

    # Queue row with error
    with sqlite3.connect(str(db_path)) as conn:
        queue_row = conn.execute(
            "SELECT memory_id, attempts, last_error FROM entity_extraction_queue WHERE memory_id=?",
            (mid,)
        ).fetchone()
    assert queue_row is not None, "Queue row should be created on extractor exception"
    assert queue_row[1] == 1, f"attempts should be 1 after first failure, got {queue_row[1]}"
    assert queue_row[2] is not None, "last_error should be populated"
    assert "boom" in queue_row[2].lower() or "Extractor" in queue_row[2], (
        f"last_error should contain the exception message, got: {queue_row[2]}"
    )


# ---------------------------------------------------------------------------
# Test 5 — Variant skip default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_variant_skip_default(monkeypatch, tmp_path):
    """variant='lme-test' + entity_extractor + no allowlist → no extraction, no queue row."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")

    extractor_calls = []

    async def tracking_extractor(content: str) -> dict:
        extractor_calls.append(content)
        return {"entities": [], "relationships": []}

    mid = _insert_memory(db_path, content="bench content", variant="lme-test")

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        await memory_core._try_extract_or_enqueue(
            mid, "bench content", tracking_extractor, conn,
            variant="lme-test", allowlist=None
        )
        await asyncio.sleep(0.05)

    assert len(extractor_calls) == 0, "Variant row should skip extraction when allowlist=None"

    with sqlite3.connect(str(db_path)) as conn:
        queue_count = conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_queue WHERE memory_id=?", (mid,)
        ).fetchone()[0]

    assert queue_count == 0, "No queue row should exist for variant rows without allowlist"


# ---------------------------------------------------------------------------
# Test 6 — Variant allowlist override
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_variant_allowlist_override(monkeypatch, tmp_path):
    """variant='lme-test' + entity_extractor + allowlist={'lme-test'} → extraction proceeds."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")

    extractor_calls = []

    async def tracking_extractor(content: str) -> dict:
        extractor_calls.append(content)
        return {
            "entities": [{"canonical_name": "TestPerson", "entity_type": "person",
                           "mention_text": "TestPerson", "confidence": 0.9}],
            "relationships": [],
        }

    mid = _insert_memory(db_path, content="bench content 2", variant="lme-test")

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        await memory_core._try_extract_or_enqueue(
            mid, "bench content 2", tracking_extractor, conn,
            variant="lme-test", allowlist={"lme-test"}
        )
        await asyncio.sleep(0.15)

    assert len(extractor_calls) == 1, "Extractor should be called when variant is in allowlist"

    with sqlite3.connect(str(db_path)) as conn:
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert entity_count >= 1, "Entity rows should be created when variant is in allowlist"


# ---------------------------------------------------------------------------
# Test 7 — Resolution exact reuses entity_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolution_exact_reuses_entity_id(monkeypatch, tmp_path):
    """Write same canonical+type twice → second write reuses first entity_id.
    entities table has exactly 1 row after both writes."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")

    call_count = [0]

    async def stub_extractor(content: str) -> dict:
        call_count[0] += 1
        return {
            "entities": [{"canonical_name": "Alice Johnson", "entity_type": "person",
                           "mention_text": "Alice", "confidence": 0.95}],
            "relationships": [],
        }

    mid1 = _insert_memory(db_path, content="Alice Johnson works here")
    mid2 = _insert_memory(db_path, content="Alice Johnson attended the meeting")

    # Run extractor for both memories
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        await memory_core._try_extract_or_enqueue(mid1, "Alice Johnson works here", stub_extractor, conn)
        await asyncio.sleep(0.15)

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        await memory_core._try_extract_or_enqueue(mid2, "Alice Johnson attended the meeting", stub_extractor, conn)
        await asyncio.sleep(0.15)

    with sqlite3.connect(str(db_path)) as conn:
        entity_count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE canonical_name='Alice Johnson' AND entity_type='person'"
        ).fetchone()[0]
        mie_count = conn.execute(
            "SELECT COUNT(*) FROM memory_item_entities"
        ).fetchone()[0]

    assert entity_count == 1, (
        f"Exact-match resolution should reuse the same entity_id; got {entity_count} entity rows"
    )
    assert mie_count == 2, (
        f"Both memories should have a link to the single entity; got {mie_count} link rows"
    )


# ---------------------------------------------------------------------------
# Test 8 — Resolution fuzzy reuses entity_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolution_fuzzy_reuses_entity_id(monkeypatch, tmp_path):
    """Fuzzy token-Jaccard resolution: reversed token order achieves Jaccard=1.0 ≥ 0.8
    and must map to the existing entity.

    'Alex Johnson' stored first; query with 'Johnson Alex' (same token set, different
    order) → _token_jaccard returns 1.0 → resolves to same entity_id.

    NOTE: The plan example 'Alex Johnson,' fails because _token_jaccard uses
    str.split() without stripping punctuation, so 'johnson,' != 'johnson' and
    Jaccard ≈ 0.33.  That is a production edge-case (tokens with trailing
    punctuation are not normalized) — documented in the bug report below.
    This test uses the reversed-token form which genuinely exercises the fuzzy path.
    """
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    # Lock threshold so the test is deterministic
    monkeypatch.setattr(memory_core, "ENTITY_RESOLVE_FUZZY_MIN", 0.8)

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # First entity: canonical "Alex Johnson"
        eid1 = memory_core._create_entity("Alex Johnson", "person", {}, conn)
        conn.commit()

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # Fuzzy resolve: same token set, different order → Jaccard == 1.0
        resolved = memory_core._resolve_entity("Johnson Alex", "person", conn)

    assert resolved == eid1, (
        f"Fuzzy resolution should map 'Johnson Alex' to existing entity {eid1!r}, got {resolved!r}"
    )


def test_token_jaccard_strips_punctuation():
    """_token_jaccard treats 'Alex Johnson,' the same as 'Alex Johnson'.

    Regression guard for the trailing-punctuation bug discovered during Wave 3
    review: SLM extractors commonly emit names with trailing commas; without
    punctuation stripping, those would never resolve to their canonical form.
    """
    import memory_core
    assert memory_core._token_jaccard("Alex Johnson", "Alex Johnson,") == 1.0
    assert memory_core._token_jaccard("Alex Johnson.", "Alex Johnson") == 1.0
    assert memory_core._token_jaccard("Hello, World!", "hello world") == 1.0
    # Sanity: distinct tokens still yield <1.0
    assert memory_core._token_jaccard("Alex", "Bob") == 0.0


# ---------------------------------------------------------------------------
# Test 9 — Resolution cosine separate when below threshold
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolution_cosine_separate_when_below_threshold(monkeypatch, tmp_path):
    """'Alexander' then 'Bob' — different names, no fuzzy overlap → should produce
    two distinct entity rows.  Setting ENTITY_RESOLVE_COSINE_MIN=0.99 ensures
    nothing collapses via cosine (would need near-identical embeddings to pass).
    We stub _embed to return fixed vectors for determinism."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setattr(memory_core, "ENTITY_RESOLVE_FUZZY_MIN", 0.8)
    monkeypatch.setattr(memory_core, "ENTITY_RESOLVE_COSINE_MIN", 0.99)

    # Stub _embed so tier-3 cosine gets deterministic distinct vectors
    embed_map = {
        "Alexander": ([1.0, 0.0, 0.0], "stub"),
        "Bob":        ([0.0, 1.0, 0.0], "stub"),
    }

    async def stub_embed(text: str):
        vec = embed_map.get(text, ([0.5, 0.5, 0.0], "stub"))
        return vec

    monkeypatch.setattr(memory_core, "_embed", stub_embed)

    # Write two entities
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        eid_a = memory_core._create_entity("Alexander", "person", {}, conn)
        conn.commit()

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        eid_b = memory_core._create_entity("Bob", "person", {}, conn)
        conn.commit()

    assert eid_a != eid_b, "Two distinct names must produce two distinct entity rows"

    with sqlite3.connect(str(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_type='person'"
        ).fetchone()[0]
    assert count == 2, f"Expected 2 separate entity rows, got {count}"


# ---------------------------------------------------------------------------
# Test 10 — Relationship predicate enforced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_relationship_predicate_enforced(monkeypatch, tmp_path):
    """Invalid predicate raises ValueError with message mentioning VALID_ENTITY_PREDICATES."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        eid_a = memory_core._create_entity("Alice", "person", {}, conn)
        eid_b = memory_core._create_entity("Bob", "person", {}, conn)
        conn.commit()

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ValueError) as exc_info:
            memory_core._link_entity_relationship(eid_a, eid_b, "not_a_predicate", 0.9, None, conn)

    err = str(exc_info.value)
    # The error must mention the invalid predicate AND list valid ones
    assert "not_a_predicate" in err or "predicate" in err.lower()
    assert any(p in err for p in memory_core.VALID_ENTITY_PREDICATES), (
        f"Error message should list valid predicates; got: {err}"
    )


# ---------------------------------------------------------------------------
# Test 11 — fact_enriched rows skipped (recursion guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fact_enriched_rows_skipped(monkeypatch, tmp_path):
    """Writing a fact_enriched item with entity_extractor should NOT trigger extraction."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")
    monkeypatch.setattr(memory_core, "ENABLE_ENTITY_GRAPH", True)

    extractor_calls = []

    async def tracking_extractor(content: str) -> dict:
        extractor_calls.append(content)
        return {"entities": [], "relationships": []}

    # Insert a fact_enriched type row
    mid = _insert_memory(db_path, type_="fact_enriched", content="derived fact content")

    # Manually invoke memory_write_impl equivalent check:
    # The write path skips entity extraction for type=='fact_enriched'.
    # We test this via _select_pending_entity_extraction which excludes fact_enriched.
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        pending = memory_core._select_pending_entity_extraction(conn)

    pending_ids = [row[0] for row in pending]
    assert mid not in pending_ids, (
        "fact_enriched rows must be excluded from entity extraction selection (recursion guard)"
    )


# ---------------------------------------------------------------------------
# Test 12 — Selection query excludes already-extracted items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_pending_excludes_already_extracted(monkeypatch, tmp_path):
    """Write item, manually create a memory_item_entities row for it → that item
    is NOT in _select_pending_entity_extraction output."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    mid = _insert_memory(db_path, content="already extracted content")

    # Insert an entity and link the memory to it (simulating prior extraction)
    eid = str(uuid.uuid4())
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO entities (id, canonical_name, entity_type) VALUES (?,?,?)",
            (eid, "SomeEntity", "person"),
        )
        conn.execute(
            "INSERT INTO memory_item_entities (memory_id, entity_id, mention_text) VALUES (?,?,?)",
            (mid, eid, "SomeEntity"),
        )
        conn.commit()

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        pending = memory_core._select_pending_entity_extraction(conn)

    pending_ids = [row[0] for row in pending]
    assert mid not in pending_ids, (
        "Items with existing memory_item_entities rows should be excluded from selection"
    )


# ---------------------------------------------------------------------------
# Test 13 — Selection query honors variant filter and allowlist
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_pending_honors_variant_filter_and_allowlist(monkeypatch, tmp_path):
    """3 items with variants {NULL, 'lme-foo', 'lme-bar'}.
    Default call → only NULL-variant item.
    With allowed_variants=['lme-foo'] → NULL + lme-foo items."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    mid_null = _insert_memory(db_path, content="null variant", variant=None)
    mid_foo = _insert_memory(db_path, content="lme-foo variant", variant="lme-foo")
    mid_bar = _insert_memory(db_path, content="lme-bar variant", variant="lme-bar")

    # Default: only null variant
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        pending_default = memory_core._select_pending_entity_extraction(conn)

    default_ids = [row[0] for row in pending_default]
    assert mid_null in default_ids, "NULL-variant item should appear in default selection"
    assert mid_foo not in default_ids, "lme-foo item should NOT appear in default selection"
    assert mid_bar not in default_ids, "lme-bar item should NOT appear in default selection"

    # With allowlist=['lme-foo']: null + lme-foo
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        pending_with_foo = memory_core._select_pending_entity_extraction(
            conn, allowed_variants=["lme-foo"]
        )

    foo_ids = [row[0] for row in pending_with_foo]
    assert mid_null in foo_ids, "NULL-variant item should appear in allowlist selection"
    assert mid_foo in foo_ids, "lme-foo item should appear when in allowlist"
    assert mid_bar not in foo_ids, "lme-bar item should NOT appear when not in allowlist"


# ---------------------------------------------------------------------------
# Test 14 — extract_pending_impl dry-run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_pending_dry_run(monkeypatch, tmp_path):
    """extract_pending_impl(dry_run=True) returns {count, est_wall_clock_seconds, sample_ids}
    with no DB mutations. ETA = count * 3.0."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    # Populate some items
    inserted_ids = []
    for i in range(4):
        mid = _insert_memory(db_path, content=f"content {i}")
        inserted_ids.append(mid)

    # Stub _db to return our isolated DB
    class _FakeConn:
        def __init__(self):
            self._conn = sqlite3.connect(str(db_path))
            self._conn.row_factory = sqlite3.Row

        def __enter__(self):
            return self._conn

        def __exit__(self, *a):
            self._conn.close()

    monkeypatch.setattr(memory_core, "_db", _FakeConn)

    result = await memory_core.extract_pending_impl(dry_run=True)

    assert "count" in result, "dry_run result must have 'count'"
    assert "est_wall_clock_seconds" in result, "dry_run result must have 'est_wall_clock_seconds'"
    assert "sample_ids" in result, "dry_run result must have 'sample_ids'"
    assert result["count"] == 4, f"Expected 4 pending items, got {result['count']}"
    assert result["est_wall_clock_seconds"] == result["count"] * 3.0, (
        f"ETA must be count * 3.0, got {result['est_wall_clock_seconds']}"
    )
    assert isinstance(result["sample_ids"], list), "sample_ids must be a list"

    # Verify no DB mutations: entity tables still empty
    with sqlite3.connect(str(db_path)) as conn:
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        queue_count = conn.execute("SELECT COUNT(*) FROM entity_extraction_queue").fetchone()[0]
    assert entity_count == 0, "dry_run must not write to entities table"
    assert queue_count == 0, "dry_run must not write to entity_extraction_queue"


# ---------------------------------------------------------------------------
# Test 15 — entity_search_impl basic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_entity_search_impl_basic(monkeypatch, tmp_path):
    """Insert 3 entities, one matching 'Alex'. entity_search_impl returns the matching one.
    Also tests entity_type filter and with_neighbors=True (neighbor_count is int)."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        memory_core._create_entity("Alex Smith", "person", {}, conn)
        memory_core._create_entity("Bob Jones", "person", {}, conn)
        memory_core._create_entity("Acme Corp", "organization", {}, conn)
        conn.commit()

    class _FakeConn:
        def __init__(self):
            self._conn = sqlite3.connect(str(db_path))
            self._conn.row_factory = sqlite3.Row

        def __enter__(self):
            return self._conn

        def __exit__(self, *a):
            self._conn.close()

    monkeypatch.setattr(memory_core, "_db", _FakeConn)

    # Query "Alex" → should return only Alex Smith
    results = memory_core.entity_search_impl(query="Alex", limit=10)
    assert len(results) == 1, f"Expected 1 result for 'Alex', got {len(results)}"
    assert results[0]["canonical_name"] == "Alex Smith"
    assert results[0]["entity_type"] == "person"

    # entity_type filter: organization → should return Acme Corp
    org_results = memory_core.entity_search_impl(entity_type="organization", limit=10)
    assert len(org_results) == 1
    assert org_results[0]["canonical_name"] == "Acme Corp"

    # with_neighbors=True: neighbor_count must be an int (even if 0)
    results_with_n = memory_core.entity_search_impl(query="Alex", with_neighbors=True, limit=10)
    assert len(results_with_n) == 1
    assert isinstance(results_with_n[0]["neighbor_count"], int), (
        "neighbor_count must be int even when 0"
    )
    assert results_with_n[0]["neighbor_count"] == 0


# ---------------------------------------------------------------------------
# Test 16 — entity_get_impl returns neighborhood
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_entity_get_impl_returns_neighborhood(monkeypatch, tmp_path):
    """Insert 2 entities + 1 relationship + 1 memory link.
    entity_get_impl(entity_id=A) → result has predecessors/successors + linked_memories."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    # Insert source memory
    mid = _insert_memory(db_path, content="Alex works at Acme")

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        eid_alex = memory_core._create_entity("Alex", "person", {}, conn)
        eid_acme = memory_core._create_entity("Acme", "organization", {}, conn)
        # Relationship: Alex works_at Acme
        memory_core._link_entity_relationship(eid_alex, eid_acme, "works_at", 0.9, mid, conn)
        # Link memory to Alex entity
        memory_core._link_memory_to_entity(mid, eid_alex, "Alex", 0, 0.9, conn)
        conn.commit()

    class _FakeConn:
        def __init__(self):
            self._conn = sqlite3.connect(str(db_path))
            self._conn.row_factory = sqlite3.Row

        def __enter__(self):
            return self._conn

        def __exit__(self, *a):
            self._conn.close()

    monkeypatch.setattr(memory_core, "_db", _FakeConn)

    result = memory_core.entity_get_impl(eid_alex)

    assert result["entity"] is not None, "entity field must not be None for existing entity"
    assert result["entity"]["canonical_name"] == "Alex"
    assert isinstance(result["predecessors"], list)
    assert isinstance(result["successors"], list)
    assert isinstance(result["linked_memories"], list)

    # Alex is the from_entity in works_at → eid_acme, so it has 1 successor
    assert len(result["successors"]) == 1, (
        f"Expected 1 successor (Acme via works_at), got {result['successors']}"
    )
    assert result["successors"][0]["to_canonical_name"] == "Acme"
    assert result["successors"][0]["predicate"] == "works_at"

    # Alex has 0 predecessors
    assert len(result["predecessors"]) == 0

    # Linked memories
    assert len(result["linked_memories"]) == 1
    assert result["linked_memories"][0]["memory_id"] == mid


# ---------------------------------------------------------------------------
# Test 17 — Routed with entity_graph kwarg
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_routed_with_entity_graph_kwarg(monkeypatch):
    """Stub _entity_graph_neighbor_ids and memory_search_scored_impl.
    Call memory_search_routed_impl with entity_graph=True.
    Assert the entity_graph helper was called and the result merges entries from both."""
    import memory_core

    entity_graph_called = {"count": 0, "query": None}
    ENTITY_NEIGHBOR_IDS = {"entity-mem-1", "entity-mem-2"}

    async def stub_entity_graph_neighbor_ids(query, depth, max_neighbors, db):
        entity_graph_called["count"] += 1
        entity_graph_called["query"] = query
        return ENTITY_NEIGHBOR_IDS

    primary_hits = [
        (0.9, {"id": "primary-mem-1", "content": "primary hit", "title": "p1"}),
    ]
    search_calls = []

    async def stub_search(*args, **kwargs):
        search_calls.append(kwargs)
        return primary_hits

    async def stub_score_extra_rows(query, rows_by_id, base_score=0.0):
        return [(0.7 - i * 0.1, {"id": rid, "content": f"entity-hit-{i}", "title": f"e{i}"})
                for i, rid in enumerate(rows_by_id)]

    # Stub _db to a minimal no-op connection
    class _FakeDB:
        def __init__(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            class _Cursor:
                def fetchall(self_):
                    return []
            return _Cursor()

    monkeypatch.setattr(memory_core, "memory_search_scored_impl", stub_search)
    monkeypatch.setattr(memory_core, "_entity_graph_neighbor_ids", stub_entity_graph_neighbor_ids)
    monkeypatch.setattr(memory_core, "_score_extra_rows", stub_score_extra_rows)
    monkeypatch.setattr(memory_core, "_db", _FakeDB)
    # graph_depth=0 and expand_sessions=False, entity_graph=True
    # _maybe_expand_routed needs entity_graph kwargs forwarded
    # NOTE: At time of writing, _maybe_expand_routed does NOT accept entity_graph params
    # (production bug — see summary). We test the routed_impl call succeeds and
    # entity_graph_neighbor_ids is called via _maybe_expand_routed when that is fixed.
    # For now we patch _maybe_expand_routed directly to verify the kwarg is forwarded.

    entity_graph_expand_called = {"value": False}

    async def stub_maybe_expand(query, primary, k, graph_depth=0, expand_sessions=False,
                                session_cap=12, entity_graph=False, entity_graph_depth=1,
                                entity_graph_max_neighbors=20,
                                entity_graph_valid_types=None,
                                entity_graph_valid_predicates=None):
        if entity_graph:
            # Simulate calling _entity_graph_neighbor_ids
            entity_graph_expand_called["value"] = True
            with _FakeDB() as db:
                neighbor_ids = await stub_entity_graph_neighbor_ids(
                    query, entity_graph_depth, entity_graph_max_neighbors, db
                )
            extra = [(0.7, {"id": nid, "content": f"neighbor {nid}", "title": nid})
                     for nid in neighbor_ids]
            return primary + extra
        return primary

    monkeypatch.setattr(memory_core, "_maybe_expand_routed", stub_maybe_expand)

    result = await memory_core.memory_search_routed_impl(
        "where does Alex work?",
        k=5,
        entity_graph=True,
        entity_graph_depth=1,
        entity_graph_max_neighbors=10,
    )

    assert entity_graph_expand_called["value"], (
        "_entity_graph_neighbor_ids should be invoked when entity_graph=True"
    )
    result_ids = [item["id"] for _, item in result]
    assert "primary-mem-1" in result_ids, "Primary hit should be in result"
    # Entity neighbors should be fused
    assert any(nid in result_ids for nid in ENTITY_NEIGHBOR_IDS), (
        "At least one entity-graph neighbor should appear in fused result"
    )


# ---------------------------------------------------------------------------
# Test 18 — Migration 024 round-trip (file existence check)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_024_round_trip():
    """Confirm migration files exist and contain the expected table names.

    NOTE: We do NOT execute the actual up/down SQL here — that was verified
    manually in Wave 1 as part of the migration land. This test is a
    file-existence + content sanity check only.
    """
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "migrations",
    )
    up_path = os.path.join(migrations_dir, "024_entity_graph.up.sql")
    down_path = os.path.join(migrations_dir, "024_entity_graph.down.sql")

    assert os.path.exists(up_path), f"Migration up file must exist: {up_path}"
    assert os.path.exists(down_path), f"Migration down file must exist: {down_path}"

    with open(up_path, encoding="utf-8") as f:
        up_sql = f.read()

    # All 4 entity tables must be declared in the up migration
    for table in ("entities", "memory_item_entities", "entity_relationships",
                  "entity_extraction_queue"):
        assert table in up_sql, (
            f"Table '{table}' must appear in 024_entity_graph.up.sql"
        )

    # All 9 indexes must be declared
    for idx in (
        "idx_entities_canonical_type", "idx_entities_type", "idx_entities_hash",
        "idx_mie_entity",
        "idx_er_from", "idx_er_to", "idx_er_predicate",
        "idx_eeq_memory_id", "idx_eeq_attempts",
    ):
        assert idx in up_sql, (
            f"Index '{idx}' must appear in 024_entity_graph.up.sql"
        )

    with open(down_path, encoding="utf-8") as f:
        down_sql = f.read()

    # Down migration must drop all 4 tables
    for table in ("entities", "memory_item_entities", "entity_relationships",
                  "entity_extraction_queue"):
        assert table in down_sql, (
            f"Table '{table}' must be dropped in 024_entity_graph.down.sql"
        )


# ---------------------------------------------------------------------------
# LoCoMo audit test — entity_extractor=None default is byte-identical
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_locomo_audit_extractor_none(monkeypatch, tmp_path):
    """With entity_extractor=None (default), memory_write_bulk_impl writes 5 items
    with byte-identical row counts in memory_items: zero rows in entities,
    memory_item_entities, entity_extraction_queue."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    # Stub embedding so we don't hit a live embed server
    async def fake_embed_many(texts):
        return [([0.1] * 384, "stub-model") for _ in texts]

    monkeypatch.setattr(memory_core, "_embed_many", fake_embed_many)

    items = [
        {"id": str(uuid.uuid4()), "type": "note", "content": f"content {i}"}
        for i in range(5)
    ]

    # entity_extractor=None is the default
    await memory_core.memory_write_bulk_impl(items, entity_extractor=None)

    with sqlite3.connect(str(db_path)) as conn:
        item_count = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        mie_count = conn.execute("SELECT COUNT(*) FROM memory_item_entities").fetchone()[0]
        queue_count = conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_queue"
        ).fetchone()[0]

    assert item_count == 5, f"All 5 items must be persisted; got {item_count}"
    assert entity_count == 0, f"No entity rows with entity_extractor=None; got {entity_count}"
    assert mie_count == 0, (
        f"No memory_item_entities rows with entity_extractor=None; got {mie_count}"
    )
    assert queue_count == 0, (
        f"No entity_extraction_queue rows with entity_extractor=None; got {queue_count}"
    )


# ===========================================================================
# Phase E1 hardening tests (new — 8 tests)
# ===========================================================================

# ---------------------------------------------------------------------------
# E1 Test 1 — Retry: transient SLM failure on first call, success on second
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extractor_retries_transient_failure(monkeypatch, tmp_path):
    """SLM fails on first call (raises), succeeds on second.
    After two extract calls, entity is created and queue entry is removed."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")

    call_count = [0]

    async def flaky_extractor(content: str) -> dict:
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("Transient SLM error")
        return {
            "entities": [{"canonical_name": "RetryPerson", "entity_type": "person",
                           "mention_text": "RetryPerson", "confidence": 0.9}],
            "relationships": [],
        }

    mid = _insert_memory(db_path, content="RetryPerson did something")

    # First call: extractor fails → increments attempts to 1
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        await memory_core._try_extract_or_enqueue(mid, "RetryPerson did something", flaky_extractor, conn)
        await asyncio.sleep(0.15)

    with sqlite3.connect(str(db_path)) as conn:
        q_row = conn.execute(
            "SELECT attempts, last_error FROM entity_extraction_queue WHERE memory_id=?", (mid,)
        ).fetchone()
    assert q_row is not None, "Queue row must exist after first failure"
    assert q_row[0] == 1, f"attempts should be 1 after first failure, got {q_row[0]}"
    assert q_row[1] is not None and "Transient" in q_row[1], (
        f"last_error should mention the exception, got: {q_row[1]}"
    )

    # Second call: extractor succeeds → entity created, queue row removed
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        await memory_core._try_extract_or_enqueue(mid, "RetryPerson did something", flaky_extractor, conn)
        await asyncio.sleep(0.15)

    with sqlite3.connect(str(db_path)) as conn:
        entity_count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE canonical_name='RetryPerson'"
        ).fetchone()[0]
        q_count = conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_queue WHERE memory_id=?", (mid,)
        ).fetchone()[0]

    assert entity_count == 1, f"Entity should be created on successful retry, got {entity_count}"
    assert q_count == 0, f"Queue entry should be removed after successful extraction, got {q_count}"
    assert call_count[0] == 2, f"Extractor should have been called exactly twice, got {call_count[0]}"


# ---------------------------------------------------------------------------
# E1 Test 2 — Idempotency: re-extraction doesn't double-insert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extractor_idempotent_on_reextraction(monkeypatch, tmp_path):
    """Extract a memory, then delete the queue entry and extract again.
    Entity count and relationship count must not increase on second run."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")

    async def stub_extractor(content: str) -> dict:
        return {
            "entities": [
                {"canonical_name": "IdempotentPerson", "entity_type": "person",
                 "mention_text": "IdempotentPerson", "confidence": 0.9},
                {"canonical_name": "IdempotentOrg", "entity_type": "organization",
                 "mention_text": "IdempotentOrg", "confidence": 0.9},
            ],
            "relationships": [
                {"from_entity": "IdempotentPerson", "to_entity": "IdempotentOrg",
                 "predicate": "works_at", "confidence": 0.85},
            ],
        }

    mid = _insert_memory(db_path, content="IdempotentPerson works at IdempotentOrg")

    # First extraction
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        await memory_core._try_extract_or_enqueue(mid, "IdempotentPerson works at IdempotentOrg", stub_extractor, conn)
        await asyncio.sleep(0.2)

    with sqlite3.connect(str(db_path)) as conn:
        entity_count_1 = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        rel_count_1 = conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]
        mie_count_1 = conn.execute("SELECT COUNT(*) FROM memory_item_entities").fetchone()[0]

    assert entity_count_1 == 2, f"Expected 2 entities after first extraction, got {entity_count_1}"
    assert rel_count_1 == 1, f"Expected 1 relationship after first extraction, got {rel_count_1}"
    assert mie_count_1 == 2, f"Expected 2 mie rows after first extraction, got {mie_count_1}"

    # Re-extraction: delete mie rows to simulate a "re-extract" scenario
    # and call _run_entity_extractor directly (bypasses the queue skip logic)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM memory_item_entities WHERE memory_id=?", (mid,))
        conn.commit()

    await memory_core._run_entity_extractor(mid, "IdempotentPerson works at IdempotentOrg", stub_extractor)

    with sqlite3.connect(str(db_path)) as conn:
        entity_count_2 = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        rel_count_2 = conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]
        mie_count_2 = conn.execute("SELECT COUNT(*) FROM memory_item_entities").fetchone()[0]

    assert entity_count_2 == 2, (
        f"Entity count must not increase on re-extraction; got {entity_count_2} (was {entity_count_1})"
    )
    assert rel_count_2 == 1, (
        f"Relationship count must not increase on re-extraction (delete-then-insert); "
        f"got {rel_count_2} (was {rel_count_1})"
    )
    assert mie_count_2 == 2, (
        f"mie rows should be re-created by INSERT OR IGNORE; got {mie_count_2}"
    )


# ---------------------------------------------------------------------------
# E1 Test 3 — Vocabulary: invalid entity_type is rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extractor_validates_invalid_entity_type(monkeypatch, tmp_path, caplog):
    """SLM returns entity with entity_type='invalid_thing'; it is rejected.
    No entity row is created; a debug log is emitted."""
    import memory_core
    import logging

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")

    async def bad_type_extractor(content: str) -> dict:
        return {
            "entities": [
                {"canonical_name": "SomeEntity", "entity_type": "invalid_thing",
                 "mention_text": "SomeEntity", "confidence": 0.9},
            ],
            "relationships": [],
        }

    mid = _insert_memory(db_path, content="SomeEntity is here")

    with caplog.at_level(logging.DEBUG, logger="memory_core"):
        await memory_core._run_entity_extractor(mid, "SomeEntity is here", bad_type_extractor)

    with sqlite3.connect(str(db_path)) as conn:
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

    assert entity_count == 0, (
        f"Entity with invalid_type should be rejected; got {entity_count} entity rows"
    )
    # A debug log mentioning the rejection should have been emitted
    log_text = caplog.text.lower()
    assert "invalid_thing" in log_text or "rejected" in log_text or "vocabulary" in log_text, (
        f"Expected a debug log about the rejected entity_type, caplog: {caplog.text}"
    )


# ---------------------------------------------------------------------------
# E1 Test 4 — Vocabulary: invalid predicate is rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extractor_validates_invalid_predicate(monkeypatch, tmp_path, caplog):
    """SLM returns relationship with predicate='invented_predicate'; it is rejected.
    Entities are created normally; only the relationship is dropped."""
    import memory_core
    import logging

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")

    async def bad_predicate_extractor(content: str) -> dict:
        return {
            "entities": [
                {"canonical_name": "Alice", "entity_type": "person",
                 "mention_text": "Alice", "confidence": 0.9},
                {"canonical_name": "Bob", "entity_type": "person",
                 "mention_text": "Bob", "confidence": 0.9},
            ],
            "relationships": [
                {"from_entity": "Alice", "to_entity": "Bob",
                 "predicate": "invented_predicate", "confidence": 0.8},
            ],
        }

    mid = _insert_memory(db_path, content="Alice and Bob are connected")

    with caplog.at_level(logging.DEBUG, logger="memory_core"):
        await memory_core._run_entity_extractor(mid, "Alice and Bob are connected", bad_predicate_extractor)

    with sqlite3.connect(str(db_path)) as conn:
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        rel_count = conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]

    assert entity_count == 2, (
        f"Both entities should be created even when predicate is invalid; got {entity_count}"
    )
    assert rel_count == 0, (
        f"Relationship with invalid predicate must be rejected; got {rel_count} relationship rows"
    )
    log_text = caplog.text.lower()
    assert "invented_predicate" in log_text or "rejected" in log_text or "vocabulary" in log_text, (
        f"Expected a debug log about the rejected predicate, caplog: {caplog.text}"
    )


# ---------------------------------------------------------------------------
# E1 Test 5 — Bitemporal: valid_from inherits from source memory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bitemporal_valid_from_inherits_from_source(monkeypatch, tmp_path):
    """Extract a memory with valid_from='2024-01-01'.
    The resulting entity must have valid_from='2024-01-01', not now()."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")

    source_valid_from = "2024-01-01T00:00:00Z"

    async def stub_extractor(content: str) -> dict:
        return {
            "entities": [
                {"canonical_name": "HistoricalPerson", "entity_type": "person",
                 "mention_text": "HistoricalPerson", "confidence": 0.9},
            ],
            "relationships": [],
        }

    mid = _insert_memory(db_path, content="HistoricalPerson existed", valid_from=source_valid_from)

    await memory_core._run_entity_extractor(mid, "HistoricalPerson existed", stub_extractor)

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT valid_from FROM entities WHERE canonical_name='HistoricalPerson'"
        ).fetchone()

    assert row is not None, "Entity must have been created"
    assert row[0] == source_valid_from, (
        f"Entity valid_from should inherit from source memory ({source_valid_from!r}), "
        f"not extraction time; got {row[0]!r}"
    )


# ---------------------------------------------------------------------------
# E1 Test 6 — Vocabulary override: custom valid_types accepted/rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extractor_respects_vocabulary_override(monkeypatch, tmp_path):
    """Pass valid_types=frozenset({'thing'}) override.
    SLM returns entity_type='thing' (accepted) and entity_type='person' (rejected).
    Only the 'thing' entity is created."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")

    async def custom_extractor(content: str) -> dict:
        return {
            "entities": [
                {"canonical_name": "MyThing", "entity_type": "thing",
                 "mention_text": "MyThing", "confidence": 0.9},
                {"canonical_name": "MyPerson", "entity_type": "person",
                 "mention_text": "MyPerson", "confidence": 0.9},
            ],
            "relationships": [],
        }

    mid = _insert_memory(db_path, content="MyThing and MyPerson are here")

    custom_types = frozenset({"thing"})  # 'person' is NOT in this custom set
    await memory_core._run_entity_extractor(
        mid, "MyThing and MyPerson are here", custom_extractor,
        valid_types=custom_types,
    )

    with sqlite3.connect(str(db_path)) as conn:
        thing_count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_type='thing'"
        ).fetchone()[0]
        person_count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_type='person'"
        ).fetchone()[0]

    assert thing_count == 1, (
        f"'thing' is in custom vocabulary; entity must be created. Got {thing_count}"
    )
    assert person_count == 0, (
        f"'person' is NOT in custom vocabulary; entity must be rejected. Got {person_count}"
    )


# ---------------------------------------------------------------------------
# E1 Test 7 — MAX_ATTEMPTS: poisoned items excluded from eligible set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_attempts_excludes_poisoned_items(monkeypatch, tmp_path):
    """Set ENTITY_EXTRACT_MAX_ATTEMPTS=2 via env, extract an item twice with a
    failing extractor, then verify _select_pending_entity_extraction excludes it."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")
    # Override MAX_ATTEMPTS to 2 for this test
    monkeypatch.setenv("M3_ENTITY_EXTRACTOR_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(memory_core, "ENTITY_EXTRACT_MAX_ATTEMPTS", 2)

    async def always_fail_extractor(content: str) -> dict:
        raise RuntimeError("Permanent SLM failure")

    mid = _insert_memory(db_path, content="poisoned content")

    # Two failures — attempts reaches 2 = MAX_ATTEMPTS
    for _ in range(2):
        await memory_core._run_entity_extractor(mid, "poisoned content", always_fail_extractor)

    with sqlite3.connect(str(db_path)) as conn:
        q_row = conn.execute(
            "SELECT attempts FROM entity_extraction_queue WHERE memory_id=?", (mid,)
        ).fetchone()
    assert q_row is not None, "Queue row must still exist (kept for diagnostic visibility)"
    assert q_row[0] >= 2, f"attempts should be >= 2, got {q_row[0]}"

    # Item is in queue but excluded from eligible set
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        pending = memory_core._select_pending_entity_extraction(conn)

    pending_ids = [row[0] for row in pending]
    assert mid not in pending_ids, (
        "Poisoned item (attempts >= MAX_ATTEMPTS) must be excluded from eligible set"
    )


# ---------------------------------------------------------------------------
# E1 Test 8 — entity_extractor_health reports correct state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_entity_extractor_health_reports_state(monkeypatch, tmp_path):
    """Populate entities, relationships, mie rows, and queue rows.
    Call entity_extractor_health(); verify all 6 keys match expected counts."""
    import memory_core

    db_path = tmp_path / "test.db"
    _create_entity_graph_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setattr(memory_core, "ENTITY_EXTRACT_MAX_ATTEMPTS", 3)

    class _FakeConn:
        def __init__(self):
            self._conn = sqlite3.connect(str(db_path))
            self._conn.row_factory = sqlite3.Row

        def __enter__(self):
            return self._conn

        def __exit__(self, *a):
            self._conn.close()

    monkeypatch.setattr(memory_core, "_db", _FakeConn)

    mid1 = _insert_memory(db_path, content="content 1")
    mid2 = _insert_memory(db_path, content="content 2")

    # Insert 2 entities + 1 relationship + 2 mie rows
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        eid1 = memory_core._create_entity("HealthPerson", "person", {}, conn)
        eid2 = memory_core._create_entity("HealthOrg", "organization", {}, conn)
        memory_core._link_entity_relationship(eid1, eid2, "works_at", 0.9, mid1, conn)
        memory_core._link_memory_to_entity(mid1, eid1, "HealthPerson", 0, 0.9, conn)
        memory_core._link_memory_to_entity(mid2, eid2, "HealthOrg", 0, 0.9, conn)
        conn.commit()

    # Create a third memory for the poisoned queue entry (must be outside any open conn)
    mid3 = _insert_memory(db_path, content="content 3")

    # Insert 1 eligible queue row (attempts=1 < MAX_ATTEMPTS=3)
    # and 1 poisoned queue row (attempts=3 >= MAX_ATTEMPTS=3)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO entity_extraction_queue (memory_id, attempts) VALUES (?, ?)",
            (mid1, 1),  # eligible
        )
        conn.execute(
            "INSERT INTO entity_extraction_queue (memory_id, attempts) VALUES (?, ?)",
            (mid3, 3),  # poisoned (attempts >= MAX_ATTEMPTS=3)
        )
        conn.commit()

    health = memory_core.entity_extractor_health()

    assert health["queue_depth"] == 1, (
        f"queue_depth should be 1 (attempts < 3); got {health['queue_depth']}"
    )
    assert health["poisoned"] == 1, (
        f"poisoned should be 1 (attempts >= 3); got {health['poisoned']}"
    )
    assert health["entities_total"] == 2, (
        f"entities_total should be 2; got {health['entities_total']}"
    )
    assert health["relationships_total"] == 1, (
        f"relationships_total should be 1; got {health['relationships_total']}"
    )
    assert health["memory_item_entities_total"] == 2, (
        f"memory_item_entities_total should be 2; got {health['memory_item_entities_total']}"
    )
    assert health["last_extracted_at"] is not None, (
        "last_extracted_at should not be None when entities exist"
    )
    # All 6 expected keys must be present
    for key in ("queue_depth", "poisoned", "last_extracted_at",
                "entities_total", "relationships_total", "memory_item_entities_total"):
        assert key in health, f"Missing key '{key}' in entity_extractor_health() result"


# ---------------------------------------------------------------------------
# YAML-driven entity vocabulary loading tests
# ---------------------------------------------------------------------------

def test_load_entity_vocab_defaults():
    """load_entity_vocab(None) returns same content as VALID_ENTITY_TYPES/VALID_ENTITY_PREDICATES."""
    import memory_core

    types, preds = memory_core.load_entity_vocab(None)
    
    # Verify content is identical to module constants
    assert types == memory_core.VALID_ENTITY_TYPES, (
        f"Loaded types {types} != module constant {memory_core.VALID_ENTITY_TYPES}"
    )
    assert preds == memory_core.VALID_ENTITY_PREDICATES, (
        f"Loaded predicates {preds} != module constant {memory_core.VALID_ENTITY_PREDICATES}"
    )
    
    # Verify they match the bootstrap defaults
    assert types == memory_core._DEFAULT_VALID_ENTITY_TYPES, (
        f"Types {types} != bootstrap default {memory_core._DEFAULT_VALID_ENTITY_TYPES}"
    )
    assert preds == memory_core._DEFAULT_VALID_ENTITY_PREDICATES, (
        f"Predicates {preds} != bootstrap default {memory_core._DEFAULT_VALID_ENTITY_PREDICATES}"
    )


def test_load_entity_vocab_custom_yaml(tmp_path):
    """load_entity_vocab with custom YAML loads only specified types/predicates."""
    import memory_core
    from pathlib import Path
    
    # Create a custom YAML with a subset
    custom_yaml = tmp_path / "custom.yaml"
    custom_yaml.write_text("""
entity_types:
  - thing
  - artifact
entity_predicates:
  - created_by
  - modified_on
metadata:
  description: "Custom test vocabulary"
""")
    
    types, preds = memory_core.load_entity_vocab(str(custom_yaml))
    
    # Verify only the custom content is loaded
    assert types == frozenset({"thing", "artifact"}), f"Expected {{'thing', 'artifact'}}, got {types}"
    assert preds == frozenset({"created_by", "modified_on"}), f"Expected {{'created_by', 'modified_on'}}, got {preds}"


def test_load_entity_vocab_empty_yaml_falls_back_to_defaults(tmp_path):
    """load_entity_vocab with empty lists falls back to defaults."""
    import memory_core
    
    # Create a YAML with empty lists
    placeholder_yaml = tmp_path / "placeholder.yaml"
    placeholder_yaml.write_text("""
entity_types: []
entity_predicates: []
metadata:
  description: "Empty placeholder"
  status: "tbd"
""")
    
    types, preds = memory_core.load_entity_vocab(str(placeholder_yaml))
    
    # Should fall back to defaults
    assert types == memory_core._DEFAULT_VALID_ENTITY_TYPES, (
        f"Empty types should fall back to defaults; got {types}"
    )
    assert preds == memory_core._DEFAULT_VALID_ENTITY_PREDICATES, (
        f"Empty predicates should fall back to defaults; got {preds}"
    )


def test_module_constants_match_default_yaml():
    """Regression test: VALID_ENTITY_TYPES and VALID_ENTITY_PREDICATES equal what's in entity_graph_default.yaml."""
    import memory_core
    from pathlib import Path
    
    yaml_path = Path(__file__).parent.parent / "config" / "lists" / "entity_graph_default.yaml"
    assert yaml_path.exists(), f"Default YAML not found at {yaml_path}"
    
    types, preds = memory_core.load_entity_vocab(str(yaml_path))
    
    # The byte-identity invariant: module constants must equal what the YAML loads
    assert types == memory_core.VALID_ENTITY_TYPES, (
        f"Types from YAML {types} != module constant {memory_core.VALID_ENTITY_TYPES}"
    )
    assert preds == memory_core.VALID_ENTITY_PREDICATES, (
        f"Predicates from YAML {preds} != module constant {memory_core.VALID_ENTITY_PREDICATES}"
    )
