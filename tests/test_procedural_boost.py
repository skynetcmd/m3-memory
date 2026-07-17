"""Procedural retrieval boost — contract tests.

Zero-regression contract (DESIGN_PHILOSOPHIES §5/§11): with the intent NOT
'procedural' (or M3_INTENT_ROUTING off), ranking is byte-identical — a `procedure`
row is treated like any other type. With intent='procedural' and routing on, a
`procedure` row gets an additive boost, so it outranks an equally-relevant `note`.

Drives memory_search_scored_impl against a real full-schema DB with seeded rows +
embeddings (query embedder stubbed for determinism). The SLM classifier gate is
left OFF, so intent is only ever what the caller passes — this stays hermetic
(no model call) and the auto-classification hook is a no-op.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from embedding_utils import pack as _pack  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    # Keep the SLM classifier gate off so auto-classification never fires and the
    # test is fully hermetic (intent comes only from the caller).
    monkeypatch.delenv("M3_SLM_CLASSIFIER", raising=False)


def _full_db(db_path):
    from conftest import create_full_main_schema
    create_full_main_schema(db_path)


def _vec(primary: float, dim: int):
    v = [0.0] * dim
    v[0] = primary
    v[1] = (1.0 - primary * primary) ** 0.5
    return v


def _seed(conn, mid, content, vec, typ, *, importance=0.5):
    from memory.config import EMBED_MODEL
    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
        "created_at, importance, is_deleted) VALUES (?,?,?,?,?,?,?,?,0)",
        (mid, typ, content[:20], content, "agent", "claude",
         "2026-01-01T00:00:00Z", importance),
    )
    conn.execute(
        "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) VALUES (?,?,?,?)",
        (mid, _pack(vec), EMBED_MODEL, len(vec)),
    )


def _patch(monkeypatch, db_path, qvec):
    import memory_core

    @contextmanager
    def fake_db(existing=None, *a, **k):
        if existing is not None:
            yield existing
            return
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    async def fake_embed(_q):
        return (qvec, "test-embed")

    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_core, "_embed", fake_embed)
    return memory_core


async def _scores(mc, **kw):
    res = await mc.memory_search_scored_impl(
        query="how do I deploy", k=5, mmr=False, search_mode="vector", **kw
    )
    out = {}
    for r in res:
        if isinstance(r, tuple):
            out[r[1]["id"]] = r[0]
        elif isinstance(r, dict):
            out[r["id"]] = None
    return out


@pytest.mark.asyncio
async def test_no_procedural_intent_is_byte_identical(monkeypatch, tmp_path):
    """The byte-identical contract: a `procedure` row scores THE SAME whether the
    intent is unset or a non-procedural intent — the boost only fires on the
    'procedural' intent. (Compares the exact same row across two runs, so any
    length/title component cancels.)"""
    from memory.config import EMBED_DIM
    db = tmp_path / "t.db"
    _full_db(db)
    qv = _vec(1.0, EMBED_DIM)
    with sqlite3.connect(str(db)) as conn:
        _seed(conn, "proc", "deploy the service via the runbook", qv, "procedure")
        conn.commit()

    import memory.config as cfg
    monkeypatch.setattr(cfg, "INTENT_ROUTING", True)
    import memory.search as srch
    monkeypatch.setattr(srch, "INTENT_PROCEDURAL_BOOST", 0.5)
    mc = _patch(monkeypatch, db, qv)

    async def _proc_score(**kw):
        res = await mc.memory_search_scored_impl(
            query="how do I deploy", k=5, mmr=False, search_mode="vector", **kw)
        for r in res:
            item = r[1] if isinstance(r, tuple) else r
            if item["id"] == "proc":
                return r[0] if isinstance(r, tuple) else None
        return None

    base = await _proc_score()                         # no intent
    other = await _proc_score(intent_hint="user-fact")  # non-procedural intent
    assert base is not None and other is not None
    assert base == pytest.approx(other), (
        "a non-procedural intent must not change a procedure row's score")


@pytest.mark.asyncio
async def test_procedural_intent_boosts_procedure_row(monkeypatch, tmp_path):
    """With intent='procedural' + routing on, the `procedure` row outranks an
    equally-relevant `note`."""
    from memory.config import EMBED_DIM
    db = tmp_path / "t.db"
    _full_db(db)
    qv = _vec(1.0, EMBED_DIM)
    with sqlite3.connect(str(db)) as conn:
        _seed(conn, "proc", "deploy the service via the runbook", qv, "procedure")
        _seed(conn, "note", "a loose note about deploying", qv, "note")
        conn.commit()

    import memory.config as cfg
    monkeypatch.setattr(cfg, "INTENT_ROUTING", True)
    monkeypatch.setattr(cfg, "INTENT_PROCEDURAL_BOOST", 0.5)  # exaggerate for a clear gap
    # search.py binds INTENT_PROCEDURAL_BOOST at import; patch the bound name too.
    import memory.search as srch
    monkeypatch.setattr(srch, "INTENT_PROCEDURAL_BOOST", 0.5)
    mc = _patch(monkeypatch, db, qv)

    res = await mc.memory_search_scored_impl(
        query="how do I deploy", k=5, mmr=False, search_mode="vector",
        intent_hint="procedural",
    )
    ids = [(r[1]["id"] if isinstance(r, tuple) else r["id"]) for r in res]
    assert ids and ids[0] == "proc", (
        f"procedural intent should rank the procedure first; got {ids}")


@pytest.mark.asyncio
async def test_procedural_intent_ignores_non_procedure_types(monkeypatch, tmp_path):
    """The boost keys on type=='procedure' only — a `note` never gets it even
    under a procedural intent."""
    from memory.config import EMBED_DIM
    db = tmp_path / "t.db"
    _full_db(db)
    qv = _vec(1.0, EMBED_DIM)
    with sqlite3.connect(str(db)) as conn:
        _seed(conn, "note_hi", "an important deploy note", qv, "note", importance=0.9)
        _seed(conn, "note_lo", "a minor deploy note", qv, "note", importance=0.1)
        conn.commit()

    import memory.config as cfg
    monkeypatch.setattr(cfg, "INTENT_ROUTING", True)
    import memory.search as srch
    monkeypatch.setattr(srch, "INTENT_PROCEDURAL_BOOST", 0.5)
    mc = _patch(monkeypatch, db, qv)

    res = await mc.memory_search_scored_impl(
        query="how do I deploy", k=5, mmr=False, search_mode="vector",
        intent_hint="procedural",
    )
    ids = [(r[1]["id"] if isinstance(r, tuple) else r["id"]) for r in res]
    # Neither is a procedure -> order driven by importance, boost never applies.
    assert ids[0] == "note_hi", f"non-procedure rows must not get the boost; got {ids}"
