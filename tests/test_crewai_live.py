"""Live in-process round-trip: real CrewAI Memory types + m3 M3StorageBackend.

Proves the actual integration end-to-end against a real m3 DB — NOT just protocol
conformance. This is the test that catches bugs the hermetic + conformance tests
can't: it exercises the real m3 write → CrewAI-vector store → vector_search recall
path (a `content_hash` import bug was caught exactly here during development).

Gated on ``crewai`` being importable AND runnable — CrewAI v1.x requires Python
<3.14, so on 3.14+ (where crewai won't install) this skips cleanly. Uses the
conftest DB-isolation fixtures (a per-test tmp engine), so no OPENAI key or network
is needed: CrewAI's own embedder is never invoked — the adapter receives vectors
we supply directly.
"""

from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_REPO, os.path.join(_REPO, "bin")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

crewai = pytest.importorskip("crewai", reason="requires `pip install crewai` (Python <3.14)")

DIM = 8


def _vec(seed: int) -> list[float]:
    return [((seed * (i + 1)) % 7) / 7.0 for i in range(DIM)]


@pytest.fixture()
def backend():
    from m3_memory.integrations.crewai.backend import M3StorageBackend

    b = M3StorageBackend(user_id="live-crew")
    yield b
    try:
        b.reset()
    except Exception:
        pass


def _record(**kw):
    from crewai.memory.types import MemoryRecord

    return MemoryRecord(**kw)


def test_save_then_vector_search_roundtrip(backend):
    r1 = _record(content="the capital of France is Paris", scope="/crew/geo",
                 categories=["fact"], embedding=_vec(1))
    r2 = _record(content="water boils at 100C at sea level", scope="/crew/science",
                 categories=["fact"], embedding=_vec(3))
    backend.save([r1, r2])

    hits = backend.search(_vec(1), limit=5)
    assert isinstance(hits, list) and hits, "search returned no results"
    rec, score = hits[0]
    assert isinstance(score, float)
    assert "Paris" in rec.content, "vector search did not rank the matching record first"
    assert rec.scope == "/crew/geo", "scope path did not round-trip"
    assert rec.categories == ["fact"], "categories did not round-trip"


def test_scope_prefix_filter(backend):
    backend.save([
        _record(content="geo fact", scope="/crew/geo", categories=["f"], embedding=_vec(1)),
        _record(content="sci fact", scope="/crew/science", categories=["f"], embedding=_vec(2)),
    ])
    geo = backend.search(_vec(1), scope_prefix="/crew/geo", limit=5)
    assert all(r.scope.startswith("/crew/geo") for r, _ in geo)
    assert not any(r.scope.startswith("/crew/science") for r, _ in geo)


def test_scope_introspection(backend):
    backend.save([
        _record(content="a", scope="/crew/geo", categories=["fact"], embedding=_vec(1)),
        _record(content="b", scope="/crew/science", categories=["fact"], embedding=_vec(2)),
    ])
    assert set(backend.list_scopes("/crew")) >= {"/crew/geo", "/crew/science"}
    info = backend.get_scope_info("/crew")
    assert info.record_count >= 2
    assert "fact" in info.categories
    assert backend.list_categories("/crew").get("fact", 0) >= 2


def test_get_record_and_list_records(backend):
    backend.save([_record(content="findme", scope="/crew/x", categories=[],
                          embedding=_vec(5))])
    all_recs = backend.list_records()
    assert len(all_recs) >= 1
    rid = all_recs[0].id
    got = backend.get_record(rid)
    assert got is not None and got.id == rid


def test_delete_by_scope_and_reset(backend):
    backend.save([
        _record(content="keep", scope="/crew/geo", categories=[], embedding=_vec(1)),
        _record(content="drop", scope="/crew/science", categories=[], embedding=_vec(2)),
    ])
    n = backend.delete(scope_prefix="/crew/science")
    assert n >= 1
    remaining = backend.list_records()
    assert all("/crew/science" not in r.scope for r in remaining)
    backend.reset()
    assert backend.list_records() == []


def test_tenant_isolation(backend):
    """A second tenant's backend never sees the first tenant's memories (§7)."""
    from m3_memory.integrations.crewai.backend import M3StorageBackend

    backend.save([_record(content="tenant-a secret", scope="/x", categories=[],
                          embedding=_vec(1))])
    other = M3StorageBackend(user_id="live-crew-B")
    try:
        assert other.list_records() == [], "tenant B saw tenant A's memories"
        assert other.search(_vec(1), limit=5) == [], "tenant B searched into tenant A"
    finally:
        other.reset()
