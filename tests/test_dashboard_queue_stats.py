"""Tests for the dashboard queue/throughput stats (pure data layer, no FastAPI).

Throughput is timestamp-derived: a queue row is deleted when processed, so rate
is measured from rows PRODUCED per window on the output tables (memory_embeddings
/ memory_items). These tests build a tiny in-memory-shaped SQLite DB and assert
the counts, rates, and drain-ETA math.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import pytest  # noqa: E402
from dashboard import queue_stats as qs  # noqa: E402


def test_identifier_allowlist_blocks_injection():
    """The COUNT queries f-string-interpolate table/column identifiers (SQLite
    can't bind them), so they're allowlisted. An identifier not in _PIPELINES —
    including a SQL-injection attempt — must raise, never reach the query."""
    # legit identifiers pass through unchanged
    assert qs._safe_ident("memory_items", qs._ALLOWED_TABLES, "table") == "memory_items"
    assert qs._safe_ident("created_at", qs._ALLOWED_TS_COLS, "col") == "created_at"
    # injection / unknown identifiers are rejected
    for bad in ("memory_items; DROP TABLE x", "x) UNION SELECT ...", "unknown_table", ""):
        with pytest.raises(ValueError):
            qs._safe_ident(bad, qs._ALLOWED_TABLES, "table")


def _make_db(tmp_path) -> str:
    db = str(tmp_path / "stats.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE observation_queue (id INTEGER PRIMARY KEY, enqueued_at TEXT);
        CREATE TABLE reflector_queue (id INTEGER PRIMARY KEY, enqueued_at TEXT);
        CREATE TABLE memory_embeddings (id TEXT PRIMARY KEY, memory_id TEXT,
            created_at TEXT);
        CREATE TABLE memory_items (id TEXT PRIMARY KEY, type TEXT, created_at TEXT);
        """
    )
    conn.commit()
    conn.close()
    return db


def _seed(db, table, cols, rows):
    conn = sqlite3.connect(db)
    ph = ",".join("?" * len(cols))
    conn.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})", rows)
    conn.commit()
    conn.close()


def test_enrichment_filters_by_type(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO memory_items (id,type,created_at) VALUES ('a','fact_enriched',datetime('now'))")
    conn.execute("INSERT INTO memory_items (id,type,created_at) VALUES ('b','note',datetime('now'))")
    conn.commit(); conn.close()
    stats = qs.collect_pipeline_stats(db)
    enrich = next(p for p in stats["pipelines"] if p["key"] == "enrich")
    # only the fact_enriched row counts toward enrichment throughput
    assert enrich["rates"][1] == 1.0


def test_eta_math_and_edge_cases():
    # empty queue -> 0 (drained)
    assert qs._eta_seconds(0, 5.0) == 0.0
    # nonzero queue, zero rate -> None (can't estimate / stalled)
    assert qs._eta_seconds(10, 0.0) is None
    # 60 items at 30/min -> 120s
    assert qs._eta_seconds(60, 30.0) == 120.0


def test_human_eta_formatting():
    assert qs._human_eta(0.0) == "drained"
    assert qs._human_eta(None) == "stalled (no recent throughput)"
    assert qs._human_eta(45).endswith("s")
    assert qs._human_eta(120).endswith("m")
    assert qs._human_eta(7200).endswith("h")


def test_missing_tables_degrade_to_zero(tmp_path):
    # DB with none of the expected tables must not raise.
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    stats = qs.collect_pipeline_stats(db)
    # One entry per _PIPELINES pipeline (enrich, reflect, entities).
    assert len(stats["pipelines"]) == len(qs._PIPELINES)
    assert all(p["queue_len"] == 0 for p in stats["pipelines"])
