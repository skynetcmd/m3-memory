"""Phase 3 tests — reinforcement (confidence as a living signal).

Two layers:
  * Pure math (decay_toward_neutral, access_reinforcement): convergence to the
    NEUTRAL fixed point, monotonicity, bounds, corroborated-floor respect, and
    the hard cap on access reinforcement.
  * The maintenance pass (_reinforce_confidence): re-aggregates ledger-active
    memories, decays the un-reinforced toward neutral in one set-based UPDATE,
    skips recently-accessed rows, and is absence-tolerant on a pre-035/036 DB.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory import confidence as C  # noqa: E402

# ── pure decay math ──────────────────────────────────────────────────────────

def test_neutral_is_a_fixed_point():
    assert C.decay_toward_neutral(C.NEUTRAL) == C.NEUTRAL


def test_decay_moves_toward_neutral_from_both_sides():
    assert C.decay_toward_neutral(0.9) < 0.9          # above neutral -> down
    assert C.decay_toward_neutral(0.9) > C.NEUTRAL     # but not past it
    assert C.decay_toward_neutral(0.1) > 0.1          # below neutral -> up
    assert C.decay_toward_neutral(0.1) < C.NEUTRAL     # but not past it


def test_decay_converges_to_neutral_no_oscillation():
    """Iterating decay must monotonically approach NEUTRAL and never overshoot
    or oscillate — the anti-runaway property."""
    c = 1.0
    prev = c
    for _ in range(500):
        c = C.decay_toward_neutral(c)
        assert C.NEUTRAL <= c <= prev + 1e-12  # monotone non-increasing, bounded below
        prev = c
    assert abs(c - C.NEUTRAL) < 1e-3  # converged


def test_decay_respects_corroborated_floor():
    """An above-neutral fact with a corroborated floor never decays below it."""
    floor = 0.8
    c = 0.85
    for _ in range(1000):
        c = C.decay_toward_neutral(c, floor=floor)
        assert c >= min(0.85, floor) - 1e-9
    assert c >= floor - 1e-9  # settles at the floor, not at NEUTRAL


def test_decay_stays_in_unit_interval():
    for x in (-5.0, 0.0, 0.5, 1.0, 5.0):
        assert 0.0 <= C.decay_toward_neutral(x) <= 1.0


# ── pure access reinforcement ────────────────────────────────────────────────

def test_access_reinforcement_zero_and_capped():
    assert C.access_reinforcement(0) == 0.0
    assert C.access_reinforcement(-3) == 0.0
    # Monotonic increasing but hard-capped.
    assert C.access_reinforcement(1) < C.access_reinforcement(10)
    assert C.access_reinforcement(10_000_000) == C.ACCESS_REINFORCE_CAP


def test_access_reinforcement_cannot_rival_corroboration():
    """The whole point: being read a lot is weaker evidence than being
    corroborated. Access cap < a single corroboration unit's bonus reach."""
    assert C.ACCESS_REINFORCE_CAP < C.CORROBORATION_CAP


# ── maintenance pass integration ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")


def _full_db(db_path):
    from conftest import create_full_main_schema
    create_full_main_schema(db_path)


def _seed(conn, mid, confidence, *, last_accessed=None, source="agent", change_agent="claude"):
    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
        "created_at, confidence, last_accessed_at, is_deleted) "
        "VALUES (?,?,?,?,?,?,?,?,?,0)",
        (mid, "fact", "t", "c", source, change_agent, "2026-01-01T00:00:00Z",
         confidence, last_accessed),
    )


def test_decays_unreinforced_toward_neutral(tmp_path):
    from memory_maintenance import _reinforce_confidence
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        _seed(conn, "high", 0.95)   # old, never accessed -> should decay down
        _seed(conn, "low", 0.10)    # -> should rise toward neutral
        conn.commit()
        reaggregated, decayed = _reinforce_confidence(conn)
        conn.commit()
        high = conn.execute("SELECT confidence FROM memory_items WHERE id='high'").fetchone()[0]
        low = conn.execute("SELECT confidence FROM memory_items WHERE id='low'").fetchone()[0]
    assert decayed == 2 and reaggregated == 0
    assert C.NEUTRAL < high < 0.95
    assert 0.10 < low < C.NEUTRAL


def test_recently_accessed_is_not_decayed(tmp_path):
    from memory_maintenance import _reinforce_confidence
    db = tmp_path / "t.db"
    _full_db(db)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        _seed(conn, "hot", 0.95, last_accessed=now)  # accessed now -> skip decay
        conn.commit()
        _reinforce_confidence(conn)
        conn.commit()
        hot = conn.execute("SELECT confidence FROM memory_items WHERE id='hot'").fetchone()[0]
    assert hot == pytest.approx(0.95)  # untouched


def test_ledger_active_is_reaggregated_not_decayed(tmp_path):
    from memory_maintenance import _reinforce_confidence

    from memory import trust
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        _seed(conn, "m", 0.70, source="agent", change_agent="claude")
        # Two distinct corroborating sources in the ledger.
        trust.record_corroboration(conn, "m", source_kind="agent", source_ref="gemini",
                                   trust_at_write=1.0, delta=1.0)
        trust.record_corroboration(conn, "m", source_kind="agent", source_ref="grok",
                                   trust_at_write=1.0, delta=1.0)
        conn.commit()
        reaggregated, decayed = _reinforce_confidence(conn)
        conn.commit()
        m = conn.execute("SELECT confidence, corroboration_count FROM memory_items WHERE id='m'").fetchone()
    assert reaggregated == 1
    # 'm' was re-aggregated (corroboration raised it), NOT in the decay set.
    assert m["confidence"] >= 0.70
    assert m["corroboration_count"] == 2


def test_reinforcement_absence_tolerant_pre_035(tmp_path):
    """Drop the confidence column → the pass is a no-op, doesn't raise."""
    from memory_maintenance import _reinforce_confidence
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_memory_items_confidence")
        conn.execute("ALTER TABLE memory_items DROP COLUMN confidence")
        conn.commit()
        reaggregated, decayed = _reinforce_confidence(conn)
    assert (reaggregated, decayed) == (0, 0)
