"""Cluster core memories into topic pages.

Clustering uses networkx greedy-modularity community detection over the memory
edge graph: strongly-connected memories are grouped into one topic. networkx is a
base dependency of m3-memory (the Memory Wiki is a core feature), so it is
imported directly and trusted to be present — the same way PyYAML, cryptography,
and the other required deps are. There is no pure-Python fallback: a base dep is
not reimplemented in-tree.

Output is deterministic run-to-run: greedy_modularity_communities is a greedy
algorithm with defined tie-breaks, and members/clusters are sorted by stable keys
(id, importance) so `m3 wiki generate --check` stays byte-reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from .select import Edge, Mem

# A cluster larger than this is split into chunks to avoid one unreadable page.
# Set high: a genuine topic of 40-60 related memories reads far better as ONE
# coherent page than as arbitrarily-sliced sub-pages (Obsidian handles long pages
# fine). Splitting only kicks in for pathologically large components.
_MAX_CLUSTER = 60


@dataclass
class Cluster:
    key: str                       # deterministic slug-seed (smallest member id)
    members: list[Mem] = field(default_factory=list)
    is_orphan: bool = False        # singleton with no binding edges

    def rank_key(self) -> tuple:
        # Bigger, more-important clusters first; ties broken by key for determinism.
        top_imp = max((m.importance or 0.0) for m in self.members) if self.members else 0.0
        return (-len(self.members), -top_imp, self.key)


def cluster(memories: list[Mem], edges: list[Edge]) -> list[Cluster]:
    """Group memories into topic clusters. Deterministic ordering guaranteed."""
    if not memories:
        return []
    return _cluster_networkx(memories, edges)


def _split(members: list[Mem]) -> list[list[Mem]]:
    """Split an over-large cluster into deterministic size-capped chunks."""
    if len(members) <= _MAX_CLUSTER:
        return [members]
    return [members[i : i + _MAX_CLUSTER] for i in range(0, len(members), _MAX_CLUSTER)]


def _cluster_networkx(memories: list[Mem], edges: list[Edge]) -> list[Cluster]:
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

    from networkx.algorithms.community import greedy_modularity_communities

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
