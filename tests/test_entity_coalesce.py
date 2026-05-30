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


@pytest.mark.parametrize("a,b", [
    ("content_hash", "_content_hash"),
    ("predicates", "_PREDICATES"),
    ("routed_impl", "_routed_impl"),
    ("foo", "__foo"),
])
def test_underscore_collision_demotes_helper_vs_value(a, b):
    # leading-underscore difference = private helper vs public value = different
    assert ec._underscore_collision(a, b) is True


@pytest.mark.parametrize("a,b", [
    ("memory item", "memory items"),     # singular/plural -> legit merge
    ("agent prompt", "agent prompts"),
    ("SQLite database", "SQLite databases"),
    ("foo", "foo"),                       # identical -> not a "collision"
    ("foo", "bar"),                       # unrelated
])
def test_underscore_collision_allows_real_variants(a, b):
    assert ec._underscore_collision(a, b) is False


@pytest.mark.parametrize("a,b", [
    ("run-config-k20", "run-config-k30"),  # trailing-number config (live false merge)
    ("gpt-4", "gpt-5"),
    ("baseline-1", "baseline-2"),
    ("v1", "v2"),
    ("phase 3", "phase 4"),
    ("qwen3-embedding-0.6b", "qwen3-embedding-1.5b"),
])
def test_numeric_suffix_collision_demotes_distinct_configs(a, b):
    # differ only by a numeric/version token = distinct config/version = different
    assert ec._numeric_suffix_collision(a, b) is True


@pytest.mark.parametrize("a,b", [
    ("entity row", "entity rows"),        # singular/plural -> legit merge
    ("MCP server", "MCP servers"),
    ("memory/migrations", "memory/migrations/"),  # trailing slash -> legit merge
    ("small models", "small model"),
    ("qwen3-embedding-0.6b", "qwen3-embedding:0.6b"),  # same number, separator differs
    ("content_hash", "_content_hash"),    # underscore guard's job, not this one
    ("alpha-v2", "beta-v3"),              # word ALSO differs -> not purely numeric
    ("foo", "foo"),                       # identical
])
def test_numeric_suffix_collision_allows_real_variants(a, b):
    assert ec._numeric_suffix_collision(a, b) is False


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
        CREATE TABLE entity_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity TEXT NOT NULL, to_entity TEXT NOT NULL,
            predicate TEXT NOT NULL, confidence REAL DEFAULT 0.85,
            valid_from TEXT, valid_to TEXT, source_memory_id TEXT, created_at TEXT
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


# ── v2: reversible overlay apply / unapply ───────────────────────────────────
def test_apply_creates_reversible_overlay_then_unapply_restores(tmp_entity_db):
    # detect -> the 'small models'/'small model' pair lands in the merge band
    ec.coalesce_detect(dry_run=False, max_pairs=50)
    # explicit db_path satisfies the mutation guard AND exercises isolation
    res = ec.apply_coalescence(include_auto_merge=True, dry_run=False,
                               db_path=os.environ["M3_MEMORY_DB"])
    assert res["applied"] >= 1
    cid = res["clusters_touched"][0]

    conn = sqlite3.connect(os.environ["M3_MEMORY_DB"])
    # overlay written: a same_as edge + a clustered member, NO entity deleted
    sa = conn.execute("SELECT count(*) FROM entity_relationships WHERE predicate='same_as'").fetchone()[0]
    clustered = conn.execute("SELECT count(*) FROM entities WHERE coalesce_state='clustered'").fetchone()[0]
    total = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
    conn.close()
    assert sa >= 1 and clustered >= 1
    assert total == 7  # nothing deleted — members intact (lesson #1)

    # reverse: unapply fully restores (drop edge + clear cluster_id)
    rev = ec.unapply_cluster(cid, db_path=os.environ["M3_MEMORY_DB"])
    assert rev["reverted_members"] >= 1
    conn = sqlite3.connect(os.environ["M3_MEMORY_DB"])
    assert conn.execute("SELECT count(*) FROM entities WHERE cluster_id=?", (cid,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM entity_relationships WHERE predicate='same_as'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 7  # still all there
    conn.close()


def test_apply_dry_run_writes_nothing(tmp_entity_db):
    ec.coalesce_detect(dry_run=False, max_pairs=50)
    ec.apply_coalescence(include_auto_merge=True, dry_run=True)  # dry-run needs no guard
    conn = sqlite3.connect(os.environ["M3_MEMORY_DB"])
    sa = conn.execute("SELECT count(*) FROM entity_relationships WHERE predicate='same_as'").fetchone()[0]
    conn.close()
    assert sa == 0  # dry-run applied nothing


def test_apply_with_nothing_selected_is_noop(tmp_entity_db):
    ec.coalesce_detect(dry_run=False, max_pairs=50)
    # neither explicit uuids nor include_auto_merge -> nothing selected
    res = ec.apply_coalescence(dry_run=False, db_path=os.environ["M3_MEMORY_DB"])
    assert res["applied"] == 0


def test_apply_refuses_real_write_without_target_or_confirm(tmp_entity_db):
    # MUTATION GUARD: a real apply with neither db_path nor confirm must refuse,
    # NOT silently write to the resolved core DB.
    ec.coalesce_detect(dry_run=False, max_pairs=50)
    res = ec.apply_coalescence(include_auto_merge=True, dry_run=False)  # no db_path, no confirm
    assert res["applied"] == 0
    assert "refused" in res.get("error", "")
    # nothing written
    conn = sqlite3.connect(os.environ["M3_MEMORY_DB"])
    sa = conn.execute("SELECT count(*) FROM entity_relationships WHERE predicate='same_as'").fetchone()[0]
    conn.close()
    assert sa == 0


def test_apply_allows_real_write_with_confirm(tmp_entity_db, monkeypatch):
    # confirm=True is the explicit acknowledgement path (resolves core DB; here
    # the fixture pointed M3_MEMORY_DB at the tmp DB, so it stays isolated).
    ec.coalesce_detect(dry_run=False, max_pairs=50)
    res = ec.apply_coalescence(include_auto_merge=True, dry_run=False, confirm=True)
    assert res["applied"] >= 1
