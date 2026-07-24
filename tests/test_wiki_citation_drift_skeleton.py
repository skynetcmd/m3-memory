"""Commit 4b SKELETON: citation-drift plumbing (off by default, provably inert).

The judge PROMPT, the labelled FIXTURE, and the recall THRESHOLD are DEFERRED
(plan §5). What ships is the plumbing — provenance-source pairing, report shape,
fail-open, report-only — proven with a stub judge, plus the inertness guarantee:
the default (no-judge) build is byte-identical to one without this module.
"""
import os
import sqlite3
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import citation_drift as CD  # noqa: E402
from wiki.build import WikiOptions, build_wiki  # noqa: E402
from wiki.cluster import Cluster  # noqa: E402
from wiki.select import Edge, Mem  # noqa: E402


def _syn(id, content="claim"):
    return Mem(id=id, type="synthesis", title=id, content=content, importance=0.5,
               confidence=0.5, valid_from=None, valid_to=None, pinned=0,
               created_at=None, updated_at=None, metadata={})


def _src(id, content="source"):
    return Mem(id=id, type="belief", title=id, content=content, importance=0.5,
               confidence=0.5, valid_from=None, valid_to=None, pinned=0,
               created_at=None, updated_at=None, metadata={})


class _StubJudge:
    """Deterministic judge stand-in — no model. Returns a fixed verdict, and
    records what it was asked, so pairing can be asserted."""

    def __init__(self, drifted=False, raises=False, abstain=False):
        self._drifted = None if abstain else drifted
        self._raises = raises
        self.calls = []

    def judge(self, synthesis_content, source_contents):
        self.calls.append((synthesis_content, tuple(source_contents)))
        if self._raises:
            raise RuntimeError("judge boom")
        return CD.DriftVerdict(synthesis_id="?", drifted=self._drifted,
                               reason="stub")


# ── inertness: the whole point of a skeleton ─────────────────────────────────

def test_no_judge_returns_none():
    clusters = [Cluster(key="a", members=[_syn("a")])]
    assert CD.check_drift(clusters, [], judge=None) is None


def test_no_judge_renders_nothing():
    assert CD.render_section(None, links=None, self_path="lint.md") == []


def test_default_build_omits_the_section_entirely():
    """A build with no judge must not contain the drift section at all."""
    conn = _db()
    try:
        pages = build_wiki(conn, None, WikiOptions(importance_threshold=0.6,
                                                   use_networkx=False))
    finally:
        conn.close()
    assert "Citation drift" not in pages["lint.md"]


def test_default_build_is_byte_identical_with_and_without_the_param():
    """Passing drift_judge=None must produce the exact same bytes as omitting it
    — the skeleton is provably inert until a judge is injected."""
    def build(pass_param):
        conn = _db()
        try:
            if pass_param:
                return build_wiki(conn, None,
                                  WikiOptions(importance_threshold=0.6,
                                              use_networkx=False),
                                  drift_judge=None)
            return build_wiki(conn, None,
                              WikiOptions(importance_threshold=0.6,
                                          use_networkx=False))
        finally:
            conn.close()
    a, b = build(True), build(False)
    assert a == b


# ── plumbing (with a stub judge) ─────────────────────────────────────────────

def test_pairs_synthesis_with_its_provenance_sources():
    syn, src1, src2 = _syn("s", "the claim"), _src("x", "src one"), _src("y", "src two")
    clusters = [Cluster(key="s", members=[syn, src1, src2])]
    edges = [Edge("s", "x", "consolidates"), Edge("s", "y", "distills_from")]
    judge = _StubJudge(drifted=True)
    report = CD.check_drift(clusters, edges, judge)
    assert report.checked == 1
    # The judge saw the synthesis content and BOTH source contents.
    (seen_syn, seen_srcs), = judge.calls
    assert seen_syn == "the claim"
    assert set(seen_srcs) == {"src one", "src two"}
    assert len(report.drifted) == 1


def test_synthesis_with_no_sources_is_skipped_and_counted():
    """Coverage is reported (§3): a sourceless synthesis is not silently ignored."""
    clusters = [Cluster(key="s", members=[_syn("s")])]
    report = CD.check_drift(clusters, [], _StubJudge())
    assert report.checked == 0
    assert report.skipped_no_sources == 1


def test_judge_error_is_fail_open_not_a_drift():
    syn, src = _syn("s"), _src("x")
    clusters = [Cluster(key="s", members=[syn, src])]
    edges = [Edge("s", "x", "consolidates")]
    report = CD.check_drift(clusters, edges, _StubJudge(raises=True))
    assert report.abstained == 1
    assert report.drifted == [], "a judge crash must never manufacture a drift"


def test_abstain_is_not_a_drift():
    syn, src = _syn("s"), _src("x")
    clusters = [Cluster(key="s", members=[syn, src])]
    edges = [Edge("s", "x", "consolidates")]
    report = CD.check_drift(clusters, edges, _StubJudge(abstain=True))
    assert report.abstained == 1
    assert report.drifted == []


def test_supersedes_is_not_a_source():
    """Only provenance edges pair a synthesis with sources — a supersede does not."""
    syn, other = _syn("s"), _src("x")
    clusters = [Cluster(key="s", members=[syn, other])]
    edges = [Edge("s", "x", "supersedes")]
    report = CD.check_drift(clusters, edges, _StubJudge(drifted=True))
    assert report.skipped_no_sources == 1  # no provenance source ⇒ nothing to judge


def test_report_only_never_mutates_a_row():
    """The check must not write verdict / is_deleted / anything onto a member."""
    syn, src = _syn("s", "claim"), _src("x", "source")
    clusters = [Cluster(key="s", members=[syn, src])]
    edges = [Edge("s", "x", "consolidates")]
    CD.check_drift(clusters, edges, _StubJudge(drifted=True))
    assert syn.metadata == {}, "check_drift mutated the synthesis metadata"


# ── render section (with a report) ───────────────────────────────────────────

def test_section_reports_coverage_even_at_zero_findings():
    report = CD.DriftReport(checked=3, skipped_no_sources=1, abstained=0)
    lines = CD.render_section(report, links=None, self_path="lint.md")
    text = "\n".join(lines)
    assert "## Citation drift (0)" in text
    assert "Checked 3" in text  # coverage shown, not silence


def test_section_lists_drifted_syntheses():
    report = CD.DriftReport(checked=1)
    report.findings.append(CD.DriftVerdict("syn12345", drifted=True, reason="scope changed"))
    lines = CD.render_section(report, links=None, self_path="lint.md")
    text = "\n".join(lines)
    assert "## Citation drift (1)" in text
    assert "syn12345"[:8] in text
    assert "scope changed" in text
    assert "flag, not an automatic change" in text  # report-only framing


def _db():
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
    conn.executescript(
        "INSERT INTO memory_items (id,type,title,content,importance,confidence,"
        "valid_from,created_at,updated_at,metadata_json) VALUES "
        "('s','synthesis','S','claim',0.9,0.8,'2026-01-01','2026-01-01',"
        "'2026-01-02','{\"authority\": \"canonical\"}'),"
        "('x','belief','X','source',0.9,0.8,'2026-01-01','2026-01-01',"
        "'2026-01-02','{}');"
        "INSERT INTO memory_relationships (from_id,to_id,relationship_type) "
        "VALUES ('s','x','consolidates');"
    )
    conn.commit()
    return conn
