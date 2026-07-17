"""Unit tests for sqlite-vec dynamic integration and fallback logic.

Verifies:
1. When sqlite-vec is active, the database connection executes a SQL query that uses
   `vec_distance_cosine` directly and scores vectors in SQL.
2. When sqlite-vec is inactive (fails to load or not present), the system falls back
   gracefully to the FFI / Python _cosine_batch_packed pipeline.
"""
from __future__ import annotations

import os
import sqlite3
import struct
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


def test_detect_sqlite_vec_active():
    """_detect_sqlite_vec returns True when vec_version() succeeds on a real connection."""
    from memory.search import _detect_sqlite_vec

    executed = []
    class ActiveConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            executed.append(sql)
            mock_res = mock.Mock()
            mock_res.fetchone.return_value = ("v0.1.0",)
            return mock_res

    conn = sqlite3.connect(":memory:", factory=ActiveConnection)
    try:
        assert _detect_sqlite_vec(conn) is True
        assert executed == ["SELECT vec_version()"]
    finally:
        conn.close()


def test_detect_sqlite_vec_inactive():
    """_detect_sqlite_vec returns False when vec_version() raises an exception on a real connection."""
    from memory.search import _detect_sqlite_vec

    conn = sqlite3.connect(":memory:")
    try:
        # A clean Connection raises sqlite3.OperationalError for vec_version(), returning False
        assert _detect_sqlite_vec(conn) is False
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_search_uses_sqlite_vec_when_active(monkeypatch):
    """When sqlite-vec is active, semantic search uses vec_distance_cosine directly in SQL."""
    import memory_core

    from memory import search

    # Set skip migrations flag
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")

    # Mock _detect_sqlite_vec to return True
    monkeypatch.setattr(search, "_detect_sqlite_vec", lambda db: True)

    # Mock _embed to return a dummy query vector
    dummy_q_vec = [0.1, 0.2, 0.3]
    async def mock_embed(query):
        return dummy_q_vec, 1.0
    monkeypatch.setattr(search, "_embed", mock_embed)
    monkeypatch.setattr(memory_core, "_embed", mock_embed)

    # Mock observation gates to return False to avoid NameError inside lazy-loader boundaries
    monkeypatch.setattr(search, "_prefer_observations_gate", lambda: False)
    monkeypatch.setattr(search, "_two_stage_observations_gate", lambda: False)
    monkeypatch.setattr(memory_core, "_prefer_observations_gate", lambda: False)
    monkeypatch.setattr(memory_core, "_two_stage_observations_gate", lambda: False)

    # Record executed SQL queries
    executed_queries = []

    class MockCursor:
        def fetchall(self):
            # Return dummy database hits with bm25_score
            return [
                {
                    "id": "mem1",
                    "content": "test content",
                    "title": "test title",
                    "type": "concept",
                    "importance": 0.5,
                    "embedding": b"\x00" * 12,
                    "vec_score": 0.85,
                    "bm25_score": 0.0,
                }
            ]

    class ActiveSearchConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            params = args[0] if args else ()
            executed_queries.append((sql, params))
            return MockCursor()

    conn = sqlite3.connect(":memory:", factory=ActiveSearchConnection)
    try:
        # Stub the DB context manager to yield our connection
        class MockDBContext:
            def __enter__(self):
                return conn
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

        monkeypatch.setattr(memory_core, "_db", lambda: MockDBContext())
        monkeypatch.setattr(search, "_db", lambda: MockDBContext())
        monkeypatch.setattr(search, "EMBED_DIM", 3)

        results = await search.memory_search_scored_impl("test query", search_mode="semantic", k=5)

        assert len(results) == 1
        score, hit = results[0]
        assert hit["id"] == "mem1"

        # Verify that vec_distance_cosine was in the executed query
        assert len(executed_queries) == 1
        sql, params = executed_queries[0]
        assert "vec_distance_cosine" in sql
        assert len(params) >= 1
        # First parameter must be the packed query vector blob
        expected_blob = struct.pack("3f", *dummy_q_vec)
        assert params[0] == expected_blob
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_search_falls_back_when_sqlite_vec_inactive(monkeypatch):
    """When sqlite-vec is inactive, search falls back to FFI/Python _cosine_batch_packed."""
    import memory_core

    from memory import search

    # Set skip migrations flag
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")

    # Mock _detect_sqlite_vec to return False
    monkeypatch.setattr(search, "_detect_sqlite_vec", lambda db: False)

    # Mock _embed to return a dummy query vector
    dummy_q_vec = [1.0, 0.0, 0.0]
    async def mock_embed(query):
        return dummy_q_vec, 1.0
    monkeypatch.setattr(search, "_embed", mock_embed)
    monkeypatch.setattr(memory_core, "_embed", mock_embed)

    # Mock observation gates to return False to avoid NameError inside lazy-loader boundaries
    monkeypatch.setattr(search, "_prefer_observations_gate", lambda: False)
    monkeypatch.setattr(search, "_two_stage_observations_gate", lambda: False)
    monkeypatch.setattr(memory_core, "_prefer_observations_gate", lambda: False)
    monkeypatch.setattr(memory_core, "_two_stage_observations_gate", lambda: False)

    # Record executed SQL queries
    executed_queries = []

    class MockCursor:
        def fetchall(self):
            # Return dummy database hit with a packed float32 [1.0, 0.0, 0.0] embedding (length 12)
            # Struct format: 3f (3 float32s)
            packed_emb = struct.pack("3f", 1.0, 0.0, 0.0)
            return [
                {
                    "id": "mem2",
                    "content": "fallback content",
                    "title": "fallback title",
                    "type": "fact",
                    "importance": 0.9,
                    "embedding": packed_emb,
                    "bm25_score": 0.0,
                }
            ]

    class InactiveSearchConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            params = args[0] if args else ()
            executed_queries.append((sql, params))
            return MockCursor()

    conn = sqlite3.connect(":memory:", factory=InactiveSearchConnection)
    try:
        # Stub the DB context manager to yield our connection
        class MockDBContext:
            def __enter__(self):
                return conn
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

        monkeypatch.setattr(memory_core, "_db", lambda: MockDBContext())
        monkeypatch.setattr(search, "_db", lambda: MockDBContext())
        monkeypatch.setattr(search, "EMBED_DIM", 3)

        results = await search.memory_search_scored_impl("test query", search_mode="semantic", k=5)

        assert len(results) == 1
        score, hit = results[0]
        assert hit["id"] == "mem2"

        # Verify that vec_distance_cosine was NOT in the executed query
        assert len(executed_queries) == 1
        sql, params = executed_queries[0]
        assert "vec_distance_cosine" not in sql
    finally:
        conn.close()
