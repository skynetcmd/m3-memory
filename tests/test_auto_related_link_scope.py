"""Variant-scope guard for the auto-related-link path in memory_core.

Regression: an INSERT under one variant could auto-link to twins of another
variant when both shared content. The fix threads `variant` into
`_check_contradictions` and gates the candidate scan on
`M3_AUTO_RELATED_LINK_SCOPE_BY_VARIANT` (default ON).

These tests verify the guard at the unit level without spinning up the full
memory_write pipeline.
"""

import os
import struct
import sqlite3
import asyncio
from contextlib import contextmanager

import pytest


def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _make_db(tmp_path, items: list[dict]) -> str:
    """Build a tiny DB with memory_items + memory_embeddings populated.

    Each item dict needs: id, type, title, content, variant, embedding (list[float]).
    """
    db_path = tmp_path / "test_scope.db"
    db = sqlite3.connect(str(db_path))
    db.executescript("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            type TEXT,
            title TEXT,
            content TEXT,
            agent_id TEXT,
            is_deleted INTEGER DEFAULT 0,
            variant TEXT,
            created_at TEXT
        );
        CREATE TABLE memory_embeddings (
            memory_id TEXT PRIMARY KEY,
            embedding BLOB,
            embed_model TEXT,
            dim INTEGER
        );
    """)
    for it in items:
        db.execute(
            "INSERT INTO memory_items(id,type,title,content,agent_id,is_deleted,variant,created_at) "
            "VALUES (?,?,?,?,?,0,?,?)",
            (it["id"], it["type"], it.get("title", ""), it["content"], it.get("agent_id", ""),
             it.get("variant"), "2026-05-08T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO memory_embeddings(memory_id,embedding,embed_model,dim) VALUES (?,?,?,?)",
            (it["id"], _pack_vec(it["embedding"]), "test-embed", len(it["embedding"])),
        )
    db.commit()
    db.close()
    return str(db_path)


def _patch_memory_core_db(monkeypatch, db_path: str, scope_on: bool):
    """Bind memory_core._db() to a sqlite3 connection on db_path; set scope env.

    Avoids importlib.reload (which triggers migrate_memory side effects against
    our minimal tmp schema). Instead, monkeypatch the _db contextmanager and
    the AUTO_RELATED_LINK_SCOPE_BY_VARIANT module flag in place.
    """
    import memory_core

    @contextmanager
    def fake_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_core, "AUTO_RELATED_LINK_SCOPE_BY_VARIANT", scope_on)
    return memory_core


def test_scope_on_filters_out_other_variant(tmp_path, monkeypatch):
    """With scope ON (default), candidates from a different variant are excluded.

    Uses vectors in the related band (0.7 < cos < CONTRADICTION_THRESHOLD=0.92)
    so the path that lands in `related` (not contradiction) is exercised.
    """
    target_id = "00000000-0000-0000-0000-000000000001"
    twin_id   = "00000000-0000-0000-0000-000000000002"
    # Vectors with cosine ~0.85 (related band). Both unit-length.
    target_vec = [1.0, 0.0]
    twin_vec   = [0.85, 0.5267]  # cos(target,twin) ≈ 0.85
    items = [
        {"id": target_id, "type": "observation", "title": "title-a",
         "content": "User likes coffee", "variant": "tier-a/main",
         "embedding": target_vec},
        {"id": twin_id, "type": "observation", "title": "title-b",
         "content": "User likes tea", "variant": "tier-b/smoke",
         "embedding": twin_vec},
    ]
    db_path = _make_db(tmp_path, items)
    mc = _patch_memory_core_db(monkeypatch, db_path, scope_on=True)

    superseded, related = asyncio.run(
        mc._check_contradictions(
            item_id=target_id,
            content="User likes coffee",
            title="title-a",
            vec=target_vec,
            type_="observation",
            agent_id="",
            variant="tier-a/main",
        )
    )
    twin_ids = {r[0] for r in related}
    assert twin_id not in twin_ids, (
        "scope ON must exclude cross-variant candidate twin; got related=%r" % related
    )
    assert superseded == [], "no supersedes expected (identical-content case skipped)"


def test_scope_off_includes_other_variant(tmp_path, monkeypatch):
    """With scope OFF (legacy), cross-variant candidates remain visible."""
    target_id = "00000000-0000-0000-0000-000000000003"
    twin_id   = "00000000-0000-0000-0000-000000000004"
    target_vec = [1.0, 0.0]
    twin_vec   = [0.85, 0.5267]
    items = [
        {"id": target_id, "type": "observation", "title": "title-c",
         "content": "User likes apples", "variant": "tier-a/main",
         "embedding": target_vec},
        {"id": twin_id, "type": "observation", "title": "title-d",
         "content": "User likes oranges", "variant": "tier-b/smoke",
         "embedding": twin_vec},
    ]
    db_path = _make_db(tmp_path, items)
    mc = _patch_memory_core_db(monkeypatch, db_path, scope_on=False)

    _, related = asyncio.run(
        mc._check_contradictions(
            item_id=target_id,
            content="User likes apples",
            title="title-c",
            vec=target_vec,
            type_="observation",
            agent_id="",
            variant="tier-a/main",
        )
    )
    twin_ids = {r[0] for r in related}
    assert twin_id in twin_ids, (
        "scope OFF must restore legacy variant-blind behavior; got related=%r" % related
    )


def test_variant_none_preserves_legacy(tmp_path, monkeypatch):
    """When the inserted item has variant=None, no scope filter applies even with scope env ON."""
    target_id = "00000000-0000-0000-0000-000000000005"
    other_id  = "00000000-0000-0000-0000-000000000006"
    target_vec = [1.0, 0.0]
    other_vec  = [0.85, 0.5267]
    items = [
        {"id": target_id, "type": "observation", "title": "title-e",
         "content": "alpha", "variant": None, "embedding": target_vec},
        {"id": other_id, "type": "observation", "title": "title-f",
         "content": "beta", "variant": "some/variant", "embedding": other_vec},
    ]
    db_path = _make_db(tmp_path, items)
    mc = _patch_memory_core_db(monkeypatch, db_path, scope_on=True)

    _, related = asyncio.run(
        mc._check_contradictions(
            item_id=target_id,
            content="alpha",
            title="title-e",
            vec=target_vec,
            type_="observation",
            agent_id="",
            variant=None,
        )
    )
    other_ids = {r[0] for r in related}
    assert other_id in other_ids, (
        "variant=None must preserve legacy variant-blind candidate scan; got related=%r" % related
    )
