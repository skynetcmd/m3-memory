"""Commit 4a: blast-radius + delta-lint (deterministic graph walk).

The metric is EXACT — 100% precision AND recall on a known topology — because the
answer is a graph fact, not a judgement. A wrong synthesis contaminates exactly
the syntheses compiled FROM it (transitively via consolidates / distills_from),
and nothing else.
"""
import os
import sqlite3
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import blast_radius as B  # noqa: E402
from wiki.build import WikiOptions, build_wiki  # noqa: E402
from wiki.cluster import Cluster  # noqa: E402
from wiki.select import Edge, Mem  # noqa: E402


def _mem(id, verdict=None):
    meta = {"verdict": verdict} if verdict is not None else {}
    return Mem(id=id, type="synthesis", title=id, content=f"c-{id}",
               importance=0.5, confidence=0.5, valid_from=None, valid_to=None,
               pinned=0, created_at=None, updated_at=None, metadata=meta)


def _clusters(*mems):
    return [Cluster(key=m.id, members=[m]) for m in mems]


def _edge(frm, to, rel="consolidates"):
    return Edge(from_id=frm, to_id=to, rel=rel)


# ── exact topology: precision AND recall ─────────────────────────────────────

def test_chain_contaminates_all_downstream():
    """C consolidates B consolidates A; A wrong ⇒ exactly {B, C}, nothing else."""
    clusters = _clusters(_mem("A", verdict="wrong"), _mem("B"), _mem("C"), _mem("Z"))
    edges = [_edge("B", "A"), _edge("C", "B")]  # B<-A, C<-B ; Z unrelated
    assert B.contaminated(clusters, edges) == {"B", "C"}


def test_unrelated_node_is_not_flagged():
    """Precision: a synthesis not downstream of the wrong one is untouched."""
    clusters = _clusters(_mem("A", verdict="wrong"), _mem("B"), _mem("Z"))
    edges = [_edge("B", "A")]  # Z has no path to A
    result = B.contaminated(clusters, edges)
    assert "Z" not in result
    assert result == {"B"}


def test_nothing_wrong_means_empty_radius():
    clusters = _clusters(_mem("A"), _mem("B"))
    edges = [_edge("B", "A")]
    assert B.contaminated(clusters, edges) == set()


def test_seeds_themselves_are_excluded():
    """The radius is the DOWNSTREAM set; the wrong node itself is the input."""
    clusters = _clusters(_mem("A", verdict="wrong"), _mem("B"))
    edges = [_edge("B", "A")]
    assert "A" not in B.contaminated(clusters, edges)


def test_supersedes_is_not_a_provenance_edge():
    """A supersede REPLACES, it does not derive-from — must not propagate taint."""
    clusters = _clusters(_mem("A", verdict="wrong"), _mem("B"))
    edges = [_edge("B", "A", rel="supersedes")]
    assert B.contaminated(clusters, edges) == set()


def test_distills_from_also_propagates():
    clusters = _clusters(_mem("A", verdict="wrong"), _mem("B"))
    edges = [_edge("B", "A", rel="distills_from")]
    assert B.contaminated(clusters, edges) == {"B"}


def test_cycle_is_safe():
    """A pathological consolidates cycle must terminate, not hang."""
    clusters = _clusters(_mem("A", verdict="wrong"), _mem("B"), _mem("C"))
    edges = [_edge("B", "A"), _edge("C", "B"), _edge("A", "C")]  # cycle
    result = B.contaminated(clusters, edges)
    assert result == {"B", "C"}  # A is a seed, excluded; walk terminates


def test_multiple_wrong_roots_union():
    clusters = _clusters(_mem("A", verdict="wrong"), _mem("X", verdict="wrong"),
                         _mem("B"), _mem("Y"))
    edges = [_edge("B", "A"), _edge("Y", "X")]
    assert B.contaminated(clusters, edges) == {"B", "Y"}


# ── delta-lint scope ─────────────────────────────────────────────────────────

def test_delta_scope_is_changed_plus_direct_dependents():
    edges = [_edge("B", "A"), _edge("C", "B")]
    # A changed → re-check A and its DIRECT dependent B (not C, one hop).
    assert B.delta_scope(edges, {"A"}) == {"A", "B"}


def test_delta_scope_of_leaf_is_just_itself():
    edges = [_edge("B", "A")]
    assert B.delta_scope(edges, {"B"}) == {"B"}  # nothing consolidates from B


# ── end-to-end: lint page renders the section deterministically ──────────────

def _db_with_wrong_chain():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """CREATE TABLE memory_items (id TEXT PRIMARY KEY, type TEXT, title TEXT,
             content TEXT, importance REAL DEFAULT 0.5, confidence REAL,
             valid_from TEXT, valid_to TEXT, pinned INTEGER DEFAULT 0,
             is_deleted INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT,
             metadata_json TEXT);
           CREATE TABLE memory_relationships (from_id TEXT, to_id TEXT,
             relationship_type TEXT, created_at TEXT);
           CREATE TABLE memory_item_entities (memory_id TEXT, entity_id TEXT,
             mention_text TEXT, mention_offset INTEGER DEFAULT 0,
             confidence REAL DEFAULT 0.85);"""
    )
    import json
    rows = [
        ("wrong-a", "synthesis", "Wrong A", "bad root",
         json.dumps({"authority": "canonical", "verdict": "wrong"})),
        ("dep-b", "synthesis", "Dependent B", "downstream",
         json.dumps({"authority": "canonical"})),
    ]
    conn.executemany(
        "INSERT INTO memory_items (id,type,title,content,importance,confidence,"
        "valid_from,created_at,updated_at,metadata_json) VALUES "
        "(?,?,?,?,0.9,0.8,'2026-01-01','2026-01-01','2026-01-02',?)",
        [(r[0], r[1], r[2], r[3], r[4]) for r in rows],
    )
    conn.execute("INSERT INTO memory_relationships (from_id,to_id,relationship_type) "
                 "VALUES ('dep-b','wrong-a','consolidates')")
    conn.commit()
    return conn


def test_lint_page_reports_blast_radius():
    conn = _db_with_wrong_chain()
    try:
        pages = build_wiki(conn, None, WikiOptions(importance_threshold=0.6,
                                                   use_networkx=False))
    finally:
        conn.close()
    lint = pages["lint.md"]
    assert "## Blast radius (1)" in lint
    assert "dep-b"[:8] in lint  # the contaminated downstream synthesis is named


def test_blast_radius_adds_zero_queries():
    """S7: the walk uses ONLY the already-loaded edge list. A per-node re-query
    would reintroduce an N+1 the metric forbids. Proven by comparing the SQL
    statement count of a build WITH a wrong-verdict chain against one without —
    the blast-radius section must add nothing.

    sqlite3.set_trace_callback fires per executed statement — the real query
    count, independent of the row factory the seam sets.
    """
    def count(with_wrong):
        conn = _db_with_wrong_chain() if with_wrong else _db_clean()
        n = [0]
        conn.set_trace_callback(lambda _sql: n.__setitem__(0, n[0] + 1))
        try:
            build_wiki(conn, None, WikiOptions(importance_threshold=0.6,
                                               use_networkx=False))
        finally:
            conn.set_trace_callback(None)
            conn.close()
        return n[0]

    with_wrong = count(True)
    clean = count(False)
    # The clean store has one fewer relationship row, so allow that the fixtures
    # differ by their own inserts — what matters is the BUILD does not scale
    # queries with the wrong-verdict walk. Assert the walk itself issues none by
    # re-counting on the SAME fixture with the section reachable vs not: here we
    # assert the with-wrong build is not MORE query-heavy per contaminated node.
    # Both fixtures are 2 items / 1 edge, so query counts must be identical.
    assert with_wrong == clean, (
        f"blast radius scaled queries: {with_wrong} vs {clean} — expected equal")


def _db_clean():
    """Same shape as _db_with_wrong_chain but with no wrong verdict — so the two
    differ ONLY in metadata, never in row/edge count."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """CREATE TABLE memory_items (id TEXT PRIMARY KEY, type TEXT, title TEXT,
             content TEXT, importance REAL DEFAULT 0.5, confidence REAL,
             valid_from TEXT, valid_to TEXT, pinned INTEGER DEFAULT 0,
             is_deleted INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT,
             metadata_json TEXT);
           CREATE TABLE memory_relationships (from_id TEXT, to_id TEXT,
             relationship_type TEXT, created_at TEXT);
           CREATE TABLE memory_item_entities (memory_id TEXT, entity_id TEXT,
             mention_text TEXT, mention_offset INTEGER DEFAULT 0,
             confidence REAL DEFAULT 0.85);"""
    )
    import json
    rows = [
        ("wrong-a", "synthesis", "Wrong A", "bad root",
         json.dumps({"authority": "canonical"})),  # NO wrong verdict
        ("dep-b", "synthesis", "Dependent B", "downstream",
         json.dumps({"authority": "canonical"})),
    ]
    conn.executemany(
        "INSERT INTO memory_items (id,type,title,content,importance,confidence,"
        "valid_from,created_at,updated_at,metadata_json) VALUES "
        "(?,?,?,?,0.9,0.8,'2026-01-01','2026-01-01','2026-01-02',?)",
        [(r[0], r[1], r[2], r[3], r[4]) for r in rows],
    )
    conn.execute("INSERT INTO memory_relationships (from_id,to_id,relationship_type) "
                 "VALUES ('dep-b','wrong-a','consolidates')")
    conn.commit()
    return conn


def test_clean_store_says_blast_radius_zero_not_absent():
    """§3: the section is always present (count 0), so its absence never reads as
    'not checked'."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """CREATE TABLE memory_items (id TEXT PRIMARY KEY, type TEXT, title TEXT,
             content TEXT, importance REAL DEFAULT 0.5, confidence REAL,
             valid_from TEXT, valid_to TEXT, pinned INTEGER DEFAULT 0,
             is_deleted INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT,
             metadata_json TEXT);
           CREATE TABLE memory_relationships (from_id TEXT, to_id TEXT,
             relationship_type TEXT, created_at TEXT);
           CREATE TABLE memory_item_entities (memory_id TEXT, entity_id TEXT,
             mention_text TEXT, mention_offset INTEGER DEFAULT 0,
             confidence REAL DEFAULT 0.85);"""
    )
    conn.execute("INSERT INTO memory_items (id,type,title,content,importance,"
                 "confidence,valid_from,created_at,updated_at,metadata_json) VALUES "
                 "('b','belief','B','x',0.9,0.8,'2026-01-01','2026-01-01',"
                 "'2026-01-02','{}')")
    conn.commit()
    try:
        pages = build_wiki(conn, None, WikiOptions(importance_threshold=0.6,
                                                   use_networkx=False))
    finally:
        conn.close()
    assert "## Blast radius (0)" in pages["lint.md"]
