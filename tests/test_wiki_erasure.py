"""GDPR erasure ↔ wiki: a synthesis derived from an erased member is restricted.

Covers wiki.erasure (find/restrict/full-hook) against an in-memory SQLite store
with a minimal dialect stub — no gdpr_forget, no seam. The render-gate half
(restricted overrides authority) lives in test_wiki_authority_gate.py.
"""
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import erasure as E  # noqa: E402


class _Dialect:
    """Just the primitives wiki.erasure uses, SQLite-flavoured."""

    def placeholder(self, n):
        return ",".join(["?"] * n)

    def param(self):
        return "?"

    def json_column_to_dict(self, value):
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY, type TEXT, "
                 "valid_to TEXT, metadata_json TEXT)")
    conn.execute("CREATE TABLE memory_relationships (from_id TEXT, to_id TEXT, "
                 "relationship_type TEXT)")
    return conn


def _add_syn(db, sid, meta=None):
    db.execute("INSERT INTO memory_items VALUES (?,?,?,?)",
               (sid, "synthesis", None, json.dumps(meta or {})))


def _add_member(db, mid):
    db.execute("INSERT INTO memory_items VALUES (?,?,?,?)",
               (mid, "belief", None, "{}"))


def _consolidates(db, sid, mid):
    db.execute("INSERT INTO memory_relationships VALUES (?,?,?)",
               (sid, mid, "consolidates"))


# ── find_derived_syntheses ───────────────────────────────────────────────────

def test_find_derived_syntheses():
    db, d = _db(), _Dialect()
    _add_syn(db, "syn-1")
    _add_member(db, "m-a")
    _add_member(db, "m-b")
    _consolidates(db, "syn-1", "m-a")
    # erasing m-a hits syn-1; erasing m-b (not consolidated) hits nothing
    assert E.find_derived_syntheses(db, d, ["m-a"]) == ["syn-1"]
    assert E.find_derived_syntheses(db, d, ["m-b"]) == []


def test_find_ignores_non_synthesis_and_superseded():
    db, d = _db(), _Dialect()
    # a superseded synthesis must not be restricted (already out of the vault)
    db.execute("INSERT INTO memory_items VALUES ('syn-old','synthesis','2026-01-01','{}')")
    _add_member(db, "m-a")
    _consolidates(db, "syn-old", "m-a")
    assert E.find_derived_syntheses(db, d, ["m-a"]) == []


def test_find_empty_input():
    db, d = _db(), _Dialect()
    assert E.find_derived_syntheses(db, d, []) == []


# ── restrict_syntheses ───────────────────────────────────────────────────────

def test_restrict_sets_flag_and_record():
    db, d = _db(), _Dialect()
    _add_syn(db, "syn-1", {"authority": "canonical"})
    n = E.restrict_syntheses(db, d, ["syn-1"], erased_count=2, timestamp="2026-07-24T00:00:00Z")
    assert n == 1
    meta = json.loads(db.execute(
        "SELECT metadata_json FROM memory_items WHERE id='syn-1'").fetchone()[0])
    assert meta["restricted"] is True                 # top-level fail-closed flag
    assert meta["review"]["restricted"] is True       # and the review path
    rec = meta["review"]["erasure_records"]
    assert rec == [{"erased_members": 2, "erased_at": "2026-07-24T00:00:00Z",
                    "reason": "gdpr_forget (Art. 17)"}]
    assert meta["authority"] == "canonical"           # authority untouched — restricted OVERRIDES it


def test_restrict_accumulates_across_erasures():
    db, d = _db(), _Dialect()
    _add_syn(db, "syn-1")
    E.restrict_syntheses(db, d, ["syn-1"], erased_count=1, timestamp="t1")
    E.restrict_syntheses(db, d, ["syn-1"], erased_count=1, timestamp="t2")
    meta = json.loads(db.execute(
        "SELECT metadata_json FROM memory_items WHERE id='syn-1'").fetchone()[0])
    assert len(meta["review"]["erasure_records"]) == 2  # both erasures tracked


def test_restrict_idempotent_same_timestamp():
    db, d = _db(), _Dialect()
    _add_syn(db, "syn-1")
    E.restrict_syntheses(db, d, ["syn-1"], erased_count=1, timestamp="t1")
    E.restrict_syntheses(db, d, ["syn-1"], erased_count=1, timestamp="t1")  # replay
    meta = json.loads(db.execute(
        "SELECT metadata_json FROM memory_items WHERE id='syn-1'").fetchone()[0])
    assert len(meta["review"]["erasure_records"]) == 1  # not duplicated


# ── full hook ────────────────────────────────────────────────────────────────

def test_restrict_derived_on_erasure_summary():
    db, d = _db(), _Dialect()
    _add_syn(db, "syn-1")
    _add_member(db, "m-a")
    _consolidates(db, "syn-1", "m-a")
    summary = E.restrict_derived_on_erasure(db, d, ["m-a"], timestamp="t1")
    assert summary == {"restricted": 1, "synthesis_ids": ["syn-1"]}
    meta = json.loads(db.execute(
        "SELECT metadata_json FROM memory_items WHERE id='syn-1'").fetchone()[0])
    assert meta["restricted"] is True


def test_hook_noop_when_no_derived_content():
    db, d = _db(), _Dialect()
    _add_member(db, "m-a")  # erased member feeds no synthesis
    summary = E.restrict_derived_on_erasure(db, d, ["m-a"], timestamp="t1")
    assert summary == {"restricted": 0, "synthesis_ids": []}
