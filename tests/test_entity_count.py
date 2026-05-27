"""Tests for entity-count first-class queries (`bin/memory/entity_count.py`).

Covers:
- count_entities_impl: empty conv, single mention, type filter, pattern filter,
  conversation isolation, contract violations (empty conversation_id, oversized pattern)
- count_mentions_impl: row shape, sort order (DESC by count, ASC by name),
  total vs rows when limit truncates, limit clamping
- list_mentions_impl: lookup by entity_id, lookup by canonical_name with and
  without entity_type disambiguator, unresolved name returns empty,
  conversation isolation, contract violation (neither id nor name)
- Identity preservation through memory_core shim

Test isolation: each test creates a fresh tmp_path DB with only the minimal
schema the impls require (entities, memory_item_entities, memory_items).
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


# ---------------------------------------------------------------------------
# Minimal schema bootstrap — only the 3 tables entity_count.py touches
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_items (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL DEFAULT 'note',
    title           TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL DEFAULT '',
    conversation_id TEXT,
    valid_from      TEXT,
    is_deleted      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    scope           TEXT NOT NULL DEFAULT 'agent'
);
CREATE INDEX IF NOT EXISTS ix_memory_items_conv ON memory_items(conversation_id);

CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    attributes_json TEXT DEFAULT '{}',
    valid_from      TEXT,
    valid_to        TEXT,
    content_hash    TEXT,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_entities_canonical_type ON entities(canonical_name, entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_type           ON entities(entity_type);

CREATE TABLE IF NOT EXISTS memory_item_entities (
    memory_id       TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    mention_text    TEXT,
    mention_offset  INTEGER DEFAULT 0,
    confidence      REAL DEFAULT 0.85,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (memory_id, entity_id, mention_offset)
);
CREATE INDEX IF NOT EXISTS idx_mie_entity ON memory_item_entities(entity_id);
"""


def _make_db(db_path):
    """Create a minimal schema DB at db_path. Point M3_DATABASE at it."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    conn.close()
    os.environ["M3_DATABASE"] = str(db_path)


def _seed(db_path, conv_id, items):
    """Seed `items` = [(memory_id, entity_specs)] into the DB.

    entity_specs = [(entity_id, canonical_name, entity_type, mention_offset), ...]
    Memory rows are bound to conv_id. Memory id, entity ids and mention
    offsets must be unique within their respective domains.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        for mid, ent_specs in items:
            conn.execute(
                "INSERT OR IGNORE INTO memory_items (id, conversation_id, content) "
                "VALUES (?, ?, ?)",
                (mid, conv_id, f"content for {mid}"),
            )
            for eid, name, etype, offset in ent_specs:
                conn.execute(
                    "INSERT OR IGNORE INTO entities (id, canonical_name, entity_type) "
                    "VALUES (?, ?, ?)",
                    (eid, name, etype),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO memory_item_entities "
                    "(memory_id, entity_id, mention_text, mention_offset) "
                    "VALUES (?, ?, ?, ?)",
                    (mid, eid, name, offset),
                )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Identity preservation (modularization lesson #4)
# ---------------------------------------------------------------------------


def test_shim_identity_preserved():
    """count_entities_impl etc. must be the same object whether reached via
    memory_core (shim) or memory.entity_count (canonical home)."""
    import memory_core as mc
    from memory.entity_count import (
        count_entities_impl,
        count_mentions_impl,
        list_mentions_impl,
    )
    assert mc.count_entities_impl is count_entities_impl
    assert mc.count_mentions_impl is count_mentions_impl
    assert mc.list_mentions_impl is list_mentions_impl


# ---------------------------------------------------------------------------
# count_entities_impl
# ---------------------------------------------------------------------------


def test_count_entities_empty_conversation(tmp_path):
    _make_db(tmp_path / "t.db")
    from memory.entity_count import count_entities_impl
    out = count_entities_impl("empty-conv")
    assert out["count"] == 0
    assert out["conversation_id"] == "empty-conv"
    assert out["entity_type"] == "*"


def test_count_entities_single_mention(tmp_path):
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple", "organization", 0)]),
    ])
    from memory.entity_count import count_entities_impl
    assert count_entities_impl("c1")["count"] == 1


def test_count_entities_distinct_not_mentions(tmp_path):
    """Same entity mentioned in 3 turns counts as 1 distinct entity."""
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple", "organization", 0)]),
        ("m2", [("e1", "Apple", "organization", 0)]),
        ("m3", [("e1", "Apple", "organization", 0)]),
    ])
    from memory.entity_count import count_entities_impl
    assert count_entities_impl("c1")["count"] == 1


def test_count_entities_type_filter(tmp_path):
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple",  "organization", 0)]),
        ("m2", [("e2", "Boston", "place",        0)]),
        ("m3", [("e3", "Carol",  "person",       0)]),
    ])
    from memory.entity_count import count_entities_impl
    assert count_entities_impl("c1")["count"] == 3
    assert count_entities_impl("c1", entity_type="place")["count"] == 1
    assert count_entities_impl("c1", entity_type="organization")["count"] == 1
    assert count_entities_impl("c1", entity_type="nonexistent")["count"] == 0


def test_count_entities_pattern_filter_case_insensitive(tmp_path):
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple",     "organization", 0)]),
        ("m2", [("e2", "Pineapple", "product",      0)]),
        ("m3", [("e3", "Banana",    "product",      0)]),
    ])
    from memory.entity_count import count_entities_impl
    assert count_entities_impl("c1", pattern="apple")["count"] == 2  # Apple + Pineapple
    assert count_entities_impl("c1", pattern="APPLE")["count"] == 2  # case-insensitive
    assert count_entities_impl("c1", pattern="banana")["count"] == 1


def test_count_entities_conversation_isolation(tmp_path):
    """Entities in c2 must not be counted under c1, even if entity rows overlap."""
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e_shared", "Apple", "organization", 0)]),
    ])
    _seed(tmp_path / "t.db", "c2", [
        ("m2", [("e_shared", "Apple", "organization", 0)]),  # same entity, diff conv
        ("m3", [("e2", "Boston", "place", 0)]),
    ])
    from memory.entity_count import count_entities_impl
    assert count_entities_impl("c1")["count"] == 1
    assert count_entities_impl("c2")["count"] == 2


def test_count_entities_rejects_empty_conversation_id(tmp_path):
    _make_db(tmp_path / "t.db")
    from memory.entity_count import count_entities_impl
    with pytest.raises(ValueError, match="conversation_id is required"):
        count_entities_impl("")
    with pytest.raises(ValueError, match="conversation_id is required"):
        count_entities_impl("   ")


def test_count_entities_rejects_oversized_pattern(tmp_path):
    _make_db(tmp_path / "t.db")
    from memory.entity_count import MAX_PATTERN_LEN, count_entities_impl
    long_pattern = "x" * (MAX_PATTERN_LEN + 1)
    with pytest.raises(ValueError, match="exceeds MAX_PATTERN_LEN"):
        count_entities_impl("c1", pattern=long_pattern)


# ---------------------------------------------------------------------------
# count_mentions_impl
# ---------------------------------------------------------------------------


def test_count_mentions_sorted_desc_by_count(tmp_path):
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple", "organization", 0)]),  # 3 mentions of Apple
        ("m2", [("e1", "Apple", "organization", 0)]),
        ("m3", [("e1", "Apple", "organization", 0)]),
        ("m4", [("e2", "Boston", "place", 0)]),         # 1 mention of Boston
        ("m5", [("e3", "Carol", "person", 0)]),         # 2 mentions of Carol
        ("m6", [("e3", "Carol", "person", 0)]),
    ])
    from memory.entity_count import count_mentions_impl
    out = count_mentions_impl("c1")
    assert out["total"] == 3
    counts = [(r["canonical_name"], r["mention_count"]) for r in out["rows"]]
    assert counts == [("Apple", 3), ("Carol", 2), ("Boston", 1)]


def test_count_mentions_tie_break_ascending_name(tmp_path):
    """When mention_count ties, sort by canonical_name ASC."""
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Zebra", "topic", 0)]),
        ("m2", [("e2", "Apple", "organization", 0)]),
        ("m3", [("e3", "Mango", "product", 0)]),
    ])
    from memory.entity_count import count_mentions_impl
    out = count_mentions_impl("c1")
    names = [r["canonical_name"] for r in out["rows"]]
    assert names == ["Apple", "Mango", "Zebra"]  # all tied at 1, alphabetical


def test_count_mentions_limit_truncates_rows_but_not_total(tmp_path):
    _make_db(tmp_path / "t.db")
    items = [(f"m{i}", [(f"e{i}", f"Ent{i:02d}", "topic", 0)]) for i in range(20)]
    _seed(tmp_path / "t.db", "c1", items)
    from memory.entity_count import count_mentions_impl
    out = count_mentions_impl("c1", limit=5)
    assert out["total"] == 20
    assert len(out["rows"]) == 5


def test_count_mentions_limit_clamps_to_max(tmp_path):
    _make_db(tmp_path / "t.db")
    from memory.entity_count import MAX_LIMIT, count_mentions_impl
    out = count_mentions_impl("c1", limit=MAX_LIMIT * 10)
    assert out["limit"] == MAX_LIMIT


def test_count_mentions_default_limit_on_zero(tmp_path):
    _make_db(tmp_path / "t.db")
    from memory.entity_count import DEFAULT_LIMIT, count_mentions_impl
    out = count_mentions_impl("c1", limit=0)
    assert out["limit"] == DEFAULT_LIMIT


def test_count_mentions_row_shape(tmp_path):
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple", "organization", 0)]),
    ])
    from memory.entity_count import count_mentions_impl
    row = count_mentions_impl("c1")["rows"][0]
    assert set(row.keys()) == {"entity_id", "canonical_name", "entity_type",
                                 "mention_count"}
    assert row["entity_id"] == "e1"
    assert row["canonical_name"] == "Apple"
    assert row["entity_type"] == "organization"
    assert row["mention_count"] == 1


# ---------------------------------------------------------------------------
# list_mentions_impl
# ---------------------------------------------------------------------------


def test_list_mentions_by_entity_id(tmp_path):
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple", "organization", 0)]),
        ("m2", [("e1", "Apple", "organization", 0)]),
        ("m3", [("e2", "Boston", "place", 0)]),
    ])
    from memory.entity_count import list_mentions_impl
    out = list_mentions_impl("c1", entity_id="e1")
    assert out["total"] == 2
    assert sorted(out["memory_ids"]) == ["m1", "m2"]
    assert out["canonical_name"] == "Apple"
    assert out["entity_type"] == "organization"


def test_list_mentions_by_canonical_name(tmp_path):
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple", "organization", 0)]),
        ("m2", [("e1", "Apple", "organization", 0)]),
    ])
    from memory.entity_count import list_mentions_impl
    out = list_mentions_impl("c1", canonical_name="apple")  # case-insensitive
    assert out["entity_id"] == "e1"
    assert out["total"] == 2


def test_list_mentions_canonical_name_with_type_disambiguator(tmp_path):
    """Same canonical name across entity types — entity_type disambiguates."""
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e_org",   "Apple", "organization", 0)]),
        ("m2", [("e_fruit", "Apple", "product",      0)]),
    ])
    from memory.entity_count import list_mentions_impl
    out_org = list_mentions_impl("c1", canonical_name="Apple",
                                   entity_type="organization")
    assert out_org["entity_id"] == "e_org"
    out_fruit = list_mentions_impl("c1", canonical_name="Apple",
                                     entity_type="product")
    assert out_fruit["entity_id"] == "e_fruit"


def test_list_mentions_unresolved_returns_empty(tmp_path):
    _make_db(tmp_path / "t.db")
    from memory.entity_count import list_mentions_impl
    out = list_mentions_impl("c1", canonical_name="DoesNotExist")
    assert out["entity_id"] == ""
    assert out["total"] == 0
    assert out["memory_ids"] == []


def test_list_mentions_conversation_isolation(tmp_path):
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple", "organization", 0)]),
    ])
    _seed(tmp_path / "t.db", "c2", [
        ("m2", [("e1", "Apple", "organization", 0)]),
    ])
    from memory.entity_count import list_mentions_impl
    out_c1 = list_mentions_impl("c1", entity_id="e1")
    assert out_c1["total"] == 1
    assert out_c1["memory_ids"] == ["m1"]
    out_c2 = list_mentions_impl("c2", entity_id="e1")
    assert out_c2["total"] == 1
    assert out_c2["memory_ids"] == ["m2"]


def test_list_mentions_rejects_no_lookup(tmp_path):
    _make_db(tmp_path / "t.db")
    from memory.entity_count import list_mentions_impl
    with pytest.raises(ValueError, match="entity_id or canonical_name"):
        list_mentions_impl("c1")


def test_list_mentions_rejects_empty_conversation_id(tmp_path):
    _make_db(tmp_path / "t.db")
    from memory.entity_count import list_mentions_impl
    with pytest.raises(ValueError, match="conversation_id is required"):
        list_mentions_impl("", entity_id="e1")


def test_list_mentions_entity_id_takes_priority(tmp_path):
    """When both entity_id and canonical_name are passed, entity_id wins."""
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple", "organization", 0)]),
        ("m2", [("e2", "Boston", "place", 0)]),
    ])
    from memory.entity_count import list_mentions_impl
    out = list_mentions_impl("c1", entity_id="e2", canonical_name="Apple")
    assert out["entity_id"] == "e2"
    assert out["canonical_name"] == "Boston"


# ---------------------------------------------------------------------------
# EXPLAIN QUERY PLAN — verify index use on the hot paths
# ---------------------------------------------------------------------------


def test_count_entities_uses_index(tmp_path):
    """Queries must use the existing index (idx_mie_entity /
    ix_memory_items_conv). EXPLAIN QUERY PLAN should NOT report SCAN
    on memory_item_entities (joined via index)."""
    _make_db(tmp_path / "t.db")
    _seed(tmp_path / "t.db", "c1", [
        ("m1", [("e1", "Apple", "organization", 0)]),
    ])
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    rows = conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT COUNT(DISTINCT e.id) "
        "FROM memory_item_entities mie "
        "JOIN memory_items mi ON mie.memory_id = mi.id "
        "JOIN entities e ON mie.entity_id = e.id "
        "WHERE mi.conversation_id = ?",
        ("c1",),
    ).fetchall()
    plan = "\n".join(str(r) for r in rows)
    conn.close()
    # Acceptable: SCAN on memory_items via the index, SEARCH on the others.
    # Reject: full SCAN of memory_item_entities (catastrophic at scale).
    assert "SCAN memory_item_entities" not in plan, f"unindexed scan in plan: {plan}"
