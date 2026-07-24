"""Commit 3: the authority render gate (S6).

A compiled synthesis renders its content as body prose ONLY when its authority
is in the configured body set AND it is not GDPR-restricted. Otherwise the page
shows a marker and withholds the content — a provisional / unknown / restricted
synthesis must never read as authoritative.

The gate is exercised end-to-end through build_wiki (metadata_json threaded
select -> Mem -> render), and the policy resolver is unit-tested directly.
"""
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import authority as A  # noqa: E402
from wiki.build import WikiOptions, build_wiki  # noqa: E402

# ── policy resolver (unit) ───────────────────────────────────────────────────

def test_canonical_renders_as_body():
    assert A.renders_as_body({"authority": "canonical"}) is True


def test_provisional_does_not_render_as_body():
    assert A.renders_as_body({"authority": "provisional"}) is False


def test_unknown_authority_fails_closed():
    # A typo or org-specific value not in the body set must NOT render as body.
    assert A.renders_as_body({"authority": "cannonical"}) is False
    assert A.renders_as_body({"authority": "peer-reviewed"}) is False


def test_absent_or_malformed_authority_fails_closed():
    assert A.renders_as_body({}) is False
    assert A.renders_as_body({"authority": None}) is False
    assert A.renders_as_body({"authority": 42}) is False


def test_restricted_overrides_even_canonical():
    # restricted is a legal-hold dimension, not an editorial one: a canonical
    # page still stops rendering when restricted.
    assert A.renders_as_body({"authority": "canonical", "restricted": True}) is False
    assert A.renders_as_body(
        {"authority": "canonical", "review": {"restricted": True}}) is False


def test_marker_names_the_reason():
    assert A.render_marker({"authority": "canonical"}) == ""  # renders → no marker
    assert "restricted" in A.render_marker({"restricted": True}).lower()
    assert "provisional" in A.render_marker({"authority": "provisional"}).lower()
    assert A.render_marker({}) != ""  # unknown still gets a withholding marker


def test_body_authorities_config_override(tmp_path, monkeypatch):
    """An org can widen the body set via a config file — no code edit, no fork."""
    A._CACHE.clear(); A._WARNED.clear()
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / ".wiki_authority.json").write_text(
        json.dumps({"body_authorities": ["canonical", "peer-reviewed"]}))
    monkeypatch.setenv("M3_CONFIG_ROOT", str(cfg))
    A._CACHE.clear()  # force re-read under the new root
    assert A.renders_as_body({"authority": "peer-reviewed"}) is True
    assert A.renders_as_body({"authority": "provisional"}) is False  # still gated
    A._CACHE.clear()


def test_malformed_config_falls_back_to_default(tmp_path, monkeypatch):
    A._CACHE.clear(); A._WARNED.clear()
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / ".wiki_authority.json").write_text("{ this is not json")
    monkeypatch.setenv("M3_CONFIG_ROOT", str(cfg))
    A._CACHE.clear()
    # Falls back to the default set, does not raise.
    assert A.renders_as_body({"authority": "canonical"}) is True
    assert A.renders_as_body({"authority": "provisional"}) is False
    A._CACHE.clear()


# ── end-to-end through build_wiki ────────────────────────────────────────────

def _mem_db_with_authority():
    """A minimal store where two synthesis rows differ only in authority."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY, type TEXT, title TEXT, content TEXT,
            importance REAL DEFAULT 0.5, confidence REAL, valid_from TEXT,
            valid_to TEXT, pinned INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0,
            created_at TEXT, updated_at TEXT, metadata_json TEXT
        );
        CREATE TABLE memory_relationships (
            from_id TEXT, to_id TEXT, relationship_type TEXT, created_at TEXT
        );
        CREATE TABLE memory_item_entities (
            memory_id TEXT, entity_id TEXT, mention_text TEXT,
            mention_offset INTEGER DEFAULT 0, confidence REAL DEFAULT 0.85
        );
        """
    )
    # Each synthesis is bound (via consolidates) to a belief anchor so it forms a
    # real TOPIC page — orphan pages render title-only and would withhold content
    # regardless, so they wouldn't exercise the gate. The anchor gives the body
    # loop something to run over.
    rows = [
        ("a-can", "belief", "Canon Anchor", "anchor one", "{}"),
        ("a-prov", "belief", "Prov Anchor", "anchor two", "{}"),
        ("a-rest", "belief", "Rest Anchor", "anchor three", "{}"),
        ("s-can", "synthesis", "Canon Topic", "CANON BODY VISIBLE",
         json.dumps({"authority": "canonical", "synthesis_kind": "compiled"})),
        ("s-prov", "synthesis", "Prov Topic", "PROVISIONAL BODY HIDDEN",
         json.dumps({"authority": "provisional", "synthesis_kind": "compiled"})),
        ("s-rest", "synthesis", "Restricted Topic", "RESTRICTED BODY HIDDEN",
         json.dumps({"authority": "canonical", "restricted": True})),
    ]
    conn.executemany(
        "INSERT INTO memory_items (id,type,title,content,importance,confidence,"
        "pinned,valid_from,created_at,updated_at,metadata_json) "
        "VALUES (?,?,?,?,0.9,0.8,0,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], "2026-01-01T00:00:00+00:00",
          "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", r[4])
         for r in rows],
    )
    conn.executemany(
        "INSERT INTO memory_relationships (from_id,to_id,relationship_type) "
        "VALUES (?,?,'consolidates')",
        [("s-can", "a-can"), ("s-prov", "a-prov"), ("s-rest", "a-rest")],
    )
    conn.commit()
    return conn


def test_gate_end_to_end():
    conn = _mem_db_with_authority()
    try:
        pages = build_wiki(conn, None, WikiOptions(importance_threshold=0.6,
                                                   use_networkx=False,
                                                   admission_gate=False))
    finally:
        conn.close()
    body = "\n".join(pages.values())
    # Canonical content is present; provisional and restricted are withheld.
    assert "CANON BODY VISIBLE" in body
    assert "PROVISIONAL BODY HIDDEN" not in body, "provisional content leaked as body"
    assert "RESTRICTED BODY HIDDEN" not in body, "restricted content leaked as body"
    # ...but the titles still appear (the pages exist, only content is gated),
    # and a withholding marker is shown.
    assert "Prov Topic" in body and "Restricted Topic" in body
    assert "restricted" in body.lower()


def test_restricted_orphan_shows_marker_not_silent():
    """A restricted synthesis with no edges lands on the orphans page. Orphans
    are title-only so content can't leak, but the legal-hold status must still be
    visible — a silent plain row would hide the GDPR flag from a reviewer."""
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
    conn.execute(
        "INSERT INTO memory_items (id,type,title,content,importance,confidence,"
        "valid_from,created_at,updated_at,metadata_json) VALUES "
        "('o','synthesis','Orphan Restricted','SECRET ORPHAN',0.9,0.8,"
        "'2026-01-01','2026-01-01','2026-01-02',?)",
        (json.dumps({"authority": "canonical", "restricted": True}),),
    )
    conn.commit()
    try:
        pages = build_wiki(conn, None, WikiOptions(importance_threshold=0.6,
                                                   use_networkx=False,
                                                   admission_gate=False))
    finally:
        conn.close()
    orph = pages["topics/orphans.md"]
    assert "SECRET ORPHAN" not in orph
    assert "restricted" in orph.lower()
    # The marker sits on the row, not just in the page boilerplate.
    assert "🔒" in orph


def test_canonical_synthesis_renders_as_topic_BODY_above_members():
    """The point of compile-at-ingest: a topic with a canonical synthesis reads
    as a coherent page — its compiled prose is the BODY, above the member list,
    not merely one bullet within it. (Found during live review 2026-07-24: the
    prose was stored but only appeared as a member bullet.)"""
    conn = _mem_db_with_authority()
    try:
        pages = build_wiki(conn, None, WikiOptions(importance_threshold=0.6,
                                                   use_networkx=False,
                                                   admission_gate=False))
    finally:
        conn.close()
    canon = [t for p, t in pages.items()
             if p.startswith("topics/") and "Canon Topic" in t][0]
    before_members = canon.split("## Members")[0]
    assert "CANON BODY VISIBLE" in before_members, \
        "canonical synthesis prose must render as the topic body, above Members"


def test_gate_is_deterministic():
    """Two builds are byte-identical with the gate active (no config present)."""
    h = []
    for _ in range(3):
        conn = _mem_db_with_authority()
        try:
            pages = build_wiki(conn, None, WikiOptions(importance_threshold=0.6,
                                                       use_networkx=False,
                                                       admission_gate=False))
        finally:
            conn.close()
        h.append("".join(f"{k}\x00{pages[k]}" for k in sorted(pages)))
    assert len(set(h)) == 1, "gate broke determinism"
