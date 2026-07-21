"""Render clusters + files into deterministic Markdown pages.

Produces a dict {relpath: text}. Every page carries a generator banner, YAML
frontmatter built from real columns, standard Markdown hyperlinks for edges (so the
vault is browsable in ANY renderer — GitHub, a browser, Obsidian), an Evidence
section (memory -> source file via promotions), and Backlinks. Output is fully
sorted/stable so re-running with no DB change yields byte-identical files.
"""
from __future__ import annotations

import posixpath
import re
from typing import Optional

from .cluster import Cluster
from .files_layer import FilesLayer, FileNode
from .select import Edge, Mem, Promo

BANNER = (
    "> **Generated** by `bin/gen_wiki.py` from your m3 memory + files stores — "
    "do not edit by hand; re-run `m3 wiki generate` to refresh."
)

def _logo_src() -> str:
    """The m3 logo as an inline base64 data-URI so a rendered vault carries its
    branding with NO network — it works offline, over file://, and when embedded.
    Falls back to the raw.githubusercontent.com URL if the packaged PNG isn't found.
    """
    import base64
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "..", "docs", "m3_logo_icon.png"),  # dev tree (bin/wiki -> ../../docs)
        os.path.join(here, "..", "docs", "m3_logo_icon.png"),        # installed (m3_memory/docs vs bin/wiki)
    ]
    for path in candidates:
        try:
            with open(os.path.abspath(path), "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except OSError:
            continue
    return ("https://raw.githubusercontent.com/skynetcmd/m3-memory/main/"
            "docs/m3_logo_icon.png")


# The m3 logo <img>, emitted on the vault's landing pages. Resolved once at import.
_LOGO = (
    f'<img src="{_logo_src()}" height="60" '
    'style="vertical-align: baseline; margin-bottom: -15px;"> '
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s or "untitled"


# GitHub heading-anchor slug: lowercase, drop anything but word chars/spaces/-,
# spaces → hyphens. Matches how GitHub/most Markdown renderers id a heading, so
# `#section` fragment links land on that `## Section` heading.
_ANCHOR_STRIP = re.compile(r"[^\w\s-]", re.UNICODE)
_ANCHOR_SPACE = re.compile(r"\s+")


def heading_anchor(heading: str) -> str:
    s = (heading or "").strip().lower()
    s = _ANCHOR_STRIP.sub("", s)
    s = _ANCHOR_SPACE.sub("-", s)
    return s.strip("-")


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


class LinkResolver:
    """Resolves a page reference to a relative Markdown hyperlink.

    Standard `[title](relpath.md)` links render as real hyperlinks in every
    Markdown viewer (GitHub, browsers, static-site generators) AND are followed by
    Obsidian — unlike `[[wikilinks]]`, which only work inside Obsidian. Paths are
    computed relative to the SOURCE page's directory so they resolve on disk.
    """

    def __init__(self) -> None:
        # ref -> (path_from_vault_root, display_title)
        self._reg: dict[str, tuple[str, str]] = {}

    def register(self, ref: str, path_from_root: str, title: str) -> None:
        self._reg[ref] = (path_from_root, title)

    def has(self, ref: str) -> bool:
        return ref in self._reg

    def link(self, ref: str, src_path_from_root: str,
             text: Optional[str] = None, anchor: Optional[str] = None) -> str:
        """Markdown link to `ref` from the page at `src_path_from_root`.

        `anchor` (a heading text or pre-slugged id) appends a `#fragment` so the
        link jumps to a section within the target page — GitHub/most renderers
        auto-assign these ids to headings. Falls back to plain text if the target
        is unknown, so a dangling reference never emits a broken link.
        """
        entry = self._reg.get(ref)
        if not entry:
            return text or ref
        target, title = entry
        src_dir = posixpath.dirname(src_path_from_root)
        rel = posixpath.relpath(target, src_dir or ".")
        frag = f"#{heading_anchor(anchor)}" if anchor else ""
        label = text or title
        return f"[{_esc_label(label)}]({_url_quote(rel)}{frag})"


def _esc_label(s: str) -> str:
    # Markdown link text: escape brackets that would break the [ ]( ) syntax.
    return (s or "").replace("[", "\\[").replace("]", "\\]")


def _url_quote(path: str) -> str:
    # Slugs are [a-z0-9-] and dirs are literal, so only spaces need handling;
    # keep '/' and '.' intact for a readable, working relative link.
    return path.replace(" ", "%20")


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

    # Map memory id -> topic slug (for links + backlinks + evidence).
    mem_to_topic: dict[str, str] = {}
    for c in topic_clusters:
        slug = topic_slugs.get(c.key)
        for m in c.members:
            mem_to_topic[m.id] = slug  # type: ignore[assignment]

    # Build the link registry: every page a ref can point at.
    links = LinkResolver()
    for name in ("index", "overview", "lint", "about"):
        links.register(name, f"{name}.md", name.title())
    if orphan_members:
        links.register("orphans", "topics/orphans.md", "Orphans")
    for c in topic_clusters:
        slug = topic_slugs.get(c.key)
        links.register(slug, f"topics/{slug}.md", c.members[0].display_title)  # type: ignore[arg-type]
    for fn in files.files:
        slug = source_slugs.get(fn.uuid)
        links.register(slug, f"sources/{slug}.md", fn.filename)  # type: ignore[arg-type]

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
            c, edges, mem_to_topic, promo_by_mem, backlinks, files, links, ledes.get(c.key)
        )

    if orphan_members:
        pages["topics/orphans.md"] = _render_orphans(orphan_members, promo_by_mem, files, links)

    for fn in files.files:
        slug = source_slugs.get(fn.uuid)
        pages[f"sources/{slug}.md"] = _render_source(fn, promotions, mem_to_topic, links)

    pages["index.md"] = _render_index(topic_clusters, topic_slugs, files, source_slugs, links, bool(orphan_members))
    pages["overview.md"] = _render_overview(clusters, files, links)
    pages["lint.md"] = _render_lint(clusters, edges, mem_to_topic, topic_slugs, links)
    pages["about.md"] = _render_about(links)

    return pages


def _render_topic(
    c: Cluster,
    edges: list[Edge],
    mem_to_topic: dict[str, str],
    promo_by_mem: dict[str, list[Promo]],
    backlinks: dict[str, set[str]],
    files: FilesLayer,
    links: "LinkResolver",
    lede: Optional[str] = None,
) -> str:
    top = c.members[0]
    slug = mem_to_topic.get(top.id, c.key)
    self_path = f"topics/{slug}.md"
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
        f"title: {_yaml(top.display_title)}",
        f"type: {top.type}",
        f"confidence: {_conf(top)}",
        f"memory_ids: [{', '.join(m.id for m in c.members)}]",
        f"pinned: {'true' if any(m.pinned for m in c.members) else 'false'}",
    ]
    if top.valid_from:
        fm.append(f"valid_from: {top.valid_from}")

    lines = [_fm(fm), f"# {top.display_title}", "", BANNER, ""]
    lines.append(_nav(links, self_path))
    lines.append("")

    if lede:
        lines.append(lede.strip())
        lines.append("")

    if contradictions:
        lines.append("> ⚠️ **Contradiction on this page** — members below disagree; "
                     "the higher-confidence claim should be treated as current. "
                     f"See {links.link('lint', self_path, 'the lint report', anchor='Contradictions')}.")
        lines.append("")

    # Body: ranked member list.
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

    # Related topics — real hyperlinks.
    if related:
        lines.append("## Related topics")
        lines.append("")
        for s in sorted(related, key=lambda s: links.link(s, self_path)):
            lines.append(f"- {links.link(s, self_path)}")
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
        for s in sorted(incoming, key=lambda s: links.link(s, self_path)):
            lines.append(f"- {links.link(s, self_path)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _yaml(s: str) -> str:
    """Quote a YAML scalar that may contain colons/special chars."""
    if s and (":" in s or s[0] in "#-[]{}!&*?|>%@`\"'"):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _nav(links: "LinkResolver", self_path: str) -> str:
    """A small breadcrumb of hyperlinks back to the vault's key pages."""
    parts = [links.link("index", self_path, "Index"),
             links.link("overview", self_path, "Overview"),
             links.link("about", self_path, "About")]
    return "↑ " + " · ".join(parts)


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
    links: "LinkResolver",
) -> str:
    lines = [
        _fm(["title: Orphans", "type: index"]),
        "# Orphans",
        "",
        BANNER,
        "",
        _nav(links, "topics/orphans.md"),
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
    links: "LinkResolver",
) -> str:
    # All source pages live in sources/, so any sources/*.md path is a correct
    # relative-link base — use a placeholder ("sources/x.md").
    fm = [
        f"title: {_yaml(fn.filename)}",
        "type: source",
        f"filetype: {fn.filetype}",
    ]
    if fn.corpus_id:
        fm.append(f"corpus: {fn.corpus_id}")
    lines = [_fm(fm), f"# {fn.filename}", "", BANNER, ""]
    lines.append(_nav(links, "sources/x.md"))
    lines.append("")
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
        for s in sorted(up, key=lambda s: links.link(s, "sources/x.md")):
            lines.append(f"- {links.link(s, 'sources/x.md')}")
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


def _render_index(topic_clusters, topic_slugs, files, source_slugs, links, has_orphans: bool) -> str:
    SELF = "index.md"
    total_mem = sum(len(c.members) for c in topic_clusters)

    def topic_line(c: Cluster) -> str:
        slug = topic_slugs.get(c.key)
        pin = " 📌" if _pin_count(c) else ""
        return f"- {links.link(slug, SELF, c.members[0].display_title)}{pin} " \
               f"({len(c.members)} memories)"

    lines = [
        _fm(["title: Index", "type: index"]),
        f"# {_LOGO}m3 Wiki",
        "",
        BANNER,
        "",
        f"Your knowledge, compiled: **{len(topic_clusters)} topics** covering "
        f"**{total_mem} memories**, plus **{len(files.files)} source documents**. "
        f"Start with the {links.link('overview', SELF, 'overview')}, or jump to a "
        "topic below.",
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
        lines.extend(topic_line(c) for c in starred)
        lines.append("")

    # Group by dominant type. Named sections (belief/procedure/…) get their own
    # heading; everything else folds into a single "Other topics" bucket so the
    # index isn't cluttered with one-item 📄 Fact / Note / Document sections.
    by_kind: dict[str, list[Cluster]] = {}
    for c in topic_clusters:
        by_kind.setdefault(_dominant_type(c), []).append(c)

    named = [k for k in by_kind if k in _TYPE_ORDER]
    other = [k for k in by_kind if k not in _TYPE_ORDER]

    for kind in sorted(named, key=lambda k: _TYPE_ORDER[k]):
        clusters_here = sorted(by_kind[kind], key=prominence)
        lines.append(f"## {_TYPE_LABEL[kind]}")
        lines.append("")
        lines.extend(topic_line(c) for c in clusters_here)
        lines.append("")

    if other:
        other_clusters = sorted(
            (c for k in other for c in by_kind[k]), key=prominence
        )
        lines.append("## 📄 Other topics")
        lines.append("")
        lines.extend(topic_line(c) for c in other_clusters)
        lines.append("")

    if files.files:
        lines.append(f"## 📁 Source documents ({len(files.files)})")
        lines.append("")
        for fn in files.files:
            slug = source_slugs.get(fn.uuid)
            lines.append(f"- {links.link(slug, SELF, fn.filename)}")
        lines.append("")

    lines.append("## Housekeeping")
    lines.append("")
    lines.append(f"- {links.link('about', SELF, 'About')} — what this vault is and how it's built")
    if has_orphans:
        lines.append(f"- {links.link('orphans', SELF, 'Orphans')} — core memories with no links yet")
    lines.append(f"- {links.link('lint', SELF, 'Lint')} — orphans, dangling links, contradictions")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_overview(clusters: list[Cluster], files: FilesLayer, links: "LinkResolver") -> str:
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
        _nav(links, "overview.md"),
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


def _render_about(links: "LinkResolver") -> str:
    """A self-documenting page: explains the vault to whoever opens it.

    Rendered as a vault-native page with real hyperlinks into the vault's own
    structure — so the wiki explains itself in any Markdown viewer.
    """
    SELF = "about.md"
    idx = links.link("index", SELF, "Index")
    ovr = links.link("overview", SELF, "Overview")
    lnt = links.link("lint", SELF, "Lint")
    body = f"""This vault was compiled by **m3** from your memory store — it is a
*projection*, not something you edit by hand. Re-run `m3 wiki generate` to refresh
it; your edits here would be overwritten.

## How to read it

- **{idx}** — the table of contents: a ⭐ *Start here* shortlist, then topics
  grouped by kind (Knowledge, Runbooks, Decisions, References).
- **{ovr}** — counts and your largest topics at a glance.
- **Topics** (`topics/`) — one page per cluster of related memories. Each carries
  its source `memory_ids`, confidence, a *Related topics* list, an *Evidence*
  section linking to the files a fact came from, and *Backlinks*.
- **Sources** (`sources/`) — one page per indexed document, with its summary and
  notable extracted facts.
- **{lnt}** — housekeeping: orphaned memories and contradictions (memories that
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
        f"# {_LOGO}About this wiki",
        "",
        BANNER,
        "",
        body,
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_lint(clusters, edges, mem_to_topic, topic_slugs, links: "LinkResolver") -> str:
    SELF = "lint.md"
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
        _nav(links, SELF),
        "",
        f"## Orphans ({len(orphans)})",
        "",
    ]
    for m in sorted(orphans, key=lambda m: m.rank_key()):
        lines.append(f"- {m.display_title} · `id:{m.id[:8]}`")
    lines.append("")
    lines.append(f"## Contradictions ({len(contradictions)})")
    lines.append("")

    def topic_ref(mid: str) -> str:
        slug = mem_to_topic.get(mid)
        if slug and links.has(slug):
            # Deep-link straight to the Members section of the topic.
            return links.link(slug, SELF, anchor="Members")
        return "_(orphan)_"

    for a, b in contradictions:
        lines.append(f"- `{a[:8]}` ({topic_ref(a)}) ⚔️ `{b[:8]}` ({topic_ref(b)})")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"
