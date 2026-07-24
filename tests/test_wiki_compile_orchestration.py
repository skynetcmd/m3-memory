"""Orchestration for `m3 wiki compile`: load_heads + compile_clusters.

The per-cluster decision (compile_cluster) is tested in test_wiki_compile.py.
Here: title-keyed head resolution, the full-pass loop, prompt-written-once,
and WAL checkpoint cadence — all with stubs, no store, no model.
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


def _mem(id, title="T", conf=0.7, type="belief"):
    return Mem(id=id, type=type, title=title, content=f"c-{id}", importance=0.5,
               confidence=conf, valid_from=None, valid_to=None, pinned=0,
               created_at=None, updated_at=None, metadata={})


def _cluster(title, *mems):
    ms = list(mems)
    # Force the top member's display_title to `title` so head-keying is testable.
    ms[0].title = title
    return Cluster(key=min(m.id for m in ms), members=ms)


class _Compiler:
    prompt_version = "1"
    model = "stub"

    def __init__(self, prose="PROSE"):
        self._prose = prose
        self.compiles = 0

    def prompt_text(self):
        return "PROMPT"

    def compile(self, cluster):
        self.compiles += 1
        return self._prose


# ── load_heads ───────────────────────────────────────────────────────────────

def test_load_heads_keys_by_title_and_hashes_content():
    rows = [
        {"id": "s1", "title": "Alpha", "content": "alpha prose",
         "metadata": {"compiler": {"cluster_hash": "h-alpha"}}},
        {"id": "s2", "title": "Beta", "content": "beta prose",
         "metadata": {"compiler": {"cluster_hash": "h-beta"}}},
    ]
    heads = WC.load_heads(rows)
    assert set(heads) == {"Alpha", "Beta"}
    assert heads["Alpha"].id == "s1"
    assert heads["Alpha"].cluster_hash == "h-alpha"
    assert heads["Alpha"].prose_hash == WC.prose_hash("alpha prose")


def test_load_heads_missing_compiler_metadata_is_safe():
    rows = [{"id": "s1", "title": "Alpha", "content": "x", "metadata": {}}]
    heads = WC.load_heads(rows)
    assert heads["Alpha"].cluster_hash == ""  # no hash → will recompile, not crash


def test_load_heads_dedupes_same_title_deterministically():
    rows = [
        {"id": "s1", "title": "Alpha", "content": "old", "metadata": {}},
        {"id": "s2", "title": "Alpha", "content": "new", "metadata": {}},
    ]
    heads = WC.load_heads(rows)
    assert heads["Alpha"].id == "s2"  # id sorts last → wins


# ── compile_clusters (full pass) ─────────────────────────────────────────────

async def _run(clusters, compiler, heads, **kw):
    writes, sups, prompts = [], [], []

    async def _write(**a):
        writes.append(a)
        return "Created: new-id"

    async def _supersede(**a):
        sups.append(a)
        return "Superseded: new-id"

    async def _ensure_prompt(comp, stats, *, scope, user_id):
        prompts.append(comp.prompt_version)
        return "prompt-id"

    stats = await WC.compile_clusters(
        clusters, compiler, heads, _write=_write, _supersede=_supersede,
        _ensure_prompt=_ensure_prompt, **kw)
    return stats, writes, sups, prompts


@pytest.mark.asyncio
async def test_first_pass_creates_each_cluster():
    clusters = [_cluster("Alpha", _mem("a")), _cluster("Beta", _mem("b"))]
    stats, writes, sups, prompts = await _run(clusters, _Compiler(), heads={})
    assert stats.created == 2
    assert len(writes) == 2 and sups == []
    assert prompts == ["1"], "prompt must be written exactly once, before the loop"


@pytest.mark.asyncio
async def test_second_pass_unchanged_writes_nothing():
    """Idempotence at the pass level: heads present + hashes match → 0 writes."""
    from wiki.synth import _cluster_hash
    comp = _Compiler(prose="P")
    c = _cluster("Alpha", _mem("a"))
    heads = {"Alpha": WC.ExistingSynthesis(
        id="h", cluster_hash=_cluster_hash(c, comp.model),
        prose_hash=WC.prose_hash("P"))}
    stats, writes, sups, prompts = await _run([c], comp, heads)
    assert writes == [] and sups == []
    assert stats.skipped_hash == 1
    assert comp.compiles == 0, "unchanged cluster must not call the model"


@pytest.mark.asyncio
async def test_changed_prose_supersedes():
    comp = _Compiler(prose="NEW")
    c = _cluster("Alpha", _mem("a"))
    heads = {"Alpha": WC.ExistingSynthesis(id="old", cluster_hash="stale",
                                           prose_hash=WC.prose_hash("OLD"))}
    stats, writes, sups, prompts = await _run([c], comp, heads)
    assert len(sups) == 1 and sups[0]["old_id"] == "old"
    assert stats.superseded == 1


# ── WAL checkpoint cadence (§10, via the seam) ───────────────────────────────

class _Backend:
    """Records maintenance_checkpoint calls without a real DB."""
    def __init__(self):
        self.checkpoints = []  # list of `final` flags

    def connection(self):
        import contextlib
        return contextlib.nullcontext(object())

    def maintenance_checkpoint(self, conn, *, final=False):
        self.checkpoints.append(final)


@pytest.mark.asyncio
async def test_checkpoints_on_cadence_and_at_end():
    clusters = [_cluster(f"T{i}", _mem(f"m{i}")) for i in range(5)]
    be = _Backend()
    stats, writes, sups, prompts = await _run(
        clusters, _Compiler(), heads={}, backend=be, checkpoint_every=2)
    # 5 creates: mid-batch checkpoints after #2 and #4 (final=False), then one
    # final=True at the end.
    assert be.checkpoints == [False, False, True]


@pytest.mark.asyncio
async def test_no_backend_means_no_checkpoint_calls():
    clusters = [_cluster("Alpha", _mem("a"))]
    stats, writes, sups, prompts = await _run(clusters, _Compiler(), heads={},
                                              backend=None)
    assert stats.created == 1  # runs fine, just no checkpointing
