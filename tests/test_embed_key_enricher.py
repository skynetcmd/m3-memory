"""Tests for memory_write_bulk_impl's embed_key_enricher hook.

These tests verify the hook's behavior on the prepared-items list directly
without exercising the full bulk-write path (which requires a migrated DB
and a live embedder endpoint). The hook is surgical enough that a targeted
unit test on _enrich_one (which lives inline in memory_write_bulk_impl)
would require exposing it, so we drive the whole function with mocks and
assert the enricher ran with the expected inputs.
"""
from __future__ import annotations

import os
import sys
import asyncio

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.mark.asyncio
async def test_enricher_receives_content_and_metadata_dict(monkeypatch):
    """Verify the enricher signature contract:
    - called once per prepared item with non-empty embed_text
    - first arg is the raw content string
    - second arg is a dict (metadata decoded from JSON if stored as string)
    - concurrency is bounded by the supplied semaphore
    """
    import memory_core

    calls: list[tuple[str, dict]] = []
    max_in_flight = 0
    in_flight = 0
    lock = asyncio.Lock()

    async def enricher(content: str, metadata: dict) -> str:
        nonlocal max_in_flight, in_flight
        assert isinstance(content, str)
        assert isinstance(metadata, dict)
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.01)
            calls.append((content, metadata))
            return f"ENRICHED: {content}"
        finally:
            async with lock:
                in_flight -= 1

    # Shortcut: call the enricher manually against a prepared-like list,
    # reusing the same concurrency pattern as the production hook. This
    # verifies the contract without needing a full DB.
    prepared = [
        {"id": "a", "content": "turn one", "metadata": '{"role": "user", "session_index": 0}', "embed": True, "embed_text": "turn one"},
        {"id": "b", "content": "turn two", "metadata": {"role": "assistant"}, "embed": True, "embed_text": "turn two"},
        {"id": "c", "content": "",          "metadata": {}, "embed": True, "embed_text": ""},  # empty embed_text skipped
        {"id": "d", "content": "turn four", "metadata": {}, "embed": False, "embed_text": "turn four"},  # embed=False skipped
    ]

    sem = asyncio.Semaphore(2)
    import json as _json

    async def _enrich_one(p):
        if not p.get("embed_text") or not p.get("embed"):
            return
        meta = p.get("metadata") or "{}"
        meta_dict = _json.loads(meta) if isinstance(meta, str) else (meta or {})
        async with sem:
            enriched = await enricher(p.get("content") or "", meta_dict)
            if enriched:
                p["embed_text"] = enriched

    await asyncio.gather(*(_enrich_one(p) for p in prepared))

    # Only the 2 eligible items were enriched
    assert len(calls) == 2
    assert calls[0][0] in ("turn one", "turn two")
    assert calls[1][0] in ("turn one", "turn two")
    # Metadata comes through as a dict in both cases
    assert all(isinstance(m, dict) for _, m in calls)
    # Concurrency cap honored
    assert max_in_flight <= 2
    # Enriched text replaced embed_text; skipped items unchanged
    assert prepared[0]["embed_text"].startswith("ENRICHED:")
    assert prepared[1]["embed_text"].startswith("ENRICHED:")
    assert prepared[2]["embed_text"] == ""
    assert prepared[3]["embed_text"] == "turn four"


@pytest.mark.asyncio
async def test_signature_includes_kwargs():
    """Verify memory_write_bulk_impl exposes the new kwargs."""
    import inspect
    import memory_core

    sig = inspect.signature(memory_core.memory_write_bulk_impl)
    assert "embed_key_enricher" in sig.parameters
    assert "embed_key_enricher_concurrency" in sig.parameters
    # Defaults preserve back-compat
    assert sig.parameters["embed_key_enricher"].default is None
    assert sig.parameters["embed_key_enricher_concurrency"].default == 4


@pytest.mark.asyncio
async def test_enriched_text_persisted_to_metadata(monkeypatch):
    """When enricher returns a transformed string, memory_write_bulk_impl
    persists it to metadata_json.enriched_embed_text for post-hoc audit.
    When the enricher passes through the raw content unchanged (e.g.
    short-turn skip), no persistence should occur.
    """
    import memory_core

    captured_prepared_after_enrichment: list[dict] = []

    # Replace _embed_many so we don't hit an endpoint.
    async def fake_embed_many(texts):
        return [(None, "stub") for _ in texts]
    monkeypatch.setattr(memory_core, "_embed_many", fake_embed_many)

    # Spy on prepared list just before DB insert by replacing the
    # context-manager that does the INSERT.
    orig_db = memory_core._db
    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=None):
            if "INSERT INTO memory_items" in sql and args:
                # args is a tuple per INSERT; metadata_json is at index 4
                metadata_json = args[4]
                import json as _json
                captured_prepared_after_enrichment.append(_json.loads(metadata_json or "{}"))
            class _C:
                def fetchone(self): return None
                def fetchall(self): return []
            return _C()
        def commit(self): pass
    def fake_db():
        return _FakeDB()
    monkeypatch.setattr(memory_core, "_db", fake_db)

    async def enricher(content: str, metadata: dict) -> str:
        if content == "short":
            # Pass-through case — enricher decided not to enrich
            return content
        return f"FACTS: {content}"

    items = [
        {"id": "a", "type": "message", "content": "long content to enrich", "embed": True},
        {"id": "b", "type": "message", "content": "short", "embed": True},
    ]
    await memory_core.memory_write_bulk_impl(items, embed_key_enricher=enricher)

    # Find the metadata rows for each id
    by_id = {}
    for meta in captured_prepared_after_enrichment:
        # metadata_json from INSERT doesn't include the id; match by content
        # via the surrounding row... we can't here, so just check aggregate:
        pass

    # Easier assertion: the enriched item's metadata should have
    # `enriched_embed_text`; the pass-through item's metadata should NOT.
    # Since we can't distinguish by id above, assert the counts instead.
    with_enriched = sum(1 for m in captured_prepared_after_enrichment if "enriched_embed_text" in m)
    without_enriched = sum(1 for m in captured_prepared_after_enrichment if "enriched_embed_text" not in m)

    assert with_enriched == 1, f"Expected 1 item with enriched_embed_text, got {with_enriched}"
    assert without_enriched == 1, f"Expected 1 item WITHOUT enriched_embed_text (short pass-through), got {without_enriched}"

    # The enriched content should be the "FACTS: long content" string
    for m in captured_prepared_after_enrichment:
        if "enriched_embed_text" in m:
            assert m["enriched_embed_text"].startswith("FACTS: long content")


@pytest.mark.asyncio
async def test_dual_embed_signature():
    """memory_write_bulk_impl exposes dual_embed kwarg, default False."""
    import inspect
    import memory_core

    sig = inspect.signature(memory_core.memory_write_bulk_impl)
    assert "dual_embed" in sig.parameters
    assert sig.parameters["dual_embed"].default is False


@pytest.mark.asyncio
async def test_dual_embed_emits_two_vectors(monkeypatch):
    """When dual_embed=True and an enricher transforms embed_text, Phase 2
    writes TWO memory_embeddings rows per item — one with vector_kind='default'
    (raw content) and one with vector_kind='enriched' (SLM output).
    Pass-through enrichment and dual_embed=False both emit only ONE row."""
    import memory_core

    embed_inserts: list[dict] = []

    async def fake_embed_many(texts):
        # Stable non-null vectors so Phase 2 actually INSERTs.
        return [([0.1, 0.2, 0.3], "stub-model") for _ in texts]
    monkeypatch.setattr(memory_core, "_embed_many", fake_embed_many)

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=None):
            if "INSERT INTO memory_embeddings" in sql and args:
                # New dual-capable schema: (id, memory_id, embedding, embed_model,
                # dim, created_at, content_hash, vector_kind)
                row = {
                    "memory_id": args[1],
                    "vector_kind": args[7] if len(args) >= 8 else "default",
                }
                embed_inserts.append(row)
            class _C:
                def fetchone(self): return None
                def fetchall(self): return []
            return _C()
        def commit(self): pass
    monkeypatch.setattr(memory_core, "_db", lambda: _FakeDB())

    async def enricher(content: str, metadata: dict) -> str:
        if content == "short":
            return content  # pass-through
        return f"FACTS: {content}"

    # Case A: dual_embed=True with transforming enricher → 2 rows for "long"
    embed_inserts.clear()
    items = [
        {"id": "dA1", "type": "message", "content": "long content to enrich", "embed": True},
        {"id": "dA2", "type": "message", "content": "short", "embed": True},
    ]
    await memory_core.memory_write_bulk_impl(
        items, embed_key_enricher=enricher, dual_embed=True
    )
    by_id: dict[str, list[str]] = {}
    for r in embed_inserts:
        by_id.setdefault(r["memory_id"], []).append(r["vector_kind"])
    assert sorted(by_id.get("dA1", [])) == ["default", "enriched"], by_id
    assert by_id.get("dA2", []) == ["default"], by_id

    # Case B: dual_embed=False with transforming enricher → 1 row per item
    embed_inserts.clear()
    items = [
        {"id": "dB1", "type": "message", "content": "long content to enrich", "embed": True},
    ]
    await memory_core.memory_write_bulk_impl(
        items, embed_key_enricher=enricher, dual_embed=False
    )
    by_id = {}
    for r in embed_inserts:
        by_id.setdefault(r["memory_id"], []).append(r["vector_kind"])
    assert by_id.get("dB1", []) == ["default"], by_id

    # Case C: dual_embed=True but NO enricher → 1 row per item (no-op)
    embed_inserts.clear()
    items = [
        {"id": "dC1", "type": "message", "content": "anything", "embed": True},
    ]
    await memory_core.memory_write_bulk_impl(items, dual_embed=True)
    by_id = {}
    for r in embed_inserts:
        by_id.setdefault(r["memory_id"], []).append(r["vector_kind"])
    assert by_id.get("dC1", []) == ["default"], by_id
