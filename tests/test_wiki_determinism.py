"""Determinism + correctness tests for the wiki generator (bin/wiki/).

Drives the PURE builder (`wiki.build.build_wiki`) from tiny in-memory fixture DBs
so there's no dependency on the real store, no embedder, and no flake surface. The
core guarantee — same DB in → byte-identical vault out — is asserted by building
twice and comparing every page.
"""
import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki.build import WikiOptions, build_wiki  # noqa: E402


def _mem_db() -> sqlite3.Connection:
    """A minimal agent_memory.db with the columns the generator reads."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY, type TEXT, title TEXT, content TEXT,
            importance REAL DEFAULT 0.5, confidence REAL, valid_from TEXT,
            valid_to TEXT, pinned INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE memory_relationships (
            from_id TEXT, to_id TEXT, relationship_type TEXT, created_at TEXT
        );
        """
    )
    rows = [
        # id,      type,        title,            content,       imp, conf, pin
        ("m-aaa", "belief",    "Alpha Root",     "Alpha body",  0.9, 0.91, 1),
        ("m-bbb", "belief",    "Alpha Detail",   "Beta body",   0.8, 0.80, 0),
        ("m-ccc", "procedure", "Alpha Runbook",  "Gamma body",  0.7, None, 0),
        ("m-ddd", "reference", "Beta Standalone","Delta body",  0.65, 0.5, 0),
        ("m-eee", "belief",    "Contra One",     "Claim X",     0.85, 0.7, 0),
        ("m-fff", "belief",    "Contra Two",     "Not X",       0.85, 0.6, 0),
        # Below the importance threshold and not a core type → excluded.
        ("m-zzz", "note",      "Ignored",        "noise",       0.1, None, 0),
    ]
    conn.executemany(
        "INSERT INTO memory_items (id,type,title,content,importance,confidence,pinned,"
        "valid_from,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], r[4], r[5], r[6],
          "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
          "2026-01-02T00:00:00+00:00") for r in rows],
    )
    edges = [
        ("m-aaa", "m-bbb", "consolidates"),   # binds alpha cluster
        ("m-aaa", "m-ccc", "related"),        # binds runbook into alpha
        ("m-eee", "m-fff", "contradicts"),    # co-locate + flag
        # m-ddd has no edges → orphan.
    ]
    conn.executemany(
        "INSERT INTO memory_relationships (from_id,to_id,relationship_type) VALUES (?,?,?)",
        edges,
    )
    conn.commit()
    return conn


def _files_db() -> sqlite3.Connection:
    """A minimal files_database.db with one file_node + fact + promotion_marker."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE file_nodes (
            uuid TEXT PRIMARY KEY, filename TEXT, filetype TEXT, path_absolute TEXT,
            file_summary TEXT, corpus_id TEXT, superseded_by TEXT
        );
        CREATE TABLE leaves (
            uuid TEXT PRIMARY KEY, file_node TEXT, text TEXT, leaf_summary TEXT,
            division_label TEXT, superseded_by TEXT
        );
        CREATE TABLE facts (
            uuid TEXT PRIMARY KEY, file_node TEXT, leaf TEXT, statement TEXT,
            confidence REAL, superseded_by TEXT
        );
        CREATE TABLE promotion_markers (
            uuid TEXT PRIMARY KEY, source_memory TEXT, source_memory_type TEXT,
            promoted_to TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO file_nodes (uuid,filename,filetype,path_absolute,file_summary,corpus_id) "
        "VALUES (?,?,?,?,?,?)",
        ("fn-1", "design.md", "markdown", "/docs/design.md",
         "The design doc summary.", "default"),
    )
    conn.execute(
        "INSERT INTO leaves (uuid,file_node,text,division_label) VALUES (?,?,?,?)",
        ("lf-1", "fn-1", "Alpha is the root concept.", "intro"),
    )
    conn.execute(
        "INSERT INTO facts (uuid,file_node,leaf,statement,confidence) VALUES (?,?,?,?,?)",
        ("ft-1", "fn-1", "lf-1", "Alpha is the root concept.", 0.95),
    )
    # Bridge: fact ft-1 was promoted into memory m-aaa.
    conn.execute(
        "INSERT INTO promotion_markers (uuid,source_memory,source_memory_type,promoted_to) "
        "VALUES (?,?,?,?)",
        ("pm-1", "ft-1", "fact", "m-aaa"),
    )
    conn.commit()
    return conn


def _build(use_networkx: bool) -> dict:
    mem, files = _mem_db(), _files_db()
    try:
        return build_wiki(mem, files, WikiOptions(importance_threshold=0.6,
                                                  use_networkx=use_networkx))
    finally:
        mem.close()
        files.close()


@pytest.mark.parametrize("use_networkx", [False, True])
def test_deterministic(use_networkx):
    """Same DB in → byte-identical vault out, across two independent builds."""
    a = _build(use_networkx)
    b = _build(use_networkx)
    assert a.keys() == b.keys()
    for k in a:
        assert a[k] == b[k], f"page {k} differs between builds"


def test_core_pages_emitted():
    vault = _build(use_networkx=False)
    assert "index.md" in vault
    assert "overview.md" in vault
    assert "lint.md" in vault
    # The excluded low-importance note must not appear anywhere.
    blob = "\n".join(vault.values())
    assert "Ignored" not in blob
    assert "m-zzz" not in blob


def test_cluster_and_wikilinks():
    vault = _build(use_networkx=False)
    # Alpha Root/Detail/Runbook cluster together on one topic page.
    alpha = [t for p, t in vault.items()
             if p.startswith("topics/") and "Alpha Root" in t]
    assert len(alpha) == 1
    page = alpha[0]
    assert "m-aaa" in page and "m-bbb" in page and "m-ccc" in page
    # Beta Standalone is an orphan (no edges) → appendix, not its own topic.
    assert "topics/orphans.md" in vault
    assert "Beta Standalone" in vault["topics/orphans.md"]


def test_contradiction_flagged():
    vault = _build(use_networkx=False)
    # The contradicting pair lands together AND is reported in lint.
    lint = vault["lint.md"]
    assert "Contradictions (1)" in lint
    # Both members appear on one topic page with the warning.
    contra = [t for p, t in vault.items()
              if p.startswith("topics/") and "Contra One" in t]
    assert len(contra) == 1
    assert "Contra Two" in contra[0]
    assert "Contradiction on this page" in contra[0]


def test_evidence_links_to_source():
    vault = _build(use_networkx=False)
    # m-aaa was promoted from design.md → its topic shows an Evidence link,
    # and a sources/ page exists for the file.
    alpha = [t for p, t in vault.items()
             if p.startswith("topics/") and "Alpha Root" in t][0]
    assert "## Evidence" in alpha
    assert "design.md" in alpha
    src = [t for p, t in vault.items() if p.startswith("sources/")]
    assert len(src) == 1
    assert "The design doc summary." in src[0]


def test_memory_only_when_no_files():
    mem = _mem_db()
    try:
        vault = build_wiki(mem, None, WikiOptions(importance_threshold=0.6,
                                                  include_files=False))
    finally:
        mem.close()
    assert not any(p.startswith("sources/") for p in vault)
    # Topic pages still render; just no Evidence section.
    assert any(p.startswith("topics/") for p in vault)
