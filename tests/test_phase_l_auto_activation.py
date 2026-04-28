"""Phase L: tests for auto-activation of retrieval gates by data presence.

Targets the _gate_active helper directly. The data-presence count query is
patched per-test, so tests don't bring up a SQLite schema — the helper's
contract is "given env var + count query, return bool".
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.fixture(autouse=True)
def _clear_gate_cache():
    import memory_core
    memory_core._GATE_CACHE.clear()
    yield
    memory_core._GATE_CACHE.clear()


def _patch_count(monkeypatch, count, calls=None):
    import memory_core

    def fake(q):
        if calls is not None:
            calls.append(q)
        return count
    monkeypatch.setattr(memory_core, "_gate_count_query", fake)


@pytest.mark.parametrize("env,count,disable,expected", [
    (None,  0,    None, False),  # empty DB, no env -> off
    ("1",   0,    None, True),   # back-compat: explicit env wins on empty
    (None,  100,  None, True),   # populated -> auto-activates
    (None,  99,   None, False),  # below threshold
    ("1",   500,  None, True),   # explicit + populated
    (None,  1000, "1",  False),  # escape hatch blocks auto
    ("1",   0,    "1",  True),   # escape hatch does NOT block explicit
])
def test_prefer_observations_matrix(monkeypatch, env, count, disable, expected):
    import memory_core
    if env is None:
        monkeypatch.delenv("M3_PREFER_OBSERVATIONS", raising=False)
    else:
        monkeypatch.setenv("M3_PREFER_OBSERVATIONS", env)
    if disable is None:
        monkeypatch.delenv("M3_DISABLE_AUTO_ACTIVATION", raising=False)
    else:
        monkeypatch.setenv("M3_DISABLE_AUTO_ACTIVATION", disable)
    _patch_count(monkeypatch, count)
    assert memory_core._prefer_observations_gate() is expected


def test_entity_graph_threshold_one(monkeypatch):
    import memory_core
    monkeypatch.delenv("M3_ENABLE_ENTITY_GRAPH", raising=False)
    monkeypatch.delenv("M3_DISABLE_AUTO_ACTIVATION", raising=False)
    _patch_count(monkeypatch, 1)
    assert memory_core._enable_entity_graph_gate() is True


def test_two_stage_paired_with_prefer(monkeypatch):
    import memory_core
    monkeypatch.delenv("M3_TWO_STAGE_OBSERVATIONS", raising=False)
    monkeypatch.delenv("M3_DISABLE_AUTO_ACTIVATION", raising=False)
    _patch_count(monkeypatch, 100)
    assert memory_core._two_stage_observations_gate() is True


def test_cache_avoids_repeat_queries(monkeypatch):
    import memory_core
    monkeypatch.delenv("M3_PREFER_OBSERVATIONS", raising=False)
    monkeypatch.delenv("M3_DISABLE_AUTO_ACTIVATION", raising=False)
    calls: list = []
    _patch_count(monkeypatch, 200, calls=calls)
    for _ in range(3):
        assert memory_core._prefer_observations_gate() is True
    assert len(calls) == 1


def test_cache_expires_after_ttl(monkeypatch):
    import memory_core
    monkeypatch.delenv("M3_PREFER_OBSERVATIONS", raising=False)
    monkeypatch.delenv("M3_DISABLE_AUTO_ACTIVATION", raising=False)
    calls: list = []
    _patch_count(monkeypatch, 200, calls=calls)
    monkeypatch.setattr(memory_core, "_GATE_CACHE_TTL", 0)
    memory_core._prefer_observations_gate()
    memory_core._prefer_observations_gate()
    assert len(calls) == 2
