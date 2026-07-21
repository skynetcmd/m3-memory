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
        CREATE TABLE memory_item_entities (
            memory_id TEXT, entity_id TEXT, mention_text TEXT,
            mention_offset INTEGER DEFAULT 0, confidence REAL DEFAULT 0.85
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
    # Entity co-mention: m-ddd (an edge-orphan, standalone reference) and m-fff
    # both mention the same specific entity → they should cluster via co-mention
    # alone, with no hand-authored edge between them.
    mie = [
        ("m-ddd", "ent-specific"),
        ("m-fff", "ent-specific"),
    ]
    conn.executemany(
        "INSERT INTO memory_item_entities (memory_id, entity_id) VALUES (?,?)", mie
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


def _build(use_networkx: bool, entity_comention: bool = False) -> dict:
    mem, files = _mem_db(), _files_db()
    try:
        return build_wiki(mem, files, WikiOptions(importance_threshold=0.6,
                                                  use_networkx=use_networkx,
                                                  entity_comention=entity_comention))
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
    # The vault documents itself, and the guide is linked from the index with a
    # REAL Markdown hyperlink (not an Obsidian-only [[wikilink]]).
    assert "about.md" in vault
    assert "(about.md)" in vault["index.md"]
    assert "[[" not in vault["index.md"], "index must use standard Markdown links"
    assert "m3 wiki generate" in vault["about.md"]
    # The excluded low-importance note must not appear anywhere.
    blob = "\n".join(vault.values())
    assert "Ignored" not in blob
    assert "m-zzz" not in blob


def test_uses_real_markdown_links_not_wikilinks():
    """The whole vault must be browsable in any Markdown renderer: standard
    [text](path.md) links only, no Obsidian-only [[wikilinks]]."""
    vault = _build(use_networkx=False, entity_comention=True)
    for path, text in vault.items():
        assert "[[" not in text, f"{path} contains an Obsidian-only [[wikilink]]"
    # A topic page links back up to the index with a correct relative path.
    topic = [t for p, t in vault.items()
             if p.startswith("topics/") and p != "topics/orphans.md"][0]
    assert "(../index.md)" in topic, "topic nav should link up to ../index.md"


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


def test_entity_comention_binds_orphans():
    """With entity co-mention on, two memories sharing a specific entity cluster
    together even with no hand-authored edge — and are NOT left as orphans."""
    without = _build(use_networkx=False, entity_comention=False)
    with_ = _build(use_networkx=False, entity_comention=True)

    # Without co-mention, m-ddd (Beta Standalone) is an orphan.
    assert "Beta Standalone" in without["topics/orphans.md"]

    # With co-mention, m-ddd + m-fff (share ent-specific) land on one topic page,
    # and m-ddd is no longer an orphan.
    orphans = with_.get("topics/orphans.md", "")
    assert "Beta Standalone" not in orphans
    shared = [t for p, t in with_.items()
              if p.startswith("topics/")
              and "m-ddd" in t and "m-fff" in t]
    assert len(shared) == 1, "m-ddd and m-fff should share one topic via co-mention"

    # Determinism must hold with co-mention on, too.
    again = _build(use_networkx=False, entity_comention=True)
    for k in with_:
        assert with_[k] == again[k]


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


def test_html_viewer_self_contained():
    """--html emits a single self-contained document embedding every page, with no
    unescaped </script> breakout and no unfilled template placeholders."""
    import json
    import re as _re
    _bin = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
    if _bin not in sys.path:
        sys.path.insert(0, _bin)
    from wiki.html_view import build_html

    vault = _build(use_networkx=False, entity_comention=True)
    html = build_html(vault)
    assert "__PAGES_JSON__" not in html and "__TITLE__" not in html
    m = _re.search(r'<script id="data" type="application/json">(.*?)</script>',
                   html, _re.S)
    assert m, "embedded data blob missing"
    blob = m.group(1)
    assert "</script>" not in blob, "unescaped </script> would break out of the tag"
    data = json.loads(blob.replace("<\\/", "</"))
    assert set(data.keys()) == set(vault.keys())
    assert html.count("<html") == 1 and html.count("</html>") == 1


class _StubSynth:
    """A Synthesizer stand-in — no network, deterministic prose per cluster."""

    def __init__(self):
        self.seen = []

    def lede_for(self, cluster):
        self.seen.append(cluster.key)
        return f"LEDE for {cluster.members[0].display_title}."


def test_synthesis_injects_lede():
    """When a synthesizer is passed, topic pages carry its prose lede; orphans
    do not get synthesized (no wasted calls)."""
    mem, files = _mem_db(), _files_db()
    stub = _StubSynth()
    try:
        vault = build_wiki(
            mem, files,
            WikiOptions(importance_threshold=0.6, use_networkx=False, entity_comention=True),
            synthesizer=stub,
        )
    finally:
        mem.close()
        files.close()
    topic_pages = [t for p, t in vault.items() if p.startswith("topics/") and p != "topics/orphans.md"]
    assert topic_pages, "expected at least one topic page"
    assert any("LEDE for" in t for t in topic_pages)
    # The pure (no-synth) build must NOT contain a lede — confirms opt-in only.
    mem2, files2 = _mem_db(), _files_db()
    try:
        plain = build_wiki(mem2, files2, WikiOptions(importance_threshold=0.6,
                                                     use_networkx=False))
    finally:
        mem2.close()
        files2.close()
    assert not any("LEDE for" in t for t in plain.values())


def test_synth_cache_roundtrip(tmp_path):
    """The synth cache stores + returns a lede keyed by cluster content-hash, so
    an unchanged cluster is not re-generated."""
    import sys as _sys
    _bin = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
    if _bin not in _sys.path:
        _sys.path.insert(0, _bin)
    from wiki.synth import SynthConfig, Synthesizer
    from wiki.cluster import Cluster
    from wiki.select import Mem

    m = Mem(id="m-1", type="belief", title="T", content="body", importance=0.9,
            confidence=0.8, valid_from=None, valid_to=None, pinned=0,
            created_at=None, updated_at=None)
    c = Cluster(key="m-1", members=[m])

    calls = {"n": 0}

    class OneShot(Synthesizer):
        def __init__(self, cfg):
            super().__init__(cfg)

        def lede_for(self, cluster):
            # Force the model call to a stub the first time; cache on second.
            import wiki.synth as s
            orig = s._call_model
            s._call_model = lambda cfg, prompt: (calls.__setitem__("n", calls["n"] + 1) or "cached prose")
            try:
                return super().lede_for(cluster)
            finally:
                s._call_model = orig

    cfg = SynthConfig(cache_dir=str(tmp_path))
    syn = OneShot(cfg)
    first = syn.lede_for(c)
    second = syn.lede_for(c)
    assert first == "cached prose" and second == "cached prose"
    assert calls["n"] == 1, "second call should hit the on-disk cache, not the model"
