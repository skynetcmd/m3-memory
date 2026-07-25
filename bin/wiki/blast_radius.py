"""Blast-radius + delta-lint over the compiled-knowledge graph (commit 4a).

When a synthesis is judged WRONG (`metadata.verdict == "wrong"`), every synthesis
that was compiled FROM it is now suspect — its prose may carry the wrong claim
forward. In m3 that blast radius is *computable*, because a synthesis records its
sources as `consolidates` / `distills_from` edges. A file-based wiki cannot do
this: a wikilink does not say "this page was an INPUT to that compilation."

This is a DETERMINISTIC graph walk over the already-loaded edge list — no model,
no new queries (S7: the edges are in hand from clustering; re-querying per node
would reintroduce an N+1 the metric forbids). So it lives inside the
drift-tested surface, and its result is exact: 100% precision and recall on a
known topology, because the answer is a graph fact, not a judgement.

The same reachability also gives delta-lint its scope: given a set of changed
nodes, the nodes worth re-checking are those plus everything that consolidates
from them.
"""
from __future__ import annotations

from typing import Iterable

# Edges whose direction means "the FROM node was compiled from the TO node", so a
# wrong TO node contaminates the FROM node. These are the provenance edges a
# synthesis writes (compile.py): consolidates (compiled page) / distills_from
# (a filed answer). `supersedes` is NOT here — a supersede replaces, it does not
# derive-from.
_PROVENANCE_RELS = ("consolidates", "distills_from")


def wrong_ids(clusters) -> set[str]:
    """Ids of member memories explicitly judged wrong (metadata.verdict)."""
    wrong: set[str] = set()
    for c in clusters:
        for m in c.members:
            meta = getattr(m, "metadata", None) or {}
            if isinstance(meta, dict) and meta.get("verdict") == "wrong":
                wrong.add(m.id)
    return wrong


def _dependents_index(edges) -> dict[str, list[str]]:
    """Map each source id -> the ids that were compiled FROM it.

    A provenance edge is written FROM the synthesis TO its source, so the
    dependents of a source S are the `from_id`s of provenance edges whose
    `to_id == S`. Built once from the in-memory edge list (no query).
    """
    idx: dict[str, list[str]] = {}
    for e in edges:
        if e.rel in _PROVENANCE_RELS:
            idx.setdefault(e.to_id, []).append(e.from_id)
    return idx


def contaminated(clusters, edges, *, seeds: Iterable[str] | None = None) -> set[str]:
    """Ids transitively compiled from a wrong (or seeded) node.

    `seeds` overrides the wrong-verdict scan (used by delta-lint to ask "what
    depends on THESE changed nodes"). The seeds themselves are NOT included —
    the result is strictly the downstream blast radius.

    Deterministic and cycle-safe (a visited set bounds the walk).
    """
    roots = set(seeds) if seeds is not None else wrong_ids(clusters)
    if not roots:
        return set()
    idx = _dependents_index(edges)
    out: set[str] = set()
    # Walk downstream from every root. `visited` bounds the traversal (cycle-safe
    # AND avoids re-expanding a node); `out` is the reported radius, which by
    # spec excludes the roots themselves — even a root that is also a dependent
    # of another root is a seed, and seeds are the inputs, not the blast.
    visited: set[str] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        for dep in idx.get(node, ()):
            if dep in visited:
                continue
            visited.add(dep)
            stack.append(dep)
            if dep not in roots:
                out.add(dep)
    return out


def delta_scope(edges, changed: Iterable[str]) -> set[str]:
    """Delta-lint scope: the changed nodes PLUS their direct dependents.

    Cheap re-check set after an incremental change — a full re-lint is wasteful
    when only a few nodes moved (youcanbuildthings' ~80% cut). Direct dependents
    only (one hop): a change's immediate consumers are what need re-judging;
    deeper effects surface on the next pass once those are re-evaluated.

    Bounds are REPORTED by the caller, never silent (§3): a narrowed scan that
    hides what it skipped reads as 'clean' when it merely did not look.
    """
    changed = set(changed)
    idx = _dependents_index(edges)
    scope = set(changed)
    for node in changed:
        scope.update(idx.get(node, ()))
    return scope
