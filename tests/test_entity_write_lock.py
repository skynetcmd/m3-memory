"""Entity resolution must not hold SQLite's write lock across embedding awaits.

`_run_entity_extractor` resolved every entity inside ONE write transaction, and
each resolve awaits an embedding call. After the first entity was created the
transaction held the write lock through every later embedding round trip — at
30-60 entities per memory, many seconds per row. With the loop draining a
backlog back-to-back, every other main-store writer failed "database is locked"
(measured 2026-10-04 with CLI memory writes).

The fake embedder below probes the lock from a SECOND connection at the exact
moment resolution is awaiting: `BEGIN IMMEDIATE` with no wait must succeed.
Real store (template schema), real `_db()`; only the embedder is faked.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _vec(name: str) -> list[float]:
    """Deterministic, near-orthogonal per name: nothing resolves by cosine."""
    i = int(hashlib.sha256(name.encode()).hexdigest(), 16) % 64
    return [1.0 if k == i else 0.0 for k in range(64)]


@pytest.fixture
def store(tmp_path, monkeypatch):
    from conftest import create_full_main_schema

    db = tmp_path / "t.db"
    create_full_main_schema(db)
    monkeypatch.setenv("M3_DATABASE", str(db))
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "true")
    with sqlite3.connect(str(db)) as c:
        c.execute("INSERT INTO memory_items (id, type, title, content, created_at, is_deleted) "
                  "VALUES ('m1', 'note', 't', 'Alpha Industries hired Beta Logistics', "
                  "'2026-01-01T00:00:00Z', 0)")
        # An existing entity of the same type, so resolution reaches the
        # embedding tier (no candidates means no await at all).
        c.execute("INSERT INTO entities (id, canonical_name, entity_type, attributes_json, content_hash) "
                  "VALUES ('e0', 'Zeta Holdings', 'organization', '{}', 'h0')")
        c.commit()
    return db


def test_no_write_lock_is_held_while_resolution_awaits(store, monkeypatch):
    ent = importlib.import_module("memory.entity")
    try:
        import memory.embed as me
        me._ENTITY_NAME_EMBED_CACHE.clear()
    except (ImportError, AttributeError):
        pass
    probes: list[str] = []

    async def probing_embed(name: str):
        p = sqlite3.connect(str(store), timeout=0)
        try:
            p.execute("BEGIN IMMEDIATE")
            p.rollback()
            probes.append("free")
        except sqlite3.OperationalError as e:
            probes.append(f"LOCKED ({e})")
        finally:
            p.close()
        await asyncio.sleep(0)
        return _vec(name)

    monkeypatch.setattr(ent, "_embed_canonical_cached", probing_embed)

    async def extractor(_text):
        return {"entities": [
            {"canonical_name": "Alpha Industries", "entity_type": "organization"},
            {"canonical_name": "Beta Logistics", "entity_type": "organization"},
            {"canonical_name": "Gamma Freight", "entity_type": "organization"},
        ], "relationships": []}

    asyncio.run(ent._run_entity_extractor("m1", "Alpha Industries hired Beta Logistics", extractor))

    assert probes, "precondition: resolution never reached the embedding tier"
    locked = [p for p in probes if p != "free"]
    assert not locked, f"write lock held during {len(locked)}/{len(probes)} embedding awaits: {locked[:2]}"
    with sqlite3.connect(str(store)) as c:
        names = {r[0] for r in c.execute("SELECT canonical_name FROM entities")}
        links = c.execute("SELECT COUNT(*) FROM memory_item_entities WHERE memory_id='m1'").fetchone()[0]
    assert {"Alpha Industries", "Beta Logistics", "Gamma Freight"} <= names
    assert links == 3


def test_a_name_repeated_in_one_extraction_creates_one_entity(store, monkeypatch):
    """Both copies resolve to 'new' in phase 1; the phase-2 re-check must find
    the first one created in the same transaction instead of duplicating it."""
    ent = importlib.import_module("memory.entity")

    async def embed(name):
        return _vec(name)

    monkeypatch.setattr(ent, "_embed_canonical_cached", embed)

    async def extractor(_text):
        return {"entities": [
            {"canonical_name": "Alpha Industries", "entity_type": "organization"},
            {"canonical_name": "Alpha Industries", "entity_type": "organization", "mention_offset": 40},
        ], "relationships": []}

    asyncio.run(ent._run_entity_extractor("m1", "x", extractor))
    with sqlite3.connect(str(store)) as c:
        n = c.execute("SELECT COUNT(*) FROM entities WHERE canonical_name='Alpha Industries'").fetchone()[0]
    assert n == 1


def test_no_connection_is_open_while_resolution_awaits(store, monkeypatch):
    """Backend-agnostic: no `_db()` connection may be open across an embedding
    await. On SQLite an open transaction can hold the write lock; on
    PostgreSQL even a read leaves a pooled connection idle in transaction
    (psycopg2 is not autocommit), pinning a snapshot and holding back VACUUM."""
    import contextlib

    ent = importlib.import_module("memory.entity")
    real_db = ent._db
    open_now = [0]
    seen: list[int] = []

    @contextlib.contextmanager
    def counting_db(*a, **k):
        open_now[0] += 1
        try:
            with real_db(*a, **k) as conn:
                yield conn
        finally:
            open_now[0] -= 1

    async def embed(name):
        seen.append(open_now[0])
        await asyncio.sleep(0)
        return _vec(name)

    monkeypatch.setattr(ent, "_db", counting_db)
    monkeypatch.setattr(ent, "_embed_canonical_cached", embed)

    async def extractor(_text):
        return {"entities": [
            {"canonical_name": "Alpha Industries", "entity_type": "organization"},
            {"canonical_name": "Beta Logistics", "entity_type": "organization"},
        ], "relationships": []}

    asyncio.run(ent._run_entity_extractor("m1", "x", extractor))
    assert seen, "precondition: resolution never reached the embedding tier"
    assert all(n == 0 for n in seen), f"connections open during embedding awaits: {seen}"
