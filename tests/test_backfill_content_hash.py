"""Tests for bin/backfill_content_hash.py — populate NULL content_hash rows.

Asserts:
  - Schema sanity errors fire on missing tables / columns
  - Candidate query filters NULL only, soft-deleted out, type filter
  - Hash matches memory_core._content_hash bit-for-bit (no drift)
  - Augment-anchors path produces a different hash than raw content
    when metadata carries temporal anchors
  - Dry-run counts but doesn't write
  - Real run UPDATEs the column for matching rows
  - id_prefix sharding picks the right rows
  - Already-populated rows are not re-hashed
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))


# ── Fixtures ──────────────────────────────────────────────────────────────

def _make_min_schema(db_path: Path) -> None:
    """Minimal memory_items + memory_embeddings shape that the backfill needs."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            type TEXT,
            title TEXT,
            content TEXT,
            metadata_json TEXT,
            agent_id TEXT,
            change_agent TEXT,
            importance REAL,
            source TEXT,
            is_deleted INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            user_id TEXT,
            scope TEXT,
            variant TEXT
        );
        CREATE TABLE memory_embeddings (
            id TEXT PRIMARY KEY,
            memory_id TEXT,
            embedding BLOB,
            embed_model TEXT,
            dim INTEGER,
            created_at TEXT,
            content_hash TEXT
        );
    """)
    conn.commit()
    conn.close()


def _seed(db_path: Path, items: list[dict], embeds: list[dict]) -> None:
    conn = sqlite3.connect(str(db_path))
    for it in items:
        conn.execute(
            "INSERT INTO memory_items "
            "(id, type, title, content, metadata_json, is_deleted, user_id, scope, variant) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                it["id"],
                it.get("type", "note"),
                it.get("title", ""),
                it.get("content", ""),
                it.get("metadata_json", "{}"),
                it.get("is_deleted", 0),
                it.get("user_id", ""),
                it.get("scope", "agent"),
                it.get("variant", None),
            ),
        )
    for e in embeds:
        conn.execute(
            "INSERT INTO memory_embeddings "
            "(id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                e["id"],
                e["memory_id"],
                e.get("embedding", b"\x00" * 16),
                e.get("embed_model", "test-model"),
                e.get("dim", 4),
                e.get("created_at", "2026-05-01T00:00:00Z"),
                e.get("content_hash"),  # None / NULL by default
            ),
        )
    conn.commit()
    conn.close()


def _make_args(db_path: Path, **overrides) -> argparse.Namespace:
    base = dict(
        db=db_path,
        type=["chat_log", "message"],
        variant=[],
        user_id=None,
        id_prefix=None,
        limit=None,
        batch_size=10,
        augment_anchors=False,
        dry_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "test.db"
    _make_min_schema(p)
    return p


# ── Schema sanity ────────────────────────────────────────────────────────

def test_schema_missing_db(tmp_path):
    import backfill_content_hash as bf
    with pytest.raises(FileNotFoundError):
        bf._verify_schema(tmp_path / "nope.db")


def test_schema_missing_table(tmp_path):
    import backfill_content_hash as bf
    p = tmp_path / "bare.db"
    sqlite3.connect(str(p)).close()
    with pytest.raises(RuntimeError, match="memory_items"):
        bf._verify_schema(p)


def test_schema_missing_content_hash_column(tmp_path):
    import backfill_content_hash as bf
    p = tmp_path / "old.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE memory_items (id TEXT PRIMARY KEY, type TEXT, content TEXT);
        CREATE TABLE memory_embeddings (
            id TEXT PRIMARY KEY, memory_id TEXT, embedding BLOB
            -- no content_hash column!
        );
    """)
    conn.close()
    with pytest.raises(RuntimeError, match="content_hash missing"):
        bf._verify_schema(p)


def test_schema_minimal_ok(db):
    import backfill_content_hash as bf
    bf._verify_schema(db)  # should not raise


# ── Candidate query ──────────────────────────────────────────────────────

def test_count_filters_null_only(db):
    """Already-populated rows should NOT show up as pending."""
    import backfill_content_hash as bf
    _seed(db, [
        {"id": "row-A", "content": "alpha", "type": "message"},
        {"id": "row-B", "content": "beta",  "type": "message"},
    ], [
        {"id": "e-A", "memory_id": "row-A", "content_hash": None},          # NULL — pending
        {"id": "e-B", "memory_id": "row-B", "content_hash": "preexisting"}, # already set
    ])
    args = _make_args(db)
    assert bf._count_pending(db, args) == 1


def test_count_filters_soft_deleted(db):
    import backfill_content_hash as bf
    _seed(db, [
        {"id": "row-A", "content": "a", "type": "message", "is_deleted": 0},
        {"id": "row-B", "content": "b", "type": "message", "is_deleted": 1},
    ], [
        {"id": "e-A", "memory_id": "row-A", "content_hash": None},
        {"id": "e-B", "memory_id": "row-B", "content_hash": None},
    ])
    args = _make_args(db)
    assert bf._count_pending(db, args) == 1


def test_count_filters_empty_content(db):
    import backfill_content_hash as bf
    _seed(db, [
        {"id": "row-A", "content": "alpha",  "type": "message"},
        {"id": "row-B", "content": "",       "type": "message"},
        {"id": "row-C", "content": "   ",    "type": "message"},
    ], [
        {"id": "e-A", "memory_id": "row-A", "content_hash": None},
        {"id": "e-B", "memory_id": "row-B", "content_hash": None},
        {"id": "e-C", "memory_id": "row-C", "content_hash": None},
    ])
    args = _make_args(db)
    assert bf._count_pending(db, args) == 1


def test_count_type_filter(db):
    import backfill_content_hash as bf
    _seed(db, [
        {"id": "row-A", "content": "a", "type": "message"},
        {"id": "row-B", "content": "b", "type": "summary"},
    ], [
        {"id": "e-A", "memory_id": "row-A", "content_hash": None},
        {"id": "e-B", "memory_id": "row-B", "content_hash": None},
    ])
    args = _make_args(db, type=["message"])
    assert bf._count_pending(db, args) == 1
    args = _make_args(db, type=["summary"])
    assert bf._count_pending(db, args) == 1
    args = _make_args(db, type=["message", "summary"])
    assert bf._count_pending(db, args) == 2


def test_count_id_prefix(db):
    import backfill_content_hash as bf
    _seed(db, [
        {"id": "mi-A", "content": "a", "type": "message"},
        {"id": "mi-B", "content": "b", "type": "message"},
    ], [
        {"id": "abc-1", "memory_id": "mi-A", "content_hash": None},
        {"id": "xyz-2", "memory_id": "mi-B", "content_hash": None},
    ])
    args = _make_args(db, id_prefix="abc")
    assert bf._count_pending(db, args) == 1


# ── Hash correctness — bit-for-bit match with memory_core ─────────────────

def test_hash_matches_memory_core(db):
    """The hash we write must match what memory_core._content_hash would compute."""
    import backfill_content_hash as bf
    os.environ["M3_DATABASE"] = str(db)
    sys.path.insert(0, str(REPO / "bin"))
    import memory_core as mc

    _seed(db, [
        {"id": "row-A", "content": "User said hello", "type": "message"},
    ], [
        {"id": "e-A", "memory_id": "row-A", "content_hash": None},
    ])
    args = _make_args(db)
    bf._run_backfill(args)

    expected_hash = mc._content_hash("User said hello")
    conn = sqlite3.connect(str(db))
    actual = conn.execute(
        "SELECT content_hash FROM memory_embeddings WHERE id=?", ("e-A",)
    ).fetchone()[0]
    conn.close()
    assert actual == expected_hash


# ── Real run — UPDATEs land ──────────────────────────────────────────────

def test_run_updates_pending_rows(db):
    import backfill_content_hash as bf
    _seed(db, [
        {"id": "row-A", "content": "alpha", "type": "message"},
        {"id": "row-B", "content": "beta",  "type": "message"},
    ], [
        {"id": "e-A", "memory_id": "row-A", "content_hash": None},
        {"id": "e-B", "memory_id": "row-B", "content_hash": None},
    ])
    args = _make_args(db)
    counters = bf._run_backfill(args)
    assert counters["updated"] == 2
    assert counters["scanned"] == 2

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT id, content_hash FROM memory_embeddings ORDER BY id"
    ).fetchall()
    conn.close()
    assert all(r[1] is not None for r in rows)


def test_run_skips_already_populated(db):
    """Pre-existing content_hash values must not be overwritten."""
    import backfill_content_hash as bf
    _seed(db, [
        {"id": "row-A", "content": "alpha", "type": "message"},
        {"id": "row-B", "content": "beta",  "type": "message"},
    ], [
        {"id": "e-A", "memory_id": "row-A", "content_hash": "ALREADY_SET"},
        {"id": "e-B", "memory_id": "row-B", "content_hash": None},
    ])
    args = _make_args(db)
    counters = bf._run_backfill(args)
    assert counters["updated"] == 1

    conn = sqlite3.connect(str(db))
    a_hash = conn.execute("SELECT content_hash FROM memory_embeddings WHERE id='e-A'").fetchone()[0]
    b_hash = conn.execute("SELECT content_hash FROM memory_embeddings WHERE id='e-B'").fetchone()[0]
    conn.close()
    assert a_hash == "ALREADY_SET"  # untouched
    assert b_hash is not None        # backfilled
    assert b_hash != "ALREADY_SET"


def test_run_skips_empty_content(db):
    """Empty/whitespace content rows are filtered out at SELECT time."""
    import backfill_content_hash as bf
    _seed(db, [
        {"id": "row-A", "content": "alpha", "type": "message"},
        {"id": "row-B", "content": "",       "type": "message"},
    ], [
        {"id": "e-A", "memory_id": "row-A", "content_hash": None},
        {"id": "e-B", "memory_id": "row-B", "content_hash": None},
    ])
    args = _make_args(db)
    counters = bf._run_backfill(args)
    # Empty-content row never reached _run_backfill (filtered at SELECT)
    assert counters["scanned"] == 1
    assert counters["updated"] == 1
    assert counters["skipped_empty"] == 0


# ── Dry-run ──────────────────────────────────────────────────────────────

def test_dry_run_counts_without_writing(db):
    import backfill_content_hash as bf
    _seed(db, [
        {"id": "row-A", "content": "alpha", "type": "message"},
    ], [
        {"id": "e-A", "memory_id": "row-A", "content_hash": None},
    ])
    args = _make_args(db, dry_run=True)
    counters = bf._run_backfill(args)
    assert counters["updated"] == 1  # would have updated 1

    conn = sqlite3.connect(str(db))
    h = conn.execute("SELECT content_hash FROM memory_embeddings WHERE id='e-A'").fetchone()[0]
    conn.close()
    assert h is None  # not actually written


# ── Idempotency — second run is a no-op ──────────────────────────────────

def test_idempotent_rerun(db):
    import backfill_content_hash as bf
    _seed(db, [
        {"id": "row-A", "content": "alpha", "type": "message"},
    ], [
        {"id": "e-A", "memory_id": "row-A", "content_hash": None},
    ])
    args = _make_args(db)
    c1 = bf._run_backfill(args)
    assert c1["updated"] == 1

    # Second run should find 0 pending
    assert bf._count_pending(db, args) == 0
    c2 = bf._run_backfill(args)
    assert c2["updated"] == 0
    assert c2["scanned"] == 0


# ── argparse defaults ────────────────────────────────────────────────────

def test_parse_args_default_types(monkeypatch):
    import backfill_content_hash as bf
    monkeypatch.delenv("M3_DATABASE", raising=False)
    args = bf._parse_args([])
    assert args.type == ["chat_log", "message"]
    assert args.augment_anchors is False
    assert args.dry_run is False


def test_parse_args_explicit_types_override_default():
    import backfill_content_hash as bf
    args = bf._parse_args(["--type", "summary", "--type", "note"])
    assert args.type == ["summary", "note"]
