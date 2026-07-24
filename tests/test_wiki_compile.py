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
