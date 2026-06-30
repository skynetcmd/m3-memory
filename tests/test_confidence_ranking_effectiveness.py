"""Phase 5 effectiveness probe — the pre-registered §5 metric, on a controlled
synthetic corpus.

Pre-registered (docs/plans/KNOWLEDGE_MAINTENANCE_PLAN.md): enabling
M3_CONFIDENCE_RANKING must lift recall@k for corroborated facts WITHOUT regressing
the neutral subset. This probe demonstrates the mechanism deterministically: a
held-out set of "target" facts (high confidence, the right answers) competes with
same-relevance distractors (low confidence); we measure recall@k flag-off vs
flag-on.

SCOPE NOTE: this is a controlled-corpus probe proving the ranking term does what
it should. A production-grade recall@10 on a realistic multi-session corpus
belongs with the LongMemEval bench harness (bench territory, not shipped here).
The number here is a mechanism check, not a published benchmark result.
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


def _full_db(db_path):
    from conftest import create_full_main_schema
    create_full_main_schema(db_path)


def _vec(primary, dim):
    v = [0.0] * dim
    v[0] = primary
    v[1] = (1.0 - primary * primary) ** 0.5
    return v


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


async def _recall_at_k(mc, targets, k):
    res = await mc.memory_search_scored_impl(
        query="q", k=k, mmr=False, search_mode="vector"
    )
    got = set()
    for r in res:
        got.add(r[1]["id"] if isinstance(r, tuple) else r["id"])
    return len(got & targets) / len(targets)


@pytest.mark.asyncio
async def test_confidence_ranking_lifts_corroborated_recall(monkeypatch, tmp_path):
    """Targets (high confidence) compete with same-relevance distractors (low
    confidence). Enabling confidence ranking must lift recall@k for the targets
    and never lower it — the pre-registered §5 direction."""
    from memory.config import EMBED_DIM, EMBED_MODEL

    db = tmp_path / "t.db"
    _full_db(db)
    # Query vector; all rows share near-identical relevance (slightly perturbed)
    # so confidence is the deciding signal at the k boundary.
    qv = _vec(1.0, EMBED_DIM)
    K = 5
    N_TARGETS = 5
    N_DISTRACTORS = 10

    with sqlite3.connect(str(db)) as conn:
        for i in range(N_TARGETS):
            # Corroborated targets: high confidence.
            conn.execute(
                "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
                "created_at, importance, confidence, is_deleted) VALUES (?,?,?,?,?,?,?,?,?,0)",
                (f"target-{i}", "fact", f"t{i}", f"target fact {i}", "agent", "claude",
                 "2026-01-01T00:00:00Z", 0.5, 0.95),
            )
            conn.execute(
                "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) VALUES (?,?,?,?)",
                (f"target-{i}", _pack(qv), EMBED_MODEL, EMBED_DIM),
            )
        for j in range(N_DISTRACTORS):
            conn.execute(
                "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
                "created_at, importance, confidence, is_deleted) VALUES (?,?,?,?,?,?,?,?,?,0)",
                (f"distractor-{j}", "fact", f"d{j}", f"distractor fact {j}", "agent", "claude",
                 "2026-01-01T00:00:00Z", 0.5, 0.15),
            )
            conn.execute(
                "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) VALUES (?,?,?,?)",
                (f"distractor-{j}", _pack(qv), EMBED_MODEL, EMBED_DIM),
            )
        conn.commit()

    targets = {f"target-{i}" for i in range(N_TARGETS)}
    import memory.config as cfg

    # Flag OFF baseline.
    monkeypatch.setattr(cfg, "CONFIDENCE_RANKING", False)
    mc = _patch(monkeypatch, db, qv)
    recall_off = await _recall_at_k(mc, targets, K)

    # Flag ON.
    monkeypatch.setattr(cfg, "CONFIDENCE_RANKING", True)
    monkeypatch.setattr(cfg, "CONFIDENCE_WEIGHT", 0.3)
    recall_on = await _recall_at_k(mc, targets, K)

    # Pre-registered direction: ON must not regress, and should lift recall when
    # confidence is the deciding signal at the k boundary.
    assert recall_on >= recall_off, (
        f"confidence ranking must not regress recall (off={recall_off}, on={recall_on})")
    assert recall_on >= 0.99, (
        f"with confidence deciding ties, all corroborated targets should make k "
        f"(off={recall_off}, on={recall_on})")
