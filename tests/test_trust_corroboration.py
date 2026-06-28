"""Phase 2 tests — trust-weighted & consensus provenance.

Covers the agents.trust_score column, the append-only memory_corroborations
ledger, corroboration-on-write (flag-gated), the contradiction ledger event, and
ABSENCE-TOLERANCE on a DB without migration 036. The pure aggregation math is in
test_confidence_math.py; here we test the DB wiring and the write-path behavior.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory import confidence as C  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")


def _full_db(db_path):
    from conftest import create_full_main_schema
    create_full_main_schema(db_path)


async def _write(db_path, monkeypatch, **kw):
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    from memory.write import memory_write_impl
    res = await memory_write_impl(embed=False, **kw)
    if isinstance(res, str):
        return res.split(":", 1)[1].strip() if ":" in res else res.strip()
    return res.get("id")


# ── trust get/set ────────────────────────────────────────────────────────────

def test_get_agent_trust_neutral_when_absent(tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    from memory import trust
    with sqlite3.connect(str(db)) as conn:
        assert trust.get_agent_trust(conn, "nobody") == C.TRUST_NEUTRAL


def test_set_and_get_agent_trust_clamped(tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    from memory import trust
    with sqlite3.connect(str(db)) as conn:
        # Above the ceiling clamps to 1.0; below the floor clamps to 0.5.
        assert trust.set_agent_trust(conn, "a1", 2.0) == C.TRUST_MAX
        assert trust.set_agent_trust(conn, "a2", 0.1) == C.TRUST_MIN
        conn.commit()
        assert trust.get_agent_trust(conn, "a1") == C.TRUST_MAX
        assert trust.get_agent_trust(conn, "a2") == C.TRUST_MIN


# ── corroboration ledger ─────────────────────────────────────────────────────

def test_record_corroboration_is_idempotent_per_source(tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    from memory import trust
    with sqlite3.connect(str(db)) as conn:
        first = trust.record_corroboration(conn, "m1", source_kind="agent",
                                           source_ref="claude", trust_at_write=1.0, delta=1.0)
        dup = trust.record_corroboration(conn, "m1", source_kind="agent",
                                         source_ref="claude", trust_at_write=1.0, delta=1.0)
        conn.commit()
        assert first is True and dup is False  # second is a dedup no-op
        ts, contradictions = trust.corroboration_inputs(conn, "m1")
        assert ts == pytest.approx(1.0) and contradictions == 0


def test_distinct_sources_sum_trust(tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    from memory import trust
    with sqlite3.connect(str(db)) as conn:
        trust.record_corroboration(conn, "m1", source_kind="agent", source_ref="claude",
                                   trust_at_write=1.0, delta=1.0)
        trust.record_corroboration(conn, "m1", source_kind="agent", source_ref="gemini",
                                   trust_at_write=0.8, delta=0.8)
        conn.commit()
        ts, _ = trust.corroboration_inputs(conn, "m1")
        assert ts == pytest.approx(1.8)


# ── corroboration on write ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_corroboration_off_by_default_creates_two_rows(monkeypatch, tmp_path):
    """Flag OFF (default): identical re-write still makes a 2nd row (today's
    behavior, unchanged)."""
    db = tmp_path / "t.db"
    _full_db(db)
    monkeypatch.delenv("M3_CORROBORATION", raising=False)
    await _write(db, monkeypatch, type="fact", content="the sky is blue", agent_id="claude")
    await _write(db, monkeypatch, type="fact", content="the sky is blue", agent_id="gemini")
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM memory_items WHERE content='the sky is blue'").fetchone()[0]
    assert n == 2


@pytest.mark.asyncio
async def test_corroboration_on_strengthens_existing(monkeypatch, tmp_path):
    """Flag ON: a near-identical write corroborates the existing memory — records
    a ledger event, bumps corroboration_count, and raises confidence.

    Drives _check_contradictions directly with identical embeddings (cosine=1.0)
    so the corroboration branch fires deterministically without a live embedder —
    the same direct-call pattern test_auto_related_link_scope uses.
    """
    from contextlib import contextmanager

    import memory.config as cfg

    db = tmp_path / "t.db"
    _full_db(db)
    monkeypatch.setattr(cfg, "CORROBORATION", True)
    monkeypatch.setattr(cfg, "CORROBORATION_THRESHOLD", 0.0)

    from embedding_utils import pack as _pack_vec
    vec = [1.0, 0.0]
    existing_id = "11111111-1111-1111-1111-111111111111"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
            "created_at, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (existing_id, "fact", "t", "local-first wins", "agent", "claude",
             "2026-06-01T00:00:00Z", C.AGENT_PRIOR),
        )
        conn.execute(
            "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) VALUES (?,?,?,?)",
            (existing_id, _pack_vec(vec), "test", len(vec)),
        )
        conn.commit()

    @contextmanager
    def fake_db(*a, **k):
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # _check_contradictions calls _db() inside write.py's module namespace.
    import memory.write as wmod
    monkeypatch.setattr(wmod, "_db", fake_db)

    base_conf = _col(db, existing_id, "confidence")
    # A new write (different agent) with the SAME content + vector.
    await wmod._check_contradictions(
        item_id="22222222-2222-2222-2222-222222222222",
        content="local-first wins", title="t", vec=vec, type_="fact",
        agent_id="gemini",
    )

    with sqlite3.connect(str(db)) as conn:
        led = conn.execute(
            "SELECT COUNT(*) FROM memory_corroborations WHERE memory_id=? AND delta>0",
            (existing_id,),
        ).fetchone()[0]
        corr_count = conn.execute(
            "SELECT corroboration_count FROM memory_items WHERE id=?", (existing_id,)
        ).fetchone()[0]
        new_conf = conn.execute(
            "SELECT confidence FROM memory_items WHERE id=?", (existing_id,)
        ).fetchone()[0]
    assert led >= 1, "a corroboration ledger row should exist"
    assert corr_count >= 1, "corroboration_count should be bumped"
    assert new_conf >= base_conf, "confidence should not drop after corroboration"


@pytest.mark.asyncio
async def test_corroboration_absence_tolerant_pre_036(monkeypatch, tmp_path):
    """With the ledger table dropped (pre-036), corroboration silently no-ops and
    the write still succeeds."""
    import memory.config as cfg
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DROP TABLE IF EXISTS memory_corroborations")
        conn.commit()
    monkeypatch.setattr(cfg, "CORROBORATION", True)
    monkeypatch.setattr(cfg, "CORROBORATION_THRESHOLD", 0.0)
    await _write(db, monkeypatch, type="fact", content="x", agent_id="claude")
    second = await _write(db, monkeypatch, type="fact", content="x", agent_id="gemini")
    with sqlite3.connect(str(db)) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM memory_items WHERE id=?", (second,)).fetchone()[0]
    assert cnt == 1  # write succeeded despite missing ledger


def _col(db_path, item_id, col):
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(f"SELECT {col} FROM memory_items WHERE id=?", (item_id,)).fetchone()
    return row[0] if row else None


# ── agent_set_trust MCP tool ─────────────────────────────────────────────────

def test_agent_set_trust_tool_clamps_and_surfaces(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import memory_core
    db = tmp_path / "t.db"
    _full_db(db)

    @contextmanager
    def fake_db(*a, **k):
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(memory_core, "_db", fake_db)
    assert "0.80" in memory_core.agent_set_trust_impl("claude", 0.8)
    assert "1.00" in memory_core.agent_set_trust_impl("claude", 9.0)   # clamped high
    assert "0.50" in memory_core.agent_set_trust_impl("claude", -1.0)  # clamped low
    assert "Trust: 0.5" in memory_core.agent_get_impl("claude")
    assert "required" in memory_core.agent_set_trust_impl("", 0.9).lower()


def test_agent_set_trust_registered_in_catalog():
    import mcp_tool_catalog
    names = {t.name for t in mcp_tool_catalog.TOOLS}
    assert "agent_set_trust" in names
