"""Regression tests for chroma_sync_queue orphan-prevention.

Bug history: chroma_sync_queue rows were inserted in Phase 1 (alongside
memory_items) but memory_embeddings INSERTs ran in Phase 2 after _embed_many.
When _embed_many returned (None, model) for a slot (embed-server failure,
context-size 400), Phase 2 silently skipped the embedding INSERT but the
queue row had already committed. Result: drainable-forever orphan queue rows.

Fix: move the queue INSERT into Phase 2 inside the `if vec:` branch.
These tests pin that behavior on the bulk path.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.mark.asyncio
async def test_bulk_write_skips_queue_on_embed_failure(monkeypatch):
    """If _embed_many returns (None, model) for one slot, that item's
    memory_embeddings AND chroma_sync_queue inserts are both skipped,
    while items with successful embeds get both. memory_items always lands.
    """
    import memory_core

    item_inserts: list[str] = []
    embedding_inserts: list[str] = []
    queue_inserts: list[str] = []
    history_inserts: list[str] = []

    async def fake_embed_many(texts):
        # First text succeeds, second fails (None vec), third succeeds.
        results = []
        for i, _t in enumerate(texts):
            if i == 1:
                results.append((None, "stub-fail"))
            else:
                results.append(([0.1, 0.2, 0.3], "stub-ok"))
        return results

    monkeypatch.setattr(memory_core, "_embed_many", fake_embed_many)

    class _FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=None):
            s = sql or ""
            if "INSERT INTO memory_items" in s and args:
                item_inserts.append(args[0])
            elif "INSERT INTO memory_embeddings" in s and args:
                embedding_inserts.append(args[1])
            elif "INSERT INTO chroma_sync_queue" in s and args:
                queue_inserts.append(args[0])
            elif "INSERT INTO memory_history" in s and args:
                history_inserts.append(args[0] if args else "")

            class _C:
                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

            return _C()

        def commit(self):
            pass

    monkeypatch.setattr(memory_core, "_db", lambda: _FakeDB())

    # Three distinct contents so dedup-by-hash doesn't collapse them.
    items = [
        {"id": "id-ok-1",   "type": "note", "content": "alpha distinct", "embed": True},
        {"id": "id-fail-1", "type": "note", "content": "beta distinct",  "embed": True},
        {"id": "id-ok-2",   "type": "note", "content": "gamma distinct", "embed": True},
    ]

    await memory_core.memory_write_bulk_impl(items, enrich=False)

    # Phase 1 commits all three items regardless of embed outcome.
    assert set(item_inserts) == {"id-ok-1", "id-fail-1", "id-ok-2"}, item_inserts

    # Embeddings + queue ONLY for the two successful slots.
    assert set(embedding_inserts) == {"id-ok-1", "id-ok-2"}, embedding_inserts
    assert set(queue_inserts) == {"id-ok-1", "id-ok-2"}, queue_inserts

    # The orphan case: failed-embed item must NOT be in queue.
    assert "id-fail-1" not in queue_inserts


@pytest.mark.asyncio
async def test_bulk_write_all_succeed_enqueues_all(monkeypatch):
    """Sanity: when every embed succeeds, every item gets a queue row."""
    import memory_core

    queue_inserts: list[str] = []
    embedding_inserts: list[str] = []

    async def fake_embed_many(texts):
        return [([0.1, 0.2, 0.3], "stub") for _ in texts]

    monkeypatch.setattr(memory_core, "_embed_many", fake_embed_many)

    class _FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=None):
            s = sql or ""
            if "INSERT INTO memory_embeddings" in s and args:
                embedding_inserts.append(args[1])
            elif "INSERT INTO chroma_sync_queue" in s and args:
                queue_inserts.append(args[0])

            class _C:
                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

            return _C()

        def commit(self):
            pass

    monkeypatch.setattr(memory_core, "_db", lambda: _FakeDB())

    items = [
        {"id": "ok-a", "type": "note", "content": "one", "embed": True},
        {"id": "ok-b", "type": "note", "content": "two", "embed": True},
    ]
    await memory_core.memory_write_bulk_impl(items, enrich=False)

    assert sorted(queue_inserts) == ["ok-a", "ok-b"]
    assert sorted(embedding_inserts) == ["ok-a", "ok-b"]


@pytest.mark.asyncio
async def test_bulk_write_all_fail_enqueues_none(monkeypatch):
    """When every embed fails, no queue rows are written (no orphans)."""
    import memory_core

    queue_inserts: list[str] = []
    item_inserts: list[str] = []

    async def fake_embed_many(texts):
        return [(None, "stub-fail") for _ in texts]

    monkeypatch.setattr(memory_core, "_embed_many", fake_embed_many)

    class _FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=None):
            s = sql or ""
            if "INSERT INTO memory_items" in s and args:
                item_inserts.append(args[0])
            elif "INSERT INTO chroma_sync_queue" in s and args:
                queue_inserts.append(args[0])

            class _C:
                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

            return _C()

        def commit(self):
            pass

    monkeypatch.setattr(memory_core, "_db", lambda: _FakeDB())

    items = [
        {"id": "f-1", "type": "note", "content": "one", "embed": True},
        {"id": "f-2", "type": "note", "content": "two", "embed": True},
    ]
    await memory_core.memory_write_bulk_impl(items, enrich=False)

    # Items still land in Phase 1 (we don't roll back the row).
    assert sorted(item_inserts) == ["f-1", "f-2"]
    # Critical: no queue rows for the failed embeds.
    assert queue_inserts == []
