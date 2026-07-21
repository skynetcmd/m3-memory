"""Render clusters + files into deterministic Markdown pages.

Produces a dict {relpath: text}. Every page carries a generator banner, YAML
frontmatter built from real columns, inline [[wikilinks]] for edges, an Evidence
section (memory -> source file via promotions), and Backlinks. Output is fully
sorted/stable so re-running with no DB change yields byte-identical files.
"""
from __future__ import annotations

import re
from typing import Optional

from .cluster import Cluster
from .files_layer import FilesLayer, FileNode
from .select import Edge, Mem, Promo

BANNER = (
    "> **Generated** by `bin/gen_wiki.py` from your m3 memory + files stores — "
    "do not edit by hand; re-run `m3 wiki generate` to refresh."
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s or "untitled"


class SlugBook:
    """Assigns collision-free, deterministic slugs within a namespace."""

    def __init__(self) -> None:
        self._taken: dict[str, str] = {}   # slug -> owner id
        self._by_owner: dict[str, str] = {}  # owner id -> slug

    def assign(self, owner_id: str, seed: str) -> str:
        if owner_id in self._by_owner:
            return self._by_owner[owner_id]
        base = slugify(seed)
        slug = base
        # Deterministic disambiguation: append a short id suffix on collision.
        if slug in self._taken and self._taken[slug] != owner_id:
            slug = f"{base}-{owner_id[:8]}"
            n = 2
            while slug in self._taken and self._taken[slug] != owner_id:
                slug = f"{base}-{owner_id[:8]}-{n}"
                n += 1
        self._taken[slug] = owner_id
        self._by_owner[owner_id] = slug
        return slug

    def get(self, owner_id: str) -> Optional[str]:
        return self._by_owner.get(owner_id)


def _fm(lines: list[str]) -> str:
    return "---\n" + "\n".join(lines) + "\n---\n"


def _conf(m: Mem) -> str:
    return f"{m.confidence:.2f}" if m.confidence is not None else "n/a"


def render_pages(
    clusters: list[Cluster],
    edges: list[Edge],
    files: FilesLayer,
    promotions: list[Promo],
    ledes: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Build the full vault as {relpath: markdown}.

    `ledes` maps cluster.key -> a prose summary (from optional synthesis). When a
    cluster has no lede, its page falls back to the deterministic member list.
    """
    ledes = ledes or {}
    topic_slugs = SlugBook()
    source_slugs = SlugBook()

    # Pre-assign slugs so cross-links resolve.
    topic_clusters = [c for c in clusters if not c.is_orphan]
    orphan_members: list[Mem] = [m for c in clusters if c.is_orphan for m in c.members]
    for c in topic_clusters:
        top = c.members[0] if c.members else None
        seed = top.display_title if top else c.key
        topic_slugs.assign(c.key, seed)
    for fn in files.files:
        source_slugs.assign(fn.uuid, fn.filename)

    # Map memory id -> topic slug (for wikilinks + backlinks + evidence).
    mem_to_topic: dict[str, str] = {}
    for c in topic_clusters:
        slug = topic_slugs.get(c.key)
        for m in c.members:
            mem_to_topic[m.id] = slug  # type: ignore[assignment]

    # promotions grouped by target memory id.
    promo_by_mem: dict[str, list[Promo]] = {}
    for p in promotions:
        promo_by_mem.setdefault(p.promoted_to, []).append(p)

    # Backlink index: for each memory, which other topics link to it.
    backlinks: dict[str, set[str]] = {}
    for e in edges:
        src_slug = mem_to_topic.get(e.from_id)
        if src_slug and e.to_id in mem_to_topic:
            backlinks.setdefault(e.to_id, set()).add(src_slug)

    pages: dict[str, str] = {}

    for c in topic_clusters:
        slug = topic_slugs.get(c.key)
        pages[f"topics/{slug}.md"] = _render_topic(
            c, edges, mem_to_topic, promo_by_mem, backlinks, files, ledes.get(c.key)
        )

    if orphan_members:
        pages["topics/orphans.md"] = _render_orphans(orphan_members, promo_by_mem, files)

    for fn in files.files:
        slug = source_slugs.get(fn.uuid)
        pages[f"sources/{slug}.md"] = _render_source(fn, promotions, mem_to_topic, topic_slugs)

    pages["index.md"] = _render_index(topic_clusters, topic_slugs, files, source_slugs, bool(orphan_members))
    pages["overview.md"] = _render_overview(clusters, files)
    pages["lint.md"] = _render_lint(clusters, edges, mem_to_topic)
    pages["about.md"] = _render_about()

    return pages


def _render_topic(
    c: Cluster,
    edges: list[Edge],
    mem_to_topic: dict[str, str],
    promo_by_mem: dict[str, list[Promo]],
    backlinks: dict[str, set[str]],
    files: FilesLayer,
    lede: Optional[str] = None,
) -> str:
    top = c.members[0]
    slug = mem_to_topic.get(top.id, c.key)
    # Related topics: distinct other-cluster slugs this cluster's edges point to.
    member_ids = {m.id for m in c.members}
    related: set[str] = set()
    contradictions: list[tuple[str, str]] = []
    for e in edges:
        if e.from_id in member_ids or e.to_id in member_ids:
            other = e.to_id if e.from_id in member_ids else e.from_id
            other_slug = mem_to_topic.get(other)
            if other_slug and other_slug != slug:
                related.add(other_slug)
            if e.rel == "contradicts" and e.from_id in member_ids and e.to_id in member_ids:
                contradictions.append(tuple(sorted((e.from_id, e.to_id))))  # type: ignore[arg-type]

    fm = [
        f"title: {top.display_title}",
        f"type: {top.type}",
        f"confidence: {_conf(top)}",
        f"memory_ids: [{', '.join(m.id for m in c.members)}]",
        f"pinned: {'true' if any(m.pinned for m in c.members) else 'false'}",
    ]
    if top.valid_from:
        fm.append(f"valid_from: {top.valid_from}")
    if related:
        rel_links = ", ".join(f'"[[{s}]]"' for s in sorted(related))
        fm.append(f"related: [{rel_links}]")

    lines = [_fm(fm), f"# {top.display_title}", "", BANNER, ""]

    if lede:
        lines.append(lede.strip())
        lines.append("")

    if contradictions:
        lines.append("> ⚠️ **Contradiction on this page** — members below disagree; "
                     "higher-confidence claim should be treated as current. See `lint.md`.")
        lines.append("")

    # Body: ranked member list (deterministic; synthesis is a later phase).
    lines.append("## Members")
    lines.append("")
    for m in c.members:
        pin = " 📌" if m.pinned else ""
        head = m.content.strip().splitlines()[0].strip() if m.content.strip() else ""
        snippet = (head[:200] + "…") if len(head) > 200 else head
        lines.append(f"- **{m.display_title}**{pin} · `{m.type}` · conf {_conf(m)} "
                     f"· `id:{m.id[:8]}`")
        if snippet:
            lines.append(f"  {snippet}")
    lines.append("")

    # Evidence: source files behind these memories.
    ev = _evidence_links(c.members, promo_by_mem, files)
    if ev:
        lines.append("## Evidence")
        lines.append("")
        lines.extend(ev)
        lines.append("")

    # Backlinks: other topics that link into this cluster's members.
    incoming: set[str] = set()
    for m in c.members:
        for s in backlinks.get(m.id, set()):
            if s != slug:
                incoming.add(s)
    if incoming:
        lines.append("## Backlinks")
        lines.append("")
        lines.append(" · ".join(f"[[{s}]]" for s in sorted(incoming)))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _evidence_links(
    members: list[Mem],
    promo_by_mem: dict[str, list[Promo]],
    files: FilesLayer,
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in members:
        for p in promo_by_mem.get(m.id, []):
            key = p.marker_uuid
            if key in seen:
                continue
            seen.add(key)
            fname = p.filename or "(source file)"
            src = f" — `{p.source_path}`" if p.source_path else ""
            out.append(f"- {m.display_title} ⇐ **{fname}** ({p.source_memory_type}){src}")
    return sorted(out)


def _render_orphans(
    members: list[Mem],
    promo_by_mem: dict[str, list[Promo]],
    files: FilesLayer,
) -> str:
    lines = [
        _fm(["title: Orphans", "type: index"]),
        "# Orphans",
        "",
        BANNER,
        "",
        "_Core memories with no binding links — kept here rather than minting a "
        "page each (a guard against 'graph theatre')._",
        "",
    ]
    for m in sorted(members, key=lambda m: m.rank_key()):
        pin = " 📌" if m.pinned else ""
        lines.append(f"- **{m.display_title}**{pin} · `{m.type}` · conf {_conf(m)} · `id:{m.id[:8]}`")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_source(
    fn: FileNode,
    promotions: list[Promo],
    mem_to_topic: dict[str, str],
    topic_slugs,
) -> str:
    fm = [
        f"title: {fn.filename}",
        "type: source",
        f"filetype: {fn.filetype}",
    ]
    if fn.corpus_id:
        fm.append(f"corpus: {fn.corpus_id}")
    lines = [_fm(fm), f"# {fn.filename}", "", BANNER, ""]
    if fn.path:
        lines.append(f"`{fn.path}`")
        lines.append("")
    if fn.summary:
        lines.append(fn.summary.strip())
        lines.append("")

    # Up-links: memories promoted from this file.
    up: set[str] = set()
    for p in promotions:
        if p.filename == fn.filename:
            slug = mem_to_topic.get(p.promoted_to)
            if slug:
                up.add(slug)
    if up:
        lines.append("## Fed into")
        lines.append("")
        lines.append(" · ".join(f"[[{s}]]" for s in sorted(up)))
        lines.append("")

    if fn.facts:
        lines.append("## Notable facts")
        lines.append("")
        for f in fn.facts:
            lines.append(f"- {f.statement.strip()} · conf {f.confidence:.2f}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# Human-facing section headings for the dominant memory type of a topic. A topic's
# "kind" is the most common type among its members; this groups the index the way a
# reader thinks ("runbooks", "decisions") rather than by cluster id.
_TYPE_SECTIONS = [
    ("belief", "🧠 Knowledge & beliefs"),
    ("procedure", "📘 Runbooks & procedures"),
    ("decision", "⚖️ Decisions"),
    ("reference", "📎 References"),
    ("security", "🔒 Security"),
    ("infrastructure", "🖥️ Infrastructure"),
]
_TYPE_ORDER = {t: i for i, (t, _) in enumerate(_TYPE_SECTIONS)}
_TYPE_LABEL = dict(_TYPE_SECTIONS)


def _dominant_type(c: Cluster) -> str:
    counts: dict[str, int] = {}
    for m in c.members:
        counts[m.type] = counts.get(m.type, 0) + 1
    # Deterministic: highest count, then _TYPE_ORDER, then name.
    return sorted(counts, key=lambda t: (-counts[t], _TYPE_ORDER.get(t, 99), t))[0]


def _pin_count(c: Cluster) -> int:
    return sum(1 for m in c.members if m.pinned)


def _render_index(topic_clusters, topic_slugs, files, source_slugs, has_orphans: bool) -> str:
    total_mem = sum(len(c.members) for c in topic_clusters)
    lines = [
        _fm(["title: Index", "type: index"]),
        "# m3 Wiki",
        "",
        BANNER,
        "",
        f"Your knowledge, compiled: **{len(topic_clusters)} topics** covering "
        f"**{total_mem} memories**, plus **{len(files.files)} source documents**. "
        "Start with the [[overview]], or jump to a topic below.",
        "",
    ]

    # Surface the highest-signal topics first: pinned content, then largest.
    def prominence(c: Cluster) -> tuple:
        top_imp = max((m.importance or 0.0) for m in c.members) if c.members else 0.0
        return (-_pin_count(c), -len(c.members), -top_imp, c.key)

    starred = sorted(topic_clusters, key=prominence)[:8]
    if starred:
        lines.append("## ⭐ Start here")
        lines.append("")
        for c in starred:
            slug = topic_slugs.get(c.key)
            pin = " 📌" if _pin_count(c) else ""
            lines.append(f"- [[{slug}]] — {c.members[0].display_title}{pin} "
                         f"({len(c.members)} memories)")
        lines.append("")

    # Group the full list by dominant type, in reader order.
    by_kind: dict[str, list[Cluster]] = {}
    for c in topic_clusters:
        by_kind.setdefault(_dominant_type(c), []).append(c)

    kinds = sorted(by_kind, key=lambda k: (_TYPE_ORDER.get(k, 99), k))
    for kind in kinds:
        heading = _TYPE_LABEL.get(kind, f"📄 {kind.title()}")
        clusters_here = sorted(by_kind[kind], key=prominence)
        lines.append(f"## {heading}")
        lines.append("")
        for c in clusters_here:
            slug = topic_slugs.get(c.key)
            pin = " 📌" if _pin_count(c) else ""
            lines.append(f"- [[{slug}]] — {c.members[0].display_title}{pin} "
                         f"({len(c.members)} memories)")
        lines.append("")

    if files.files:
        lines.append(f"## 📁 Source documents ({len(files.files)})")
        lines.append("")
        for fn in files.files:
            slug = source_slugs.get(fn.uuid)
            lines.append(f"- [[{slug}]] — {fn.filename}")
        lines.append("")

    lines.append("## Housekeeping")
    lines.append("")
    lines.append("- [[about]] — what this vault is and how it's built")
    if has_orphans:
        lines.append("- [[orphans]] — core memories with no links yet")
    lines.append("- [[lint]] — orphans, dangling links, contradictions")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_overview(clusters: list[Cluster], files: FilesLayer) -> str:
    topics = [c for c in clusters if not c.is_orphan]
    orphans = [m for c in clusters if c.is_orphan for m in c.members]
    all_mem = [m for c in clusters for m in c.members]
    pinned = sum(1 for m in all_mem if m.pinned)
    lines = [
        _fm(["title: Overview", "type: index"]),
        "# Overview",
        "",
        BANNER,
        "",
        f"- **Core memories:** {len(all_mem)}",
        f"- **Topics:** {len(topics)}",
        f"- **Orphans:** {len(orphans)}",
        f"- **Pinned:** {pinned}",
        f"- **Source files:** {len(files.files)}",
        "",
        "## Largest topics",
        "",
    ]
    for c in sorted(topics, key=lambda c: c.rank_key())[:10]:
        lines.append(f"- {c.members[0].display_title} ({len(c.members)} memories)")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_about() -> str:
    """A self-documenting page: explains the vault to whoever opens it.

    This is the `docs/WIKI.md` guide, rendered as a vault-native page with
    [[wikilinks]] into the vault's own structure — so the wiki explains itself in
    Obsidian without leaving the graph.
    """
    body = """This vault was compiled by **m3** from your memory store — it is a
*projection*, not something you edit by hand. Re-run `m3 wiki generate` to refresh
it; your edits here would be overwritten.

## How to read it

- **[[index]]** — the table of contents: a ⭐ *Start here* shortlist, then topics
  grouped by kind (Knowledge, Runbooks, Decisions, References).
- **[[overview]]** — counts and your largest topics at a glance.
- **Topics** (`topics/`) — one page per cluster of related memories. Each carries
  its source `memory_ids`, confidence, an *Evidence* section linking to the files a
  fact came from, and *Backlinks*.
- **Sources** (`sources/`) — one page per indexed document, with its summary and
  notable extracted facts.
- **[[lint]]** — housekeeping: orphaned memories and contradictions (memories that
  disagree are kept together and reported, never silently dropped).

## What's included

A memory appears here when it is **canonical** — pinned, high-importance, or a
consolidated `belief` / `procedure` / `reference`. Related memories are grouped
into topics using m3's relationship graph *and* shared entities, so notes about the
same thing land together even without an explicit link.

## Regenerating

```
m3 wiki generate                 # refresh this vault
m3 wiki generate --synthesize    # add an LLM prose lede to each topic
m3 wiki status                   # location, page count, last build
```

Full guide: the `docs/WIKI.md` file in the m3-memory repository."""
    lines = [
        _fm(["title: About this wiki", "type: index"]),
        "# About this wiki",
        "",
        BANNER,
        "",
        body,
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_lint(clusters, edges, mem_to_topic) -> str:
    all_ids = {m.id for c in clusters for m in c.members}
    orphans = [m for c in clusters if c.is_orphan for m in c.members]
    # Dangling: edges pointing at a memory not in the core set were already
    # dropped upstream, so here we report contradictions and orphan counts.
    contradictions = sorted(
        {tuple(sorted((e.from_id, e.to_id))) for e in edges if e.rel == "contradicts"}
    )
    lines = [
        _fm(["title: Lint", "type: index"]),
        "# Lint",
        "",
        BANNER,
        "",
        f"## Orphans ({len(orphans)})",
        "",
    ]
    for m in sorted(orphans, key=lambda m: m.rank_key()):
        lines.append(f"- {m.display_title} · `id:{m.id[:8]}`")
    lines.append("")
    lines.append(f"## Contradictions ({len(contradictions)})")
    lines.append("")
    for a, b in contradictions:
        ta, tb = mem_to_topic.get(a, "?"), mem_to_topic.get(b, "?")
        lines.append(f"- `{a[:8]}` ([[{ta}]]) ⚔️ `{b[:8]}` ([[{tb}]])")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"
