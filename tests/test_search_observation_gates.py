"""Regression tests for the observation-preference + two-stage expansion helpers
in memory.search — restored after commit d78fc1d extracted the call sites but
dropped the implementations (they raised NameError when the gates fired).

Deterministic: the helpers are pure list transforms over synthetic ranked data
(no embedder; two-stage's DB lookup is exercised against a tmp memory_items DB).
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys

import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import memory.search as s  # noqa: E402


def _obs(i, content, **md):
    return {"id": f"obs{i}", "type": "observation", "content": content,
            "metadata": md}


def _raw(i, content="x"):
    return {"id": f"raw{i}", "type": "message", "content": content}


# ── _apply_observation_preference ────────────────────────────────────────────
def test_obs_preference_interleaves_when_under_budget(monkeypatch):
    monkeypatch.setenv("M3_OBSERVATION_BUDGET_TOKENS", "100000")  # never reached
    ranked = [(0.9, _obs(1, "short")), (0.8, _raw(1)), (0.7, _raw(2)), (0.6, _raw(3))]
    out = s._apply_observation_preference(ranked, k=3)
    # obs first, then raw to fill k=3 slots
    assert out[0][1]["type"] == "observation"
    assert len(out) == 3
    assert [it["id"] for _, it in out] == ["obs1", "raw1", "raw2"]


def test_obs_preference_returns_obs_only_when_over_budget(monkeypatch):
    monkeypatch.setenv("M3_OBSERVATION_BUDGET_TOKENS", "1")  # trivially exceeded
    big = "z" * 400  # ~100 tokens by the 1-per-4-chars estimate
    ranked = [(0.9, _obs(1, big)), (0.8, _raw(1)), (0.7, _raw(2))]
    out = s._apply_observation_preference(ranked, k=5)
    # observations supply enough -> obs-only
    assert all(it["type"] == "observation" for _, it in out)
    assert [it["id"] for _, it in out] == ["obs1"]


def test_obs_preference_noop_without_observations(monkeypatch):
    monkeypatch.setenv("M3_OBSERVATION_BUDGET_TOKENS", "4000")
    ranked = [(0.9, _raw(1)), (0.8, _raw(2))]
    out = s._apply_observation_preference(ranked, k=5)
    assert out == ranked  # unchanged when no obs hits


# ── _apply_two_stage_expansion ───────────────────────────────────────────────
@pytest.fixture
def tmp_memory_db(tmp_path, monkeypatch):
    db = tmp_path / "agent_memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memory_items (id TEXT PRIMARY KEY, content TEXT, title TEXT, "
        "type TEXT, importance REAL, is_deleted INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO memory_items(id, content, title, type, importance) VALUES (?,?,?,?,?)",
        [("turnA", "verbatim turn A", "A", "message", 0.5),
         ("turnB", "verbatim turn B", "B", "message", 0.5)],
    )
    conn.commit(); conn.close()
    monkeypatch.setenv("M3_DATABASE", str(db))
    return db


def test_two_stage_expands_observation_source_turns(tmp_memory_db, monkeypatch):
    monkeypatch.setenv("M3_TWO_STAGE_TURN_PENALTY", "0.7")
    # one observation whose metadata points at two source turns
    ranked = [(1.0, _obs(1, "summary", source_turn_ids=["turnA", "turnB"]))]
    out = asyncio.run(s._apply_two_stage_expansion(ranked, k=10))
    ids = [it["id"] for _, it in out]
    assert "turnA" in ids and "turnB" in ids        # expanded turns appended
    assert ids[0] == "obs1"                          # observation still ranks top
    exp = [it for _, it in out if it["id"] == "turnA"][0]
    assert exp["_two_stage_expanded"] is True
    # expanded turns sit below the observation (penalty applied)
    obs_score = next(sc for sc, it in out if it["id"] == "obs1")
    turn_score = next(sc for sc, it in out if it["id"] == "turnA")
    assert turn_score < obs_score


def test_two_stage_noop_when_no_source_turns(tmp_memory_db):
    ranked = [(1.0, _obs(1, "summary"))]  # no source_turn_ids in metadata
    out = asyncio.run(s._apply_two_stage_expansion(ranked, k=10))
    assert out == ranked


def test_two_stage_skips_already_present_turn(tmp_memory_db):
    # if a source turn is already in the ranked list, don't duplicate it
    ranked = [(1.0, _obs(1, "s", source_turn_ids=["turnA"])),
              (0.9, {"id": "turnA", "type": "message", "content": "already here"})]
    out = asyncio.run(s._apply_two_stage_expansion(ranked, k=10))
    assert sum(1 for _, it in out if it["id"] == "turnA") == 1
