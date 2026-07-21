"""Cluster core memories into topic pages.

Default path is pure-Python (stdlib only): a weighted union-find over the memory
edge graph groups strongly-connected memories into one topic. This keeps
build_wiki() dependency-free and byte-deterministic.

If networkx is installed (`pip install "m3-memory[wiki]"`), an optional
greedy-modularity pass produces tighter communities. It is imported lazily and
guarded — never a hard dependency. The pure path is always correct; networkx only
improves cluster *quality*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .select import Edge, Mem

# Edges at or above this weight bind two memories into the same cluster in the
# pure-Python path. Weak edges (related/references at 1.0) still bind — they are
# real links — but the threshold lets us treat trivial precedes/follows (0.5) as
# non-binding so a long chain doesn't collapse into one mega-page.
_BIND_THRESHOLD = 1.0

# A cluster larger than this is split (by the pure path) to avoid one giant page.
_MAX_CLUSTER = 25


@dataclass
class Cluster:
    key: str                       # deterministic slug-seed (smallest member id)
    members: list[Mem] = field(default_factory=list)
    is_orphan: bool = False        # singleton with no binding edges

    def rank_key(self) -> tuple:
        # Bigger, more-important clusters first; ties broken by key for determinism.
        top_imp = max((m.importance or 0.0) for m in self.members) if self.members else 0.0
        return (-len(self.members), -top_imp, self.key)


class _UnionFind:
    def __init__(self, ids: list[str]) -> None:
        self.parent = {i: i for i in ids}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # path compression
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Deterministic: smaller id becomes root.
        if ra < rb:
            self.parent[rb] = ra
        else:
            self.parent[ra] = rb


def cluster(memories: list[Mem], edges: list[Edge], *, use_networkx: bool = True) -> list[Cluster]:
    """Group memories into topic clusters. Deterministic ordering guaranteed."""
    if not memories:
        return []
    nx = _try_networkx() if use_networkx else None
    if nx is not None:
        try:
            return _cluster_networkx(nx, memories, edges)
        except Exception:
            # Never let an optional-path failure break generation.
            pass
    return _cluster_pure(memories, edges)


def _cluster_pure(memories: list[Mem], edges: list[Edge]) -> list[Cluster]:
    by_id = {m.id: m for m in memories}
    uf = _UnionFind(list(by_id.keys()))
    bound: set[str] = set()
    for e in sorted(edges, key=lambda e: (e.from_id, e.to_id, e.rel)):
        if e.weight >= _BIND_THRESHOLD and e.from_id in by_id and e.to_id in by_id:
            uf.union(e.from_id, e.to_id)
            bound.add(e.from_id)
            bound.add(e.to_id)

    groups: dict[str, list[Mem]] = {}
    for mid, m in by_id.items():
        groups.setdefault(uf.find(mid), []).append(m)

    clusters: list[Cluster] = []
    for root, members in groups.items():
        members.sort(key=lambda m: m.rank_key())
        orphan = len(members) == 1 and members[0].id not in bound
        for chunk in _split(members):
            clusters.append(
                Cluster(key=min(m.id for m in chunk), members=chunk, is_orphan=orphan)
            )
    clusters.sort(key=lambda c: c.rank_key())
    return clusters


def _split(members: list[Mem]) -> list[list[Mem]]:
    """Split an over-large cluster into deterministic size-capped chunks."""
    if len(members) <= _MAX_CLUSTER:
        return [members]
    return [members[i : i + _MAX_CLUSTER] for i in range(0, len(members), _MAX_CLUSTER)]


def _try_networkx():
    try:
        import networkx as nx  # type: ignore
        return nx
    except ImportError:
        return None


def _cluster_networkx(nx, memories: list[Mem], edges: list[Edge]) -> list[Cluster]:
    by_id = {m.id: m for m in memories}
    g = nx.Graph()
    g.add_nodes_from(by_id.keys())
    for e in edges:
        if e.from_id in by_id and e.to_id in by_id:
            w = e.weight
            if g.has_edge(e.from_id, e.to_id):
                g[e.from_id][e.to_id]["weight"] += w
            else:
                g.add_edge(e.from_id, e.to_id, weight=w)

    from networkx.algorithms.community import greedy_modularity_communities  # type: ignore

    # greedy_modularity_communities needs >1 node with edges to be meaningful;
    # isolated nodes come back as singleton communities, which is what we want.
    communities = greedy_modularity_communities(g, weight="weight")
    degree = dict(g.degree())

    clusters: list[Cluster] = []
    for comm in communities:
        members = sorted((by_id[i] for i in comm), key=lambda m: m.rank_key())
        for chunk in _split(members):
            orphan = len(chunk) == 1 and degree.get(chunk[0].id, 0) == 0
            clusters.append(Cluster(key=min(m.id for m in chunk), members=chunk, is_orphan=orphan))
    clusters.sort(key=lambda c: c.rank_key())
    return clusters
