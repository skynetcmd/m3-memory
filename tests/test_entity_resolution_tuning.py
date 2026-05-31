"""Tests for entity resolution threshold tuning (Token-Jaccard and Cosine Similarity).
Verifies that entity resolution thresholds behave correctly under environmental override
and different levels of naming drift.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import memory_core
import memory.entity as me
from memory.entity import _resolve_entity, _resolve_entity_async, _token_jaccard
from conftest import create_full_main_schema


def test_token_jaccard_calculation():
    """Verify raw _token_jaccard calculation over various naming drift patterns."""
    # Perfect match after stripping punctuation & lowercasing
    assert _token_jaccard("Alex Johnson", "alex johnson!!!") == 1.0
    
    # 2 out of 3 tokens match: {"alex", "johnson"} vs {"alex", "j", "johnson"}
    # intersection = 2, union = 3 => 2/3 ≈ 0.667
    assert abs(_token_jaccard("Alex Johnson", "Alex J. Johnson") - 0.666666666) < 1e-5
    
    # 2 out of 3 tokens match: {"google", "deepmind"} vs {"google", "deepmind", "llc"}
    assert abs(_token_jaccard("Google DeepMind", "Google DeepMind LLC") - 0.666666666) < 1e-5
    
    # 1 out of 3 tokens match: {"acme", "corp"} vs {"acme", "inc"}
    # intersection = {"acme"} (1), union = {"acme", "corp", "inc"} (3) => 1/3 ≈ 0.333
    assert abs(_token_jaccard("Acme Corp", "Acme Inc") - 0.333333333) < 1e-5


def test_fuzzy_threshold_resolution_tuning(monkeypatch, tmp_path):
    """Verify that changing ENTITY_RESOLVE_FUZZY_MIN alters the resolution behavior on naming drift."""
    db_path = tmp_path / "test_fuzzy.db"
    create_full_main_schema(db_path)
    
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    
    # 1. With a high threshold (e.g. 0.85, default), "Google DeepMind" and "Google DeepMind LLC"
    # should NOT merge. Jaccard is ~0.67, which is < 0.85.
    monkeypatch.setattr(me, "ENTITY_RESOLVE_FUZZY_MIN", 0.85)
    
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        eid = memory_core._create_entity("Google DeepMind", "organization", {}, conn)
        conn.commit()
        
        # Resolve should return None because Jaccard is below the threshold of 0.85
        resolved = _resolve_entity("Google DeepMind LLC", "organization", conn)
        assert resolved is None, "Should not merge under high fuzzy threshold"

    # 2. If we lower the threshold to 0.60, they should merge. Jaccard is ~0.67, which is >= 0.60.
    monkeypatch.setattr(me, "ENTITY_RESOLVE_FUZZY_MIN", 0.60)
    
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        resolved = _resolve_entity("Google DeepMind LLC", "organization", conn)
        assert resolved == eid, "Should merge successfully under lower fuzzy threshold"


@pytest.mark.asyncio
async def test_cosine_threshold_resolution_tuning(monkeypatch, tmp_path):
    """Verify that changing ENTITY_RESOLVE_COSINE_MIN alters async resolution behavior for semantic drift."""
    db_path = tmp_path / "test_cosine.db"
    create_full_main_schema(db_path)
    
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    
    # Let's stub embedding function to return controlled vectors
    # "Acme Corp" and "Acme Corporation" are different tokens (Jaccard = 0.5), so fuzzy tier won't match.
    # We stub their embedding cosine similarity to be exactly 0.90
    # Vector A: [1.0, 0.0]
    # Vector B: [0.90, 0.43588989] (since 0.9^2 + 0.43588989^2 = 1.0, and dot product is 0.9)
    embed_map = {
        "Acme Corp": ([1.0, 0.0], "stub"),
        "Acme Corporation": ([0.90, 0.43588989], "stub"),
    }
    
    async def stub_embed(text: str):
        return embed_map.get(text, ([0.0, 1.0], "stub"))
        
    monkeypatch.setattr(memory_core, "_embed", stub_embed)
    
    # 1. With a high cosine threshold (e.g. 0.95), they should NOT merge (0.90 < 0.95)
    monkeypatch.setattr(me, "ENTITY_RESOLVE_FUZZY_MIN", 0.85)
    monkeypatch.setattr(me, "ENTITY_RESOLVE_COSINE_MIN", 0.95)
    
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        eid = memory_core._create_entity("Acme Corp", "organization", {}, conn)
        conn.commit()
        
        # Verify sync resolution doesn't merge
        assert _resolve_entity("Acme Corporation", "organization", conn) is None
        
        # Verify async resolution doesn't merge
        resolved_async = await _resolve_entity_async("Acme Corporation", "organization", conn)
        assert resolved_async is None, "Should not merge under high cosine threshold"

    # 2. If we lower the cosine threshold to 0.88, they should merge (0.90 >= 0.88)
    monkeypatch.setattr(me, "ENTITY_RESOLVE_COSINE_MIN", 0.88)
    
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        resolved_async = await _resolve_entity_async("Acme Corporation", "organization", conn)
        assert resolved_async == eid, "Should merge successfully under lower cosine threshold"
