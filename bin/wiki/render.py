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

from . import anchor as _anchor
from . import authority as _authority
from . import blast_radius as _blast
from . import citation_drift as _citation_drift
from .cluster import Cluster
from .files_layer import FileNode, FilesLayer
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
    """Resolves a page reference to a link, in one of two link formats.

    Default (`obsidian=False`): standard `[title](relpath.md)` Markdown links —
    they render as real hyperlinks in EVERY viewer (GitHub, browsers, the HTML
    viewer) and are clickable in Obsidian. Portable, but do NOT populate
    Obsidian's graph view / backlinks pane.

    Obsidian mode (`obsidian=True`, via `m3 wiki generate --obsidian`): emits
    `[[note-name|title]]` wikilinks so Obsidian's graph view and backlinks fully
    work. Obsidian resolves wikilinks by note *name* (the filename without its
    path/extension), which is our unique slug — so links resolve across the
    topics/ and sources/ folders without needing relative paths. These render as
    literal text outside Obsidian, so the mode is opt-in.
    """

    def __init__(self, obsidian: bool = False) -> None:
        self.obsidian = obsidian
        # ref -> (path_from_vault_root, display_title, note_name)
        self._reg: dict[str, tuple[str, str, str]] = {}

    def register(self, ref: str, path_from_root: str, title: str) -> None:
        # note_name = filename without dir/extension (Obsidian's link target).
        note_name = posixpath.splitext(posixpath.basename(path_from_root))[0]
        self._reg[ref] = (path_from_root, title, note_name)

    def has(self, ref: str) -> bool:
        return ref in self._reg

    def link(self, ref: str, src_path_from_root: str,
             text: Optional[str] = None, anchor: Optional[str] = None) -> str:
        """Link to `ref` from the page at `src_path_from_root`.

        `anchor` (a heading text or pre-slugged id) jumps to a section within the
        target page. Falls back to plain text if the target is unknown, so a
        dangling reference never emits a broken link.
        """
        entry = self._reg.get(ref)
        if not entry:
            return text or ref
        target, title, note_name = entry
        label = text or title
        if self.obsidian:
            # [[note-name#Section|label]] — Obsidian keeps section headings as
            # given (not slugged), so pass the raw anchor text through. Drop the
            # alias when it equals the note-name (Obsidian shows [[x]] cleaner).
            frag = f"#{anchor}" if anchor else ""
            body = f"{note_name}{frag}"
            if label == note_name and not frag:
                return f"[[{body}]]"
            return f"[[{body}|{_esc_wikilabel(label)}]]"
        src_dir = posixpath.dirname(src_path_from_root)
        rel = posixpath.relpath(target, src_dir or ".")
        frag = f"#{heading_anchor(anchor)}" if anchor else ""
        return f"[{_esc_label(label)}]({_url_quote(rel)}{frag})"


def _esc_label(s: str) -> str:
    # Markdown link text: escape brackets that would break the [ ]( ) syntax.
    return (s or "").replace("[", "\\[").replace("]", "\\]")


def _esc_wikilabel(s: str) -> str:
    # Obsidian wikilink alias: '|' separates target from alias and ']]' closes
    # the link, so neither may appear in the label. Replace with safe lookalikes.
    return (s or "").replace("|", "∣").replace("]]", "] ]")


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
    obsidian: bool = False,
    drift_judge=None,
) -> dict[str, str]:
    """Build the full vault as {relpath: markdown}.

    `ledes` maps cluster.key -> a prose summary (from optional synthesis). When a
    cluster has no lede, its page falls back to the deterministic member list.
    `obsidian` switches cross-page links to `[[wikilinks]]` (see LinkResolver).
    `drift_judge` (optional) enables the citation-drift lint section (4b). When
    None (the default) the section is omitted entirely and the vault is
    byte-identical — the judge is model-backed and non-deterministic, so it is
    kept OUT of the drift-tested surface, exactly like `synthesizer`.
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
    links = LinkResolver(obsidian=obsidian)
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
    pages["lint.md"] = _render_lint(clusters, edges, mem_to_topic, topic_slugs,
                                    links, drift_judge=drift_judge)
    pages["about.md"] = _render_about(links)

    return pages


def _topic_synthesis(c: Cluster) -> "Optional[Mem]":
    """The synthesis member whose compiled prose should be the topic body, or
    None. Deterministic: prefer a body-authority synthesis (canonical), then the
    one whose id sorts last (the current head, since a supersede mints a new id).
    A cluster with no synthesis member returns None — non-synthesis topics render
    exactly as before."""
    syns = [m for m in c.members if m.type == "synthesis"]
    if not syns:
        return None
    # Body-eligible (authority passes the gate) beats gated; within each group the
    # highest id wins (stable, and the head of a supersede chain).
    syns.sort(key=lambda m: (_authority.renders_as_body(m.metadata), m.id))
    return syns[-1]


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

    # Compiled synthesis body: if this topic has a synthesis member, surface its
    # full compiled prose as the PAGE BODY (not merely one bullet in the member
    # list). This is the point of compile-at-ingest — a topic that was compiled
    # reads as a coherent page, with the source members kept below as provenance.
    # Honors the S6 authority gate: a provisional/restricted synthesis shows its
    # withholding marker instead of the prose, exactly like the member-list gate.
    syn = _topic_synthesis(c)
    if syn is not None:
        if _authority.renders_as_body(syn.metadata):
            body = (syn.content or "").strip()
            if body:
                lines.append(body)
                lines.append("")
        else:
            marker = _authority.render_marker(syn.metadata)
            if marker:
                lines.append(f"> {marker}")
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
        lines.append(f"- **{m.display_title}**{pin} · `{m.type}` · conf {_conf(m)} "
                     f"· `id:{m.id[:8]}`")
        # Authority gate (S6): a synthesis renders its content as body prose ONLY
        # when its authority is in the configured body set AND it is not GDPR-
        # restricted. Otherwise show a marker and withhold the content — a
        # provisional/unknown/restricted page must not read as authoritative.
        # Non-synthesis members are unaffected (renders_as_body is vacuously the
        # old behavior for them since they carry no authority — but we only gate
        # the 'synthesis' type to avoid changing any existing page).
        if m.type == "synthesis" and not _authority.renders_as_body(m.metadata):
            marker = _authority.render_marker(m.metadata)
            if marker:
                lines.append(f"  {marker}")
            continue
        head = m.content.strip().splitlines()[0].strip() if m.content.strip() else ""
        snippet = (head[:200] + "…") if len(head) > 200 else head
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
        # Orphans are title-only (no content leaks here regardless), but a
        # restricted/withheld synthesis must still SHOW its status so a reviewer
        # scanning orphans sees the legal-hold, not a silent plain row.
        if m.type == "synthesis" and not _authority.renders_as_body(m.metadata):
            marker = _authority.render_marker(m.metadata)
            if marker:
                lines.append(f"  {marker}")
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
    # Placed second deliberately: _TYPE_ORDER (derived from this list) is the
    # deterministic tiebreaker in _dominant_type(). A cluster tied between
    # 'synthesis' and a later type classifies as a synthesis — correct, since a
    # synthesis is compiled ABOUT its co-members. Appended last it would lose
    # every tie and never title a topic.
    ("synthesis", "📝 Compiled syntheses"),
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

    # Obsidian guidance depends on which link format this vault was built with.
    if links.obsidian:
        obsidian_note = """This vault was generated with `--obsidian`, so cross-page
links are `[[wikilinks]]` — Obsidian's **graph view** and **backlinks** pane work.
Open the folder with **Open folder as vault**.

Treat the vault as **read-only and disposable**: don't edit these pages and don't
link to them from notes you want to keep — a page can change, move, or disappear on
the next generation (see below). Keep your own notes in a *separate* folder or vault.
"""
    else:
        obsidian_note = """Open this folder in Obsidian with **Open folder as vault**
— pages are clickable immediately. Note that links here are standard Markdown, which
Obsidian follows but does **not** use to build its graph view or backlinks pane. For
those, regenerate with `m3 wiki generate --obsidian` (emits `[[wikilinks]]`).

Treat the vault as **read-only and disposable**: don't edit these pages and don't
link to them from notes you want to keep — a page can change, move, or disappear on
the next generation (see below). Keep your own notes in a *separate* folder or vault.
"""

    body = f"""This vault is a **regenerated snapshot** of what m3 currently
remembers — a *projection* of the memory store, not a document you build on. Pages
can change, move, or **disappear** between generations: a memory may be superseded,
soft-deleted, or permanently erased (GDPR right-to-be-forgotten), and re-running
`m3 wiki generate` prunes any page whose memory no longer exists. So don't edit
these pages or link to them from notes you care about — regenerate for the current
state, and keep durable personal notes elsewhere.

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

## For Obsidian users
{obsidian_note}
## Regenerating

```
m3 wiki generate                 # refresh this vault
m3 wiki generate --obsidian      # [[wikilinks]] for Obsidian graph view + backlinks
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


def _render_lint(clusters, edges, mem_to_topic, topic_slugs, links: "LinkResolver",
                 drift_judge=None) -> str:
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

    # Blast radius (4a): syntheses transitively compiled FROM a synthesis judged
    # wrong. Deterministic graph walk over the edges already in hand — no model,
    # no new query. A file-based wiki can't compute this; m3's provenance edges
    # can. Section is always emitted (count 0 when clean) so its absence never
    # reads as "not checked".
    # Restricted (GDPR Art. 17): syntheses whose source was erased. `restricted`
    # already halts rendering per topic; this section surfaces the full review
    # queue in one place (plan G3.5 — "never only a column") so a reviewer can
    # find every page awaiting a derivability decision. Deterministic metadata
    # scan, no model, no query.
    restricted = []
    for c in clusters:
        for m in c.members:
            if m.type == "synthesis" and _authority.is_restricted(m.metadata):
                recs = (m.metadata.get("review") or {}).get("erasure_records") or []
                restricted.append((m, len(recs)))
    lines.append(f"## Restricted — GDPR review ({len(restricted)})")
    lines.append("")
    if restricted:
        lines.append("_Source memories were erased (Art. 17). These pages are "
                     "withheld from rendering pending a derivability review — the "
                     "prose may be unaffected if the erased member was redundant._")
        lines.append("")
        for m, nrec in sorted(restricted, key=lambda t: t[0].rank_key()):
            plural = "erasure" if nrec == 1 else "erasures"
            lines.append(f"- {m.display_title} · `id:{m.id[:8]}` · "
                         f"{nrec} {plural}")
    else:
        lines.append("_No synthesis is restricted; no erased sources to review._")
    lines.append("")

    wrong = sorted(_blast.wrong_ids(clusters))
    tainted = sorted(_blast.contaminated(clusters, edges))
    lines.append(f"## Blast radius ({len(tainted)})")
    lines.append("")
    if wrong:
        lines.append(f"_{len(wrong)} synthesis(es) judged wrong; "
                     f"{len(tainted)} downstream synthesis(es) may carry the error._")
        lines.append("")
        for mid in tainted:
            lines.append(f"- `{mid[:8]}` ({topic_ref(mid)}) — compiled from a "
                         f"wrong source; re-check")
    else:
        lines.append("_No synthesis is marked wrong; nothing downstream to flag._")
    lines.append("")

    # Citation drift (4b, opt-in). Omitted entirely when no judge is injected, so
    # the default vault is byte-identical (the judge is model-backed and
    # non-deterministic — kept out of the drift-tested surface).
    report = _citation_drift.check_drift(clusters, edges, drift_judge)
    drift_lines = _citation_drift.render_section(report, links, SELF)
    if drift_lines:
        lines.extend(drift_lines)

    # Knowledge Anchor Report: KAS + coverage / staleness / redundancy. Pure,
    # deterministic (no model, no new query), so it stays in the drift test.
    lines.extend(_render_anchor_report(clusters, edges))
    return "\n".join(lines).rstrip() + "\n"


def _render_anchor_report(clusters, edges) -> list:
    """The Knowledge Anchor Report lint section: which topics are anchored vs.
    adrift (KAS), whether the wiki is representative (coverage), current
    (staleness), and non-redundant. All deterministic graph math."""
    from .select import EDGE_WEIGHTS
    h = _anchor.build_health(clusters, edges, EDGE_WEIGHTS)
    L = ["## Knowledge Anchor Report", ""]
    L.append(f"_Coverage {h.coverage:.0%}: {h.covered} high-value memories in real "
             f"topics, {h.orphaned_high_value} stranded as orphans._")
    L.append("")
    # Low-anchor / adrift topics (the headline signal).
    L.append(f"### Low anchor ({len(h.low_anchor)})")
    L.append("")
    if h.low_anchor:
        L.append("_Topics held together weakly or only by co-mention — candidates "
                 "for a tighter entity filter or a manual split._")
        L.append("")
        by_key = {a.key: a for a in h.anchors}
        for key in h.low_anchor:
            a = by_key.get(key)
            if not a:
                continue
            drift = (f" · bridged by: {', '.join(a.drift_entities)}"
                     if a.drift_entities else "")
            tag = "adrift" if a.adrift else f"KAS {a.kas}"
            L.append(f"- {a.title} · `{tag}` · {a.members} members "
                     f"({a.load_bearing_edges} real / {a.comention_edges} co-mention){drift}")
    else:
        L.append("_Every topic is anchored by real connections._")
    L.append("")
    # Stale topics.
    if h.stale:
        L.append(f"### Stale sources ({len(h.stale)})")
        L.append("")
        L.append("_Topics whose source memories are mostly superseded/aged — the "
                 "page may present outdated knowledge._")
        L.append("")
        slug = {c.key: c for c in clusters}
        for key, frac in h.stale:
            title = slug[key].members[0].display_title if slug.get(key) else key
            L.append(f"- {title} · {frac:.0%} stale")
        L.append("")
    # Redundant pairs.
    if h.redundant_pairs:
        L.append(f"### Redundant topics ({len(h.redundant_pairs)})")
        L.append("")
        L.append("_Topic pairs with high member overlap — likely one topic split "
                 "in two._")
        L.append("")
        for a_key, b_key, ov in h.redundant_pairs:
            L.append(f"- `{a_key[:8]}` ⇄ `{b_key[:8]}` · {ov:.0%} overlap")
        L.append("")
    return L
