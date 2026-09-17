"""Parity gate: Rust-backed _graph_neighbor_ids (m3_core_rs.GraphIndex) vs the
pure-Python frontier-BFS fallback.

Task 2 (Fast-BFS Knowledge Graph Neighbor Traversals) wires m3_core_rs.GraphIndex
into bin/memory/graph.py:_graph_neighbor_ids. memory_relationships edges are
undirected for traversal (the SQL UNION follows both directions), so the Rust
path adds each edge in both directions and must return the IDENTICAL reachable
set as the Python path for every graph shape and depth.

If Rust and Python diverge on any input, that's a finding to surface, not hide.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory import graph as graph_mod  # noqa: E402


def _rust_ext():
    """The m3_core_rs extension, or None when it genuinely is not installed.

    ⚠ `graph_mod.config.m3_core_rs` is NOT a reliable "is it installed" probe in
    the test session. conftest.py sets M3_CORE_RS_DISABLE=1 by DEFAULT (to keep
    unit tests off a CUDA/native wheel and out of warm global embed state), so
    memory.config leaves that attribute None even on a box where the extension
    imports perfectly. Reading it alone made all 38 parity cases skip with
    "m3_core_rs not installed" on a host running m3_core_rs 3.9.7 -- a message
    that was simply false, and a gate that reported green while comparing
    nothing.

    That session default is deliberate and stays. These tests are safe to opt
    out of it: they are pure graph traversal (no embed, no CUDA, no model load,
    38 cases in 0.33s) and they monkeypatch `config.m3_core_rs` for BOTH paths
    themselves, so nothing leaks to other tests.

    Import the module directly, which answers the question actually being asked
    -- "can we compare against Rust?" -- independent of the session flag.
    """
    try:
        import m3_core_rs  # noqa: PLC0415 — probe, not a module-level dependency
    except ImportError:
        return None
    return m3_core_rs


def _make_db(edges):
    """An in-memory sqlite conn with a minimal memory_relationships table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE memory_relationships ("
        "  from_id TEXT NOT NULL, to_id TEXT NOT NULL, "
        "  PRIMARY KEY (from_id, to_id))"
    )
    conn.executemany(
        "INSERT OR IGNORE INTO memory_relationships(from_id, to_id) VALUES (?, ?)",
        edges,
    )
    conn.commit()
    return conn


@contextlib.contextmanager
def _db_yielding(conn):
    yield conn


def _patch_db(monkeypatch, conn):
    monkeypatch.setattr(graph_mod, "_db", lambda: _db_yielding(conn))


# Graph shapes: (edges, seeds). Directed edges in the table; traversal undirected.
SHAPES = [
    # simple chain 1-2-3-4 + branch 1-5
    ([("1", "2"), ("2", "3"), ("3", "4"), ("1", "5")], ["1"]),
    ([("1", "2"), ("2", "3"), ("3", "4"), ("1", "5")], ["3"]),
    ([("1", "2"), ("2", "3"), ("3", "4"), ("1", "5")], ["1", "4"]),
    # cycle
    ([("a", "b"), ("b", "c"), ("c", "a")], ["a"]),
    # disconnected components
    ([("x", "y"), ("p", "q"), ("q", "r")], ["x", "p"]),
    # reverse-only edge (seed is the to_id) — exercises undirected UNION
    ([("u", "v"), ("v", "w")], ["w"]),
    # star
    ([("c", "1"), ("c", "2"), ("c", "3"), ("c", "4")], ["1"]),
    # isolated seed (no edges touch it)
    ([("m", "n")], ["zzz"]),
    # self-loop
    ([("s", "s"), ("s", "t")], ["s"]),
]


@pytest.mark.parametrize("edges,seeds", SHAPES)
@pytest.mark.parametrize("depth", [1, 2, 3, 5])
def test_rust_python_parity(monkeypatch, edges, seeds, depth):
    rs = _rust_ext()
    if rs is None:
        pytest.skip("m3_core_rs not installed — nothing to compare against")

    # Python reference: force the fallback path.
    conn1 = _make_db(edges)
    _patch_db(monkeypatch, conn1)
    monkeypatch.setattr(graph_mod.config, "m3_core_rs", None)
    py_result = graph_mod._graph_neighbor_ids(list(seeds), depth)

    # Rust path: restore the extension.
    conn2 = _make_db(edges)
    _patch_db(monkeypatch, conn2)
    monkeypatch.setattr(graph_mod.config, "m3_core_rs", rs)
    rs_result = graph_mod._graph_neighbor_ids(list(seeds), depth)

    assert rs_result == py_result, (
        f"divergence: edges={edges} seeds={seeds} depth={depth}\n"
        f"  rust={sorted(rs_result)}\n  py  ={sorted(py_result)}"
    )


def test_seeds_excluded_and_depth_clamped(monkeypatch):
    rs = _rust_ext()
    if rs is None:
        pytest.skip("m3_core_rs not installed")
    edges = [("1", "2"), ("2", "3"), ("3", "4"), ("4", "5"), ("5", "6")]
    conn = _make_db(edges)
    _patch_db(monkeypatch, conn)
    monkeypatch.setattr(graph_mod.config, "m3_core_rs", rs)
    # depth=5 clamps to 3 → from "1" reach 2,3,4 but NOT 5,6. Seed "1" excluded.
    result = graph_mod._graph_neighbor_ids(["1"], 5)
    assert result == {"2", "3", "4"}


def test_zero_depth_and_empty_seeds(monkeypatch):
    conn = _make_db([("1", "2")])
    _patch_db(monkeypatch, conn)
    assert graph_mod._graph_neighbor_ids(["1"], 0) == set()
    assert graph_mod._graph_neighbor_ids([], 2) == set()
