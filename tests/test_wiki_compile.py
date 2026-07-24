"""Compile writer (bin/wiki/compile.py).

Pure helpers are tested directly. The async write path is driven with a STUB
compiler and stub persistence callables, so the idempotence merge-gate, the
supersede-always rule, min-confidence, and prompt dedupe are all proven with no
model and no DB — mirroring how test_wiki_determinism drives synth with _StubSynth.
"""
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import compile as WC  # noqa: E402
from wiki.cluster import Cluster  # noqa: E402
from wiki.select import Mem  # noqa: E402
from wiki.synth import _cluster_hash  # noqa: E402


def _mem(id, conf=None, imp=0.5, title="M"):
    return Mem(id=id, type="synthesis", title=title, content=f"body {id}",
               importance=imp, confidence=conf, valid_from=None, valid_to=None,
               pinned=0, created_at=None, updated_at=None)


def _cluster(*mems):
    ms = list(mems)
    return Cluster(key=min(m.id for m in ms), members=ms)


# ── min_member_confidence ────────────────────────────────────────────────────

def test_min_confidence_is_the_weakest_source():
    c = _cluster(_mem("a", 0.9), _mem("b", 0.4), _mem("c", 0.7))
    assert WC.min_member_confidence(c) == 0.4


def test_min_confidence_excludes_nulls():
    # NULL members are ignored; the min is over the present values.
    c = _cluster(_mem("a", None), _mem("b", 0.6), _mem("c", None))
    assert WC.min_member_confidence(c) == 0.6


def test_min_confidence_all_null_falls_back_to_documented_constant():
    c = _cluster(_mem("a", None), _mem("b", None))
    assert WC.min_member_confidence(c) == WC._NO_MEMBER_CONFIDENCE


def test_min_confidence_never_promotes_to_one():
    # Even an all-1.0 cluster: the rule is a floor, and the fallback stays < 1.0.
    c = _cluster(_mem("a", None))
    assert WC.min_member_confidence(c) < 1.0


# ── metadata shape ───────────────────────────────────────────────────────────

class _StubCompiler:
    prompt_version = "1"
    model = "stub-model"

    def __init__(self, prose="COMPILED PROSE", prompt="THE PROMPT"):
        self._prose, self._prompt = prose, prompt
        self.calls = 0

    def prompt_text(self):
        return self._prompt

    def compile(self, cluster):
        self.calls += 1
        return self._prose


def test_metadata_carries_provenance_and_manifest():
    c = _cluster(_mem("a", 0.8), _mem("b", 0.9))
    meta = WC.build_metadata(c, _StubCompiler(), "prompt-123")
    assert meta["synthesis_kind"] == "compiled"
    assert meta["authority"] == "provisional"  # nothing canonical until promoted
    comp = meta["compiler"]
    assert comp["prompt_version"] == "1"
    assert comp["model"] == "stub-model"
    assert comp["prompt_memory_id"] == "prompt-123"
    assert comp["member_ids"] == ["a", "b"]      # the source manifest, exact
    assert comp["member_count"] == 2
    assert comp["cluster_hash"] == _cluster_hash(c, "stub-model")
    assert meta["verdict"] is None


# ── cluster hash idempotence across cold/warm re-clustering ──────────────────

def _src(id, title="M"):
    return Mem(id=id, type="belief", title=title, content=f"body {id}",
               importance=0.5, confidence=0.7, valid_from=None, valid_to=None,
               pinned=0, created_at=None, updated_at=None)


def _syn(id, title="M"):
    return Mem(id=id, type="synthesis", title=title, content=f"prose {id}",
               importance=0.8, confidence=0.6, valid_from=None, valid_to=None,
               pinned=0, created_at=None, updated_at=None)


def test_cluster_hash_ignores_synthesis_members():
    """Idempotence guard: once compiled, a synthesis re-clusters WITH its sources
    (via consolidates edges) on the next run. The hash must exclude it, or it
    changes every run and the no-op gate never fires (duplicate syntheses pile up).
    Cold cluster (sources only) and warm cluster (sources + synthesis) must hash
    identically."""
    cold = _cluster(_src("a"), _src("b"), _src("c"))
    warm = _cluster(_src("a"), _src("b"), _src("c"), _syn("syn-1"))
    assert _cluster_hash(cold, "m") == _cluster_hash(warm, "m")


def test_cluster_hash_still_tracks_source_changes():
    """The exclusion must not make the hash blind to REAL changes: a different
    source member set still yields a different hash."""
    a = _cluster(_src("a"), _src("b"))
    b = _cluster(_src("a"), _src("c"))
    assert _cluster_hash(a, "m") != _cluster_hash(b, "m")


# ── the idempotent write rule (the merge gate) ───────────────────────────────

def _head_for(cluster, compiler, prose):
    return WC.ExistingSynthesis(
        id="head-1",
        cluster_hash=_cluster_hash(cluster, compiler.model),
        prose_hash=WC.prose_hash(prose),
    )


async def _run(cluster, compiler, head, **kw):
    """Drive compile_cluster with stub persistence; capture what it would write."""
    writes, supersedes = [], []

    async def _write(**a):
        writes.append(a)
        return "Created: new-syn-id"

    async def _supersede(**a):
        supersedes.append(a)
        return "Superseded: new-syn-id"

    stats = WC.CompileStats()
    r = await WC.compile_cluster(
        cluster, compiler, head, "prompt-1", stats,
        _write=_write, _supersede=_supersede, **kw)
    return r, writes, supersedes, stats


@pytest.mark.asyncio
async def test_prompt_is_deduped_by_exact_title_lookup():
    """Regression (found live 2026-07-24): a re-run with an unchanged prompt must
    REUSE the existing row, not write a second. The dedup is an EXACT-title
    lookup, not a semantic search — semantic search returned the wrong shape and
    matched everything, so every run wrote a duplicate prompt.
    """
    writes = []

    async def _write(**a):
        writes.append(a)
        return "Created: prompt-new"

    # First call: lookup finds nothing → writes the prompt.
    stats1 = WC.CompileStats()
    store = {}  # title -> id, our stand-in for the DB

    def _lookup(title):
        return store.get(title)

    async def _write_and_record(**a):
        writes.append(a)
        new_id = "prompt-1"
        store[a["title"]] = new_id  # persist so the next lookup finds it
        return f"Created: {new_id}"

    pid1 = await WC._ensure_prompt_row(_StubCompiler(), stats1,
                                       scope="agent", user_id="",
                                       _write=_write_and_record, _lookup=_lookup)
    assert stats1.prompts_written == 1 and stats1.prompts_reused == 0
    assert pid1 == "prompt-1"

    # Second call, same compiler (same prompt text) → lookup HITS, no new write.
    stats2 = WC.CompileStats()
    pid2 = await WC._ensure_prompt_row(_StubCompiler(), stats2,
                                       scope="agent", user_id="",
                                       _write=_write_and_record, _lookup=_lookup)
    assert stats2.prompts_written == 0, "wrote a duplicate prompt on re-run"
    assert stats2.prompts_reused == 1
    assert pid2 == "prompt-1", "did not reuse the existing prompt id"


@pytest.mark.asyncio
async def test_unchanged_cluster_writes_nothing_and_does_not_call_model():
    """THE idempotence gate: identical inputs → 0 rows, 0 model calls."""
    c = _cluster(_mem("a", 0.8))
    comp = _StubCompiler()
    head = _head_for(c, comp, "COMPILED PROSE")
    r, writes, sups, stats = await _run(c, comp, head)
    assert r.action == "skipped-hash"
    assert writes == [] and sups == []
    assert comp.calls == 0, "the model must NOT be called when the hash matches"
    assert stats.skipped_hash == 1


@pytest.mark.asyncio
async def test_identical_prose_after_change_writes_nothing():
    """Inputs changed (hash differs) but prose came back byte-identical → skip."""
    c = _cluster(_mem("a", 0.8))
    comp = _StubCompiler(prose="SAME")
    # Head has a DIFFERENT recorded cluster_hash (inputs moved) but same prose hash.
    head = WC.ExistingSynthesis(id="h", cluster_hash="stale-hash",
                                prose_hash=WC.prose_hash("SAME"))
    r, writes, sups, stats = await _run(c, comp, head)
    assert r.action == "skipped-identical"
    assert writes == [] and sups == []
    assert comp.calls == 1  # it DID compile, then found nothing new
    assert stats.skipped_identical == 1


@pytest.mark.asyncio
async def test_first_ever_compile_creates():
    c = _cluster(_mem("a", 0.8), _mem("b", 0.4))
    comp = _StubCompiler(prose="FRESH")
    r, writes, sups, stats = await _run(c, comp, head=None)
    assert r.action == "created"
    assert len(writes) == 1 and sups == []
    w = writes[0]
    assert w["type"] == "synthesis"
    assert w["content"] == "FRESH"
    assert w["confidence"] == 0.4              # MIN of members
    assert w["embed"] is True
    assert w["embed_text"].endswith("FRESH")   # title + prose in-vector (S4)
    assert stats.created == 1


@pytest.mark.asyncio
async def test_changed_prose_supersedes_never_updates_in_place():
    """The rule that makes a wrong synthesis auditable: supersede, don't mutate."""
    c = _cluster(_mem("a", 0.8))
    comp = _StubCompiler(prose="REVISED")
    head = WC.ExistingSynthesis(id="old-id", cluster_hash="stale",
                                prose_hash=WC.prose_hash("ORIGINAL"))
    r, writes, sups, stats = await _run(c, comp, head)
    assert r.action == "superseded"
    assert writes == [] and len(sups) == 1
    assert sups[0]["old_id"] == "old-id"       # targets the specific head
    assert sups[0]["content"] == "REVISED"
    assert r.superseded_id == "old-id"
    assert stats.superseded == 1


@pytest.mark.asyncio
async def test_created_synthesis_writes_consolidates_edges_to_every_member():
    """Regression (found live 2026-07-24): the writer must create a `consolidates`
    edge from the synthesis to EACH source member. Without them the synthesis is a
    graph orphan — it clusters apart from its sources, its prose never reaches the
    source topic, and blast-radius / citation-drift (which walk these edges) see
    nothing. metadata.member_ids is the manifest; these edges are the graph."""
    c = _cluster(_mem("a", 0.8), _mem("b", 0.6), _mem("c", 0.9))
    comp = _StubCompiler(prose="P")
    links = []

    async def _write(**a):
        return "Created: syn-1"

    def _link(frm, to, rel):
        links.append((frm, to, rel))
        return "linked"

    stats = WC.CompileStats()
    r = await WC.compile_cluster(c, comp, head=None, prompt_memory_id="p", stats=stats,
                                 _write=_write, _link=_link)
    assert r.action == "created"
    # One consolidates edge from the new synthesis to each of the 3 members.
    assert {(t, rel) for _f, t, rel in links} == {
        ("a", "consolidates"), ("b", "consolidates"), ("c", "consolidates")}
    assert all(f == "syn-1" for f, _t, _rel in links), "edges must originate at the synthesis"


@pytest.mark.asyncio
async def test_synthesis_never_self_links():
    """If the synthesis id somehow matches a member, no self-edge is written."""
    c = _cluster(_mem("syn-1", 0.8))
    links = []

    async def _write(**a):
        return "Created: syn-1"

    def _link(frm, to, rel):
        links.append((frm, to, rel))

    stats = WC.CompileStats()
    await WC.compile_cluster(c, _StubCompiler(), head=None, prompt_memory_id="p",
                             stats=stats, _write=_write, _link=_link)
    assert links == [], "must not self-link the synthesis to itself"


@pytest.mark.asyncio
async def test_compile_failure_is_fail_open():
    """A compiler returning None must not raise or write — the batch continues."""
    class _Broken(_StubCompiler):
        def compile(self, cluster):
            return None
    c = _cluster(_mem("a", 0.8))
    r, writes, sups, stats = await _run(c, _Broken(), head=None)
    assert r.action == "compile-failed"
    assert writes == [] and sups == []
    assert stats.failed == 1


# ── idempotence across a FULL rebuild (the plan's headline metric) ───────────

@pytest.mark.asyncio
async def test_two_consecutive_compiles_of_an_unchanged_store_write_zero_rows():
    """Rebuild twice over an unchanged cluster set → exactly 0 new rows on run 2.

    This is the merge gate: an unbounded supersede chain makes `contradicts`
    edges useless, so the second identical pass must be a pure no-op.
    """
    clusters = [_cluster(_mem("a", 0.9), _mem("b", 0.5)),
                _cluster(_mem("c", 0.7))]
    comp = _StubCompiler(prose="P")

    # Run 1: no heads → all created. Build heads from what was written.
    heads = {}
    total_writes_run1 = 0
    for cl in clusters:
        r, writes, sups, stats = await _run(cl, comp, heads.get(cl.key))
        total_writes_run1 += len(writes) + len(sups)
        heads[cl.key] = WC.ExistingSynthesis(
            id=r.synthesis_id, cluster_hash=_cluster_hash(cl, comp.model),
            prose_hash=WC.prose_hash("P"))
    assert total_writes_run1 == len(clusters)

    # Run 2: heads present, nothing changed → ZERO writes, ZERO model calls.
    comp.calls = 0
    total_writes_run2 = 0
    for cl in clusters:
        r, writes, sups, stats = await _run(cl, comp, heads.get(cl.key))
        total_writes_run2 += len(writes) + len(sups)
        assert r.action == "skipped-hash"
    assert total_writes_run2 == 0, "idempotence violated — second pass wrote rows"
    assert comp.calls == 0, "second pass called the model despite unchanged inputs"


# ── ledger integration (G1): compile_clusters records a run + frozen membership ─

def _ledger_db():
    import sqlite3
    _root = os.path.normpath(os.path.join(_HERE, ".."))
    mig = os.path.join(_root, "memory", "migrations",
                       "041_wiki_cluster_run.up.sql")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(mig, encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


class _LedgerDialect:
    """SQLite-flavoured dialect stub for direct ledger reads in assertions."""
    def param(self): return "?"
    def placeholder(self, n): return ",".join(["?"] * n)
    def insert_or_ignore(self): return "INSERT OR IGNORE INTO"
    def on_conflict_ignore(self, *, conflict_target="", index_predicate=""): return ""
    def now(self): return "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


@pytest.mark.asyncio
async def test_compile_clusters_records_ledger_run_and_membership():
    """A full compile pass opens a cluster_run, freezes membership, completes it.

    Uses the REAL dialect factory (which defaults to SqliteDialect with no active
    backend) against an in-memory SQLite DB — no monkeypatching of memory.backends,
    which is fragile (the package re-exports `dialect` as both a submodule and a
    factory attribute, so replacing it poisons every other test in the process)."""
    import wiki.compile as _wc
    import wiki.ledger as _led
    db = _ledger_db()

    clusters = [_cluster(_mem("a", 0.9), _mem("b", 0.5)),
                _cluster(_mem("c", 0.7))]
    comp = _StubCompiler(prose="P")

    writes = []
    async def _write(**a):
        writes.append(a); return "Created: syn-new"
    async def _ensure_prompt(compiler, stats, **kw):
        return "prompt-1"  # skip the memory-seam prompt write

    stats = await _wc.compile_clusters(
        clusters, comp, heads={},
        edges=None, gate=None,  # gating off (no edges) so both clusters compile
        importance_threshold=0.55, entity_comention=True,
        _write=_write, _ensure_prompt=_ensure_prompt, _ledger_db=db)

    assert stats.created == 2
    # A completed run exists with the right params.
    run = db.execute("SELECT * FROM cluster_run WHERE completed_at IS NOT NULL"
                     ).fetchone()
    assert run is not None
    assert run["importance_threshold"] == 0.55
    assert run["entity_comention"] == 1
    assert run["cluster_count"] == 2
    # Membership frozen for both clusters (UUIDs only).
    frozen = _led.frozen_membership(db, _LedgerDialect(), run["id"])
    all_members = {m for c in frozen.values() for m in c["members"]}
    assert all_members == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_compile_clusters_ledger_off_writes_no_run(monkeypatch):
    """record_run=False → no ledger row, compile still works."""
    import wiki.compile as _wc
    db = _ledger_db()
    clusters = [_cluster(_mem("a", 0.9))]
    comp = _StubCompiler(prose="P")

    async def _write(**a): return "Created: syn-new"
    async def _ensure_prompt(compiler, stats, **kw): return "prompt-1"

    await _wc.compile_clusters(
        clusters, comp, heads={}, edges=None, record_run=False,
        _write=_write, _ensure_prompt=_ensure_prompt, _ledger_db=db)
    n = db.execute("SELECT COUNT(*) AS c FROM cluster_run").fetchone()["c"]
    assert n == 0
