"""Tests for the pluggable entity-extraction backend module bin/memory/extraction.py."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

# Ensure bin/ is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory.extraction import (
    RuleBasedExtractor,
    canonicalize_relationship,
    extract_entities_impl,
    get_configured_extractor,
    normalize_entity_id,
)

from conftest import create_full_main_schema
from memory import config as _config
from memory import entity as _entity_mod
from memory import write as _write_mod


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "1")


@pytest.fixture(autouse=True)
def _mock_embeddings(monkeypatch):
    async def mock_embed_canonical_cached(canonical_name: str) -> list[float] | None:
        import hashlib
        h = hashlib.sha256(canonical_name.encode("utf-8")).digest()
        dim = getattr(_config, "EMBED_DIM", 1024)
        vec = []
        for i in range(dim):
            val = (h[i % len(h)] / 255.0) - 0.5
            vec.append(val)
        # Normalize to unit vector
        mag = sum(x*x for x in vec) ** 0.5
        if mag > 0:
            vec = [x / mag for x in vec]
        return vec
    monkeypatch.setattr(_entity_mod, "_embed_canonical_cached", mock_embed_canonical_cached)



def _init_test_db(db_path, monkeypatch):
    create_full_main_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setattr(_config, "DB_PATH", str(db_path))


def test_normalization():
    """Test entity ID normalization and relationship canonicalization."""
    assert normalize_entity_id("John Doe", "person") == "person:john_doe"
    assert normalize_entity_id("Roanoke!", "place") == "place:roanoke"
    assert normalize_entity_id("Rust Lang", "skill") == "skill:rust_lang"

    assert canonicalize_relationship("lives in") == "lives_in"
    assert canonicalize_relationship("Works_At") == "works_at"


@pytest.mark.asyncio
async def test_rule_based_extractor():
    """Test that the RuleBasedExtractor extracts entities and links based on heuristics."""
    ext = RuleBasedExtractor()
    text = "John Doe lives in Roanoke and joined Google Corp. He is learning Rust."
    res = await ext.extract(text)

    entities = res["entities"]
    relationships = res["relationships"]

    # Verify entity names and types
    names = [e["canonical_name"] for e in entities]
    assert "John Doe" in names
    assert "Roanoke" in names
    assert "Google Corp" in names
    assert "Rust" in names

    types = {e["canonical_name"]: e["entity_type"] for e in entities}
    assert types["John Doe"] == "person"
    assert types["Roanoke"] == "place"
    assert types["Google Corp"] == "organization"
    assert types["Rust"] == "topic"

    # Verify relationships
    rels = [(r["from_entity"], r["to_entity"], r["predicate"]) for r in relationships]
    assert ("John Doe", "Roanoke", "located_in") in rels
    assert ("John Doe", "Google Corp", "works_at") in rels
    assert ("John Doe", "Rust", "prefers") in rels


def test_factory_config(monkeypatch):
    """Test the extractor factory instantiates subclasses based on configuration."""
    monkeypatch.setenv("M3_EXTRACTION_TYPE", "rule_based")
    ext = get_configured_extractor()
    assert isinstance(ext, RuleBasedExtractor)

    monkeypatch.setenv("M3_EXTRACTION_TYPE", "gemini")
    ext = get_configured_extractor()
    from memory.extraction import LLMExtractor
    assert isinstance(ext, LLMExtractor)
    assert ext.provider == "gemini"


@pytest.mark.asyncio
async def test_extract_entities_impl_mcp(monkeypatch):
    """Test the extract_entities_impl tool returns valid JSON matching the schema."""
    monkeypatch.setenv("M3_EXTRACTION_TYPE", "rule_based")
    raw_json = await extract_entities_impl("John Doe lives in Roanoke.")
    res = json.loads(raw_json)

    assert "entities" in res
    assert "relationships" in res

    names = [e["canonical_name"] for e in res["entities"]]
    assert "John Doe" in names
    assert "Roanoke" in names


@pytest.mark.asyncio
async def test_extract_pending_drain(tmp_path, monkeypatch):
    """Test that extract_pending_impl executes and processes the queue using the pluggable extractor."""
    db_file = tmp_path / "test_extraction.db"
    _init_test_db(db_file, monkeypatch)

    monkeypatch.setenv("M3_EXTRACTION_TYPE", "rule_based")

    # Insert a raw memory item and enqueue it
    memory_id = "test-mem-123"
    content = "John Doe lives in Roanoke."

    from memory.db import _db
    with _db() as db:
        db.execute(
            "INSERT INTO memory_items (id, type, content, is_deleted) VALUES (?, ?, ?, 0)",
            (memory_id, "note", content),
        )
        db.execute(
            "INSERT INTO entity_extraction_queue (memory_id, attempts) VALUES (?, 0)",
            (memory_id,),
        )

    # Drain the queue using extract_pending_impl
    res = await _entity_mod.extract_pending_impl(dry_run=False)

    assert res["processed"] == 1
    assert res["succeeded"] == 1
    assert res["failed"] == 0

    # Verify entity and relationship were written to the DB
    with _db() as db:
        # Check entities
        entities = db.execute("SELECT canonical_name, entity_type FROM entities").fetchall()
        names = [e["canonical_name"] for e in entities]
        assert "John Doe" in names
        assert "Roanoke" in names

        # Check links
        links = db.execute("SELECT mention_text FROM memory_item_entities WHERE memory_id = ?", (memory_id,)).fetchall()
        assert len(links) >= 2

        # Check queue was cleared
        queue = db.execute("SELECT COUNT(*) FROM entity_extraction_queue").fetchone()[0]
        assert queue == 0


@pytest.mark.asyncio
async def test_write_through_mode(tmp_path, monkeypatch):
    """Test that memory_write_impl automatically extracts and links entities in write-through mode."""
    db_file = tmp_path / "test_write_through.db"
    _init_test_db(db_file, monkeypatch)

    # Enable write-through
    monkeypatch.setenv("M3_EXTRACTION_WRITE_THROUGH", "1")
    monkeypatch.setenv("M3_EXTRACTION_TYPE", "rule_based")

    # Write new memory
    await _write_mod.memory_write_impl(
        type="note",
        content="John Doe lives in Roanoke.",
        title="John's Move",
        embed=False,  # Skip embedding to avoid LLM calls
    )

    # Give the async background task a brief moment to finish
    await asyncio.sleep(0.1)

    # Verify entities and relationships were written immediately to the DB
    from memory.db import _db
    with _db() as db:
        entities = db.execute("SELECT canonical_name, entity_type FROM entities").fetchall()
        names = [e["canonical_name"] for e in entities]
        assert "John Doe" in names
        assert "Roanoke" in names

        links = db.execute("SELECT mention_text FROM memory_item_entities").fetchall()
        assert len(links) >= 2
