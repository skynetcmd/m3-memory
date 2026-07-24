"""Wiki compile ledger — cluster_run (provenance) + cluster_members (cache), G1.

Drives bin/wiki/ledger.py against an in-memory SQLite store whose schema is the
REAL migration file (memory/migrations/041_wiki_cluster_run.up.sql), so these
tests also prove the migration applies and the columns/indexes match what the code
writes. A SQLite-flavoured dialect stub supplies only the primitives the ledger
uses (param/placeholder/insert_or_ignore/on_conflict_ignore/now).
"""
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import ledger as L  # noqa: E402

_MIGRATION = os.path.join(
    _ROOT, "memory", "migrations", "041_wiki_cluster_run.up.sql")


class _Dialect:
    """SQLite-flavoured primitives the ledger uses (mirrors the real dialect)."""

    def param(self):
        return "?"

    def placeholder(self, n):
        return ",".join(["?"] * n)

    def insert_or_ignore(self):
        return "INSERT OR IGNORE INTO"

    def on_conflict_ignore(self, *, conflict_target="", index_predicate=""):
        return ""  # SQLite: the OR IGNORE prefix already handled it

    def now(self):
        return "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(_MIGRATION, encoding="utf-8") as f:
        conn.executescript(f.read())  # applies the real migration
    return conn


# ── the migration itself ─────────────────────────────────────────────────────

def test_migration_creates_tables_and_indexes():
    db = _db()
    tabs = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cluster_run", "cluster_members"} <= tabs
    idx = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_%'")}
    assert "idx_cluster_members_memory" in idx  # the GDPR scan index


# ── run lifecycle ────────────────────────────────────────────────────────────

def test_open_run_is_invisible_until_completed():
    db, d = _db(), _Dialect()
    rid = L.open_run(db, d, scope="agent", importance_threshold=0.55)
    # In-progress: no completed run resolvable.
    assert L.latest_completed_run(db, d) is None
    L.complete_run(db, d, rid, cluster_count=3)
    assert L.latest_completed_run(db, d) == rid


def test_seq_is_monotonic():
    db, d = _db(), _Dialect()
    r1 = L.open_run(db, d)
    L.complete_run(db, d, r1)
    r2 = L.open_run(db, d)
    s1 = db.execute("SELECT seq FROM cluster_run WHERE id=?", (r1,)).fetchone()["seq"]
    s2 = db.execute("SELECT seq FROM cluster_run WHERE id=?", (r2,)).fetchone()["seq"]
    assert s2 == s1 + 1


def test_record_members_freezes_membership():
    db, d = _db(), _Dialect()
    rid = L.open_run(db, d)
    rows = [("c1", "m-a", "h1", "syn-1"),
            ("c1", "m-b", "h1", "syn-1"),
            ("c2", "m-c", "h2", "syn-2")]
    assert L.record_members(db, d, rid, rows) == 3
    L.complete_run(db, d, rid)
    frozen = L.frozen_membership(db, d, rid)
    assert frozen["c1"]["members"] == {"m-a", "m-b"}
    assert frozen["c1"]["cluster_hash"] == "h1"
    assert frozen["c1"]["head_synthesis_id"] == "syn-1"
    assert frozen["c2"]["members"] == {"m-c"}


def test_record_members_idempotent_on_replay():
    db, d = _db(), _Dialect()
    rid = L.open_run(db, d)
    rows = [("c1", "m-a", "h1", "syn-1")]
    L.record_members(db, d, rid, rows)
    L.record_members(db, d, rid, rows)  # replay (resumed pass) must not error/dupe
    n = db.execute("SELECT COUNT(*) AS c FROM cluster_members "
                   "WHERE run_id=?", (rid,)).fetchone()["c"]
    assert n == 1


def test_members_store_uuids_never_content():
    """G2 invariant: cluster_members holds no content column at all."""
    db = _db()
    cols = {r[1] for r in db.execute("PRAGMA table_info(cluster_members)")}
    assert cols == {"run_id", "cluster_key", "memory_id",
                    "cluster_hash", "head_synthesis_id"}
    assert "content" not in cols and "text" not in cols


# ── skip index ───────────────────────────────────────────────────────────────

def test_hash_is_unchanged_only_for_completed_runs():
    db, d = _db(), _Dialect()
    rid = L.open_run(db, d)
    L.record_members(db, d, rid, [("c1", "m-a", "h-stable", "syn-1")])
    # Not completed yet → the skip index must not see it.
    assert L.hash_is_unchanged(db, d, "c1", "h-stable") is False
    L.complete_run(db, d, rid)
    assert L.hash_is_unchanged(db, d, "c1", "h-stable") is True
    assert L.hash_is_unchanged(db, d, "c1", "h-different") is False


# ── generational prune ───────────────────────────────────────────────────────

def test_prune_keeps_only_the_latest_run_members():
    db, d = _db(), _Dialect()
    r1 = L.open_run(db, d)
    L.record_members(db, d, r1, [("c1", "m-a", "h1", "syn-1")])
    L.complete_run(db, d, r1)
    r2 = L.open_run(db, d)
    L.record_members(db, d, r2, [("c1", "m-a", "h2", "syn-2")])
    L.complete_run(db, d, r2)
    L.prune_superseded_members(db, d, keep_run_id=r2)
    # r1's cache gone, r2's kept; both run rows survive (provenance never pruned).
    assert L.frozen_membership(db, d, r1) == {}
    assert L.frozen_membership(db, d, r2)["c1"]["members"] == {"m-a"}
    runs = db.execute("SELECT COUNT(*) AS c FROM cluster_run").fetchone()["c"]
    assert runs == 2


# ── GDPR scan + flush (G2) ───────────────────────────────────────────────────

def test_scan_and_flush_on_erasure_records_and_flushes():
    db, d = _db(), _Dialect()
    rid = L.open_run(db, d)
    L.record_members(db, d, rid, [("c1", "m-erased", "h1", "syn-1"),
                                  ("c1", "m-keep", "h1", "syn-1")])
    L.complete_run(db, d, rid)
    summary = L.scan_and_flush_on_erasure(db, d, ["m-erased"], timestamp="t1")
    assert summary["runs_flagged"] == 1
    assert rid in summary["run_ids"]
    # Membership cache flushed for that run.
    assert L.frozen_membership(db, d, rid) == {}
    # Run row survives, carrying the erasure record (UUID + count + timestamps).
    row = db.execute(
        "SELECT erased_member_ids, erased_count, first_erased_at, "
        "last_erased_at, membership_flushed FROM cluster_run WHERE id=?",
        (rid,)).fetchone()
    assert json.loads(row["erased_member_ids"]) == ["m-erased"]
    assert row["erased_count"] == 1
    assert row["first_erased_at"] == "t1" and row["last_erased_at"] == "t1"
    assert row["membership_flushed"] == 1


def test_scan_accumulates_multiple_erasures():
    db, d = _db(), _Dialect()
    rid = L.open_run(db, d)
    L.record_members(db, d, rid, [("c1", "m-1", "h1", "syn-1")])
    L.complete_run(db, d, rid)
    L.scan_and_flush_on_erasure(db, d, ["m-1"], timestamp="t1")
    # A second erasure touching the SAME run row (re-recorded via a later run):
    L.record_members(db, d, rid, [("c1", "m-2", "h1", "syn-1")])  # re-add to same run
    L.scan_and_flush_on_erasure(db, d, ["m-2"], timestamp="t2")
    row = db.execute("SELECT erased_member_ids, erased_count, first_erased_at, "
                     "last_erased_at FROM cluster_run WHERE id=?", (rid,)).fetchone()
    assert set(json.loads(row["erased_member_ids"])) == {"m-1", "m-2"}
    assert row["erased_count"] == 2
    assert row["first_erased_at"] == "t1"   # set once
    assert row["last_erased_at"] == "t2"    # advances


def test_scan_noop_when_erased_id_not_cached():
    db, d = _db(), _Dialect()
    rid = L.open_run(db, d)
    L.record_members(db, d, rid, [("c1", "m-a", "h1", "syn-1")])
    L.complete_run(db, d, rid)
    summary = L.scan_and_flush_on_erasure(db, d, ["m-unrelated"], timestamp="t1")
    assert summary == {"runs_flagged": 0, "members_flushed": 0, "run_ids": []}
    # Membership intact.
    assert L.frozen_membership(db, d, rid)["c1"]["members"] == {"m-a"}


def test_scan_empty_input():
    db, d = _db(), _Dialect()
    assert L.scan_and_flush_on_erasure(db, d, [], timestamp="t1") == {
        "runs_flagged": 0, "members_flushed": 0, "run_ids": []}
