"""Pure wiki builder.

`build_wiki(mem_conn, files_conn, opts)` takes two open sqlite3 connections and
returns {relpath: markdown_text}. No path resolution, no file I/O, no embedder, no
timestamps — so the determinism test can drive it from fixture DBs and assert
byte-identical output across runs. The I/O shell lives in bin/gen_wiki.py.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import admission as _admission
from . import cluster as _cluster
from . import files_layer as _files
from . import render as _render
from . import select as _select


@dataclass
class WikiOptions:
    importance_threshold: float = _select.DEFAULT_IMPORTANCE_THRESHOLD
    include_files: bool = True
    include_all_corpora: bool = True
    corpora: Optional[list[str]] = None
    exclude_corpora: Optional[list[str]] = None
    limit: int = 5000
    # When True, memories that share an extracted entity are bound into the same
    # topic even without a hand-authored edge. This is what collapses the large
    # "orphan" set into real topics. Synthetic co-mention edges affect CLUSTERING
    # only — they never appear as rendered "Related"/"Backlinks" (those stay real).
    entity_comention: bool = True
    # A regex; any memory whose title/content matches is excluded from the vault.
    # Used to keep private/bench memories out of a shareable export.
    exclude_regex: Optional[str] = None
    # Emit [[wikilinks]] instead of portable [text](path.md) links, so Obsidian's
    # graph view + backlinks work. Opt-in (renders as literal text elsewhere).
    obsidian: bool = False
    # Run the admission gate (wiki.admission): demote weakly-anchored multi-member
    # clusters to the orphan list rather than rendering them as syntheses. On by
    # default; set False for an audit build that shows every cluster ungated.
    admission_gate: bool = True


def build_wiki(
    mem_conn: sqlite3.Connection,
    files_conn: Optional[sqlite3.Connection],
    opts: Optional[WikiOptions] = None,
    synthesizer=None,
    drift_judge=None,
) -> dict[str, str]:
    """Compile the vault. `files_conn` may be None (memory-only vault).

    `synthesizer` is an optional wiki.synth.Synthesizer. When None (the default),
    the build is PURE and deterministic (topic bodies are member lists). When
    provided, each topic gets an LLM-written prose lede — this makes the build
    non-pure, so it's only used behind `--synthesize`, never in the drift test.
    """
    opts = opts or WikiOptions()

    memories = _select.select_core_memories(
        mem_conn,
        importance_threshold=opts.importance_threshold,
        limit=opts.limit,
        exclude_regex=opts.exclude_regex,
    )
    ids = {m.id for m in memories}
    edges = _select.load_memory_edges(mem_conn, ids)

    files = _files.FilesLayer()
    promotions: list[_select.Promo] = []
    if opts.include_files and files_conn is not None:
        files = _files.load_files_layer(
            files_conn,
            include_all=opts.include_all_corpora,
            corpora=opts.corpora,
            exclude_corpora=opts.exclude_corpora,
        )
        promotions = _select.load_promotions(files_conn, ids)

    # Cluster over explicit edges PLUS synthetic entity-co-mention edges, so
    # memories sharing an entity land in one topic. Render, however, only sees the
    # real edges — synthetic links never surface as "Related"/"Backlinks".
    cluster_edges = list(edges)
    if opts.entity_comention:
        cluster_edges += _select.load_entity_comention_edges(mem_conn, ids)

    clusters = _cluster.cluster(memories, cluster_edges)

    # Admission gate: a weakly-anchored multi-member cluster (co-mention grab-bag
    # or thin-provenance mega-page) is demoted to an orphan rather than rendered as
    # an authoritative synthesis. Marking it is_orphan routes its members to the
    # orphan list — the members are never dropped, only un-synthesized — and keeps
    # the anchor report honest (it scores only what actually renders as a topic).
    if opts.admission_gate:
        _apply_admission_gate(clusters, cluster_edges)

    # Optional prose ledes per topic (opt-in; needs a local model).
    ledes: dict[str, str] = {}
    if synthesizer is not None:
        for c in clusters:
            if c.is_orphan:
                continue
            prose = synthesizer.lede_for(c)
            if prose:
                ledes[c.key] = prose

    return _render.render_pages(clusters, edges, files, promotions, ledes=ledes,
                                drift_judge=drift_judge,
                                obsidian=opts.obsidian)


def _apply_admission_gate(clusters: list, cluster_edges: list) -> None:
    """Demote weakly-anchored multi-member clusters in place (set is_orphan=True).

    Scores each multi-member cluster with KAS and consults the config-resolved
    admission gate. A demoted cluster is marked is_orphan so render routes its
    members to the orphan list instead of a synthesis page. Singletons and clusters
    the gate admits are untouched. Never raises — a scoring hiccup on one cluster
    leaves it admitted (fail-open: we'd rather render a page than silently drop a
    topic), matching the wiki's other never-break-generation guards.
    """
    gate = _admission.load_gate()
    if not gate.enabled:
        return
    for c in clusters:
        if c.is_orphan or len(c.members) < 2:
            continue
        try:
            demote = not gate.admits(c, cluster_edges)
        except Exception:
            continue  # fail-open: keep the cluster rather than break the build
        if demote:
            c.is_orphan = True
