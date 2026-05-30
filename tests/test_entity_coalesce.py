"""Tests for the provisional-entity coalescing pass (v1: detect + quarantine +
review). Deterministic + hermetic: the integration tests use a tmp agent DB and
run in dry_run mode so they never touch the embedder or the live store.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from files_memory import entity_coalesce as ec  # noqa: E402


# ── Pure-function units (no DB, no embedder) ─────────────────────────────────
@pytest.mark.parametrize("name", [
    "$0.25", "$13 to $26", "%APPDATA%", "$M3_BRIDGE_PATH", "#bug-reports",
    "'SEARCH:'", "35 minutes", "9d884ed8", "status", "result", "COALESCE",
    "database", "",
])
def test_is_noise_flags_non_entities(name):
    assert ec._is_noise(name) is True


@pytest.mark.parametrize("name", [
    "SQLite", "enrichment_groups", "Project Oxidation", "ChromaDB",
    "memory_search", "BGE-M3",
])
def test_is_noise_keeps_real_entities(name):
    assert ec._is_noise(name) is False


def test_block_key_first_token_normalized():
    assert ec._block_key("Data Warehouse") == "data"
    assert ec._block_key("data warehouse") == "data"      # same block, word-order
    assert ec._block_key("enrichment_groups") == "enrichment"
    assert ec._block_key("") == ""


def test_name_hash_stable_and_case_insensitive():
    assert ec._name_hash("SQLite") == ec._name_hash(" sqlite ")
    assert ec._name_hash("a") != ec._name_hash("b")


def test_embed_tier_info_reports_fallback(monkeypatch):
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    info = ec._embed_tier_info("bge-m3-GGUF-Q4_K_M.gguf")
    assert info["in_process"] is False
    assert "M3_EMBED_GGUF" in info["hint"]


# ── Integration: tmp DB, dry-run (no embedder) ───────────────────────────────
@pytest.fixture
def tmp_entity_db(tmp_path, monkeypatch):
    """A minimal agent_memory.db with an `entities` table + a few rows."""
    db = tmp_path / "agent_memory.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE entities (
            id TEXT PRIMARY KEY, canonical_name TEXT, entity_type TEXT,
            attributes_json TEXT, valid_from TEXT, valid_to TEXT,
            content_hash TEXT, created_at TEXT, updated_at TEXT
        );
        """
    )
    prov = '{"provisional": true, "first_seen_in": "files.db"}'
    rows = [
        # noise -> should quarantine
        ("e1", "$0.25", "unknown", prov),
        ("e2", "%APPDATA%", "unknown", prov),
        ("e3", "status", "unknown", prov),
        # real near-dupes in the same block -> candidate pair
        ("e4", "small models", "unknown", prov),
        ("e5", "small model", "unknown", prov),
        # a singleton real entity -> no candidate
        ("e6", "ChromaDB", "unknown", prov),
        # a curated (non-provisional) entity -> ignored by the pass
        ("e7", "SQLite", "protocol", "{}"),
    ]
    conn.executemany(
        "INSERT INTO entities(id, canonical_name, entity_type, attributes_json) "
        "VALUES (?,?,?,?)", rows,
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("M3_MEMORY_DB", str(db))
    monkeypatch.setenv("M3_DATABASE", str(db))  # must NOT mislead the pass
    return db


def test_dry_run_detects_and_quarantines_without_writing(tmp_entity_db):
    r = ec.coalesce_detect(dry_run=True, max_pairs=50)
    assert "error" not in r, r.get("error")
    # noise quarantined (counted) but NOT persisted in dry-run
    assert r["prune"]["quarantined"] == 3
    # blocking: "small models"/"small model" share block "small"; others singleton
    assert r["blocks_multi"] >= 1
    # dry-run does not embed
    assert r["embedded"] == 0
    # nothing written: candidates table empty
    assert ec.list_coalesce_candidates(limit=99) == []
    # quarantine flag NOT applied in dry-run
    conn = sqlite3.connect(os.environ["M3_MEMORY_DB"])
    q = conn.execute("SELECT count(*) FROM entities WHERE coalesce_state='quarantined'").fetchone()[0]
    conn.close()
    assert q == 0


def test_real_run_persists_quarantine_and_fuzzy_candidate(tmp_entity_db):
    # "small models" vs "small model" -> token_sort fuzzy ~95 >= FUZZY_HIGH, so
    # it is banded WITHOUT needing the embedder (the high-fuzzy path).
    r = ec.coalesce_detect(dry_run=False, max_pairs=50)
    assert "error" not in r, r.get("error")
    assert r["prune"]["quarantined"] == 3
    # quarantine persisted
    conn = sqlite3.connect(os.environ["M3_MEMORY_DB"])
    q = conn.execute("SELECT count(*) FROM entities WHERE coalesce_state='quarantined'").fetchone()[0]
    conn.close()
    assert q == 3
    # the high-fuzzy dup pair is recorded as a 'merge'-band candidate
    cands = ec.list_coalesce_candidates(limit=99)
    names = {frozenset((c["entity_a"]["name"], c["entity_b"]["name"])) for c in cands}
    assert frozenset(("small models", "small model")) in names
    pair = next(c for c in cands if c["entity_a"]["name"] in ("small models", "small model"))
    assert pair["band"] == "merge"
    assert pair["fuzzy"] >= 90


def test_bulk_review_records_decisions(tmp_entity_db):
    ec.coalesce_detect(dry_run=False, max_pairs=50)
    cands = ec.list_coalesce_candidates(reviewed=False, limit=99)
    assert cands, "expected at least one candidate"
    res = ec.review_coalesce_candidates(
        [{"uuid": cands[0]["uuid"], "action": "merge"},
         {"uuid": "nonexistent", "action": "reject"}],
        note="unit test",
    )
    assert res["updated"] == 1
    assert any(e.get("uuid") == "nonexistent" for e in res["errors"])
    # the reviewed one drops out of the unreviewed list
    after = ec.list_coalesce_candidates(reviewed=False, limit=99)
    assert len(after) == len(cands) - 1


def test_review_rejects_bad_action(tmp_entity_db):
    ec.coalesce_detect(dry_run=False, max_pairs=50)
    cands = ec.list_coalesce_candidates(limit=1)
    res = ec.review_coalesce_candidates([{"uuid": cands[0]["uuid"], "action": "bogus"}])
    assert res["updated"] == 0
    assert res["errors"]


def test_review_non_list_raises(tmp_entity_db):
    with pytest.raises(ValueError):
        ec.review_coalesce_candidates({"uuid": "x", "action": "merge"})  # type: ignore[arg-type]
