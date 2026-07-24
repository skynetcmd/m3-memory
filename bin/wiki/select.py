"""Data loading for the wiki generator — pure reads over the memory DB.

Every function here takes an open sqlite3.Connection and returns plain dataclasses.
No path resolution, no embedder, no timestamps — so build_wiki() stays pure and the
determinism test can drive it from a fixture DB.

The "core set" is m3's three overlapping notions of a canonical memory:
    pinned = 1          — explicit "this is canon, never age it out"
    importance >= tau   — high-ranked
    type in (belief, procedure, reference, synthesis)
                        — already-consolidated distillations / curated refs
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

# Memory types that are themselves distillations / curated references, so they
# belong in the wiki regardless of importance.
CORE_TYPES = ("belief", "procedure", "reference", "synthesis")

# memory_relationships types, weighted for clustering. Higher = pulls harder
# toward "same topic page". `contradicts` is special: keep the two memories on
# one page but flag them (handled in render/lint), so it clusters strongly.
EDGE_WEIGHTS: dict[str, float] = {
    "consolidates": 3.0,
    "distills_from": 3.0,
    "supersedes": 2.5,
    "extends": 2.0,
    "contradicts": 2.0,   # co-locate both sides
    "supports": 1.5,
    "related": 1.0,
    "references": 1.0,
    "co_mentions": 1.0,   # synthetic entity-co-mention edge (see below)
    "precedes": 0.5,
    "follows": 0.5,
}

# Weight of a synthetic edge created because two core memories mention the same
# entity. Below `related` (1.0) so an explicit hand-authored link always dominates,
# but at/above the clustering bind threshold so co-mention alone can still group
# otherwise-orphan memories into a topic.
ENTITY_COMENTION_WEIGHT = 1.0

# An entity mentioned by more than this many core memories is too generic to be a
# useful topic signal (e.g. "m3", "user") — it would over-merge the whole graph.
# Skip it when building co-mention edges.
ENTITY_MAX_DEGREE = 8

# Entity TYPES that are non-discriminative for topic co-mention: they appear
# across unrelated topics and bridge them into one blob (a date, a file path, an
# env var, a port, a branch name, a generic module/table). Two memories sharing
# only "2026-04-10" or "pyproject.toml" or "M3_ENGINE_ROOT" are NOT about the same
# thing. Excluding these from co-mention is what keeps clusters coherent — a much
# sharper lever than lowering ENTITY_MAX_DEGREE (which would also drop the GOOD
# links from discriminative entities like a person, benchmark, model, or device).
# (Measured 2026-07-24: file_path/datetime/env_var/port/commit_or_branch were the
# top mergers that fused 40 unrelated notes into one "debugging" cluster.)
NON_DISCRIMINATIVE_ENTITY_TYPES = frozenset({
    # Structural / temporal noise — appear everywhere.
    "datetime", "file_path", "env_var", "port", "commit_or_branch",
    "module", "class_or_table", "memory_type", "task_id", "task_category",
    "bench_run_id", "migration", "mac_address", "memory_id",
    # Infrastructure addressing — a host/container/IP/VLAN/endpoint is shared by
    # every note that happens to touch that box, across unrelated topics.
    "host", "container", "ip_address", "vlan", "endpoint_url", "service",
    "protocol",
    # Model/tool names are too generic: "Opus", "gpt-4o-mini", "bge-m3" co-occur
    # across bench, infra, and feature notes alike. (Discriminative entities that
    # SURVIVE and rightly co-locate: person, benchmark, organization, metric.)
    "model", "variant", "device",
})

# Self-referential entity VALUES: the project's own name / repo / org identifiers.
# Type alone can't catch these — "skynetcmd/m3-memory" is an `organization`, and
# real external orgs of that type (Technitium, Mem0, PyPI) ARE discriminative and
# must survive. But the project's own repo/org means "this is an m3 note", not
# "this is about X", so it bridges everything. A VALUE-level exclusion, config-
# overridable per deployment (a different project has a different name).
# (Measured 2026-07-24: `skynetcmd/m3-memory` [org] + `m3-memory` [org] were the
# last bridges fusing a release note into the debugging cluster.)
_SELF_REF_CONFIG_BASENAME = ".wiki_self_entities.json"
_DEFAULT_SELF_REF_NAMES = frozenset({
    "m3-memory", "skynetcmd/m3-memory", "skynetcmd", "skynetCMD", "m3", "M3",
})


def _self_ref_names() -> "frozenset[str]":
    """Project self-reference entity names to exclude from co-mention. Default set
    plus an optional config file at M3_CONFIG_ROOT/.wiki_self_entities.json
    (`{"self_entities": ["myproj", "org/repo"]}`). Case-insensitive match. Config
    is a code-resolved FILE, not an env var (§3: headless launchers inherit no
    shell env). Malformed/absent → the default. Cached per config path."""
    import json as _json
    import os as _os
    root = (_os.environ.get("M3_CONFIG_ROOT")
            or (_os.path.join(_os.environ["M3_MEMORY_ROOT"], "config")
                if _os.environ.get("M3_MEMORY_ROOT") else None)
            or _os.path.join(_os.path.expanduser("~"), ".m3", "config"))
    path = _os.path.join(root, _SELF_REF_CONFIG_BASENAME)
    names = set(_DEFAULT_SELF_REF_NAMES)
    try:
        if _os.path.isfile(path):
            extra = _json.load(open(path, encoding="utf-8")).get("self_entities")
            if isinstance(extra, list):
                names.update(str(x) for x in extra)
    except Exception:
        pass  # config problems never break clustering
    return frozenset(n.lower() for n in names)


@dataclass
class Mem:
    """A core memory row, trimmed to what the wiki renders."""
    id: str
    type: str
    title: str
    content: str
    importance: float
    confidence: Optional[float]
    valid_from: Optional[str]
    valid_to: Optional[str]
    pinned: int
    created_at: Optional[str]
    updated_at: Optional[str]
    # Parsed metadata_json (already a dict via the seam's json_column_to_dict, so
    # a Postgres JSONB dict and a SQLite TEXT string both normalize here). Empty
    # dict when the column is absent or unparseable — never None, so callers need
    # no nil-check (§3 empty=zero, never None).
    metadata: dict = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        """Title, falling back to a trimmed content snippet, then the id."""
        if self.title and self.title.strip():
            return self.title.strip()
        body = (self.content or "").strip().splitlines()
        if body:
            first = body[0].strip()
            return (first[:60] + "…") if len(first) > 60 else first
        return f"memory {self.id[:8]}"

    def rank_key(self) -> tuple:
        """Deterministic sort: pinned first, then importance, then id."""
        return (0 if self.pinned else 1, -(self.importance or 0.0), self.id)


@dataclass
class Edge:
    from_id: str
    to_id: str
    rel: str

    @property
    def weight(self) -> float:
        return EDGE_WEIGHTS.get(self.rel, 1.0)


@dataclass
class Promo:
    """A promotion_marker: a files-DB item that became a memory row."""
    marker_uuid: str
    promoted_to: str          # memory_items.id
    source_memory: str        # files-DB fact/leaf/file_summary uuid
    source_memory_type: str   # 'fact' | 'leaf' | 'file_summary'
    filename: Optional[str]
    source_path: Optional[str]


@dataclass
class CoreSet:
    memories: list[Mem] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    @property
    def ids(self) -> set[str]:
        return {m.id for m in self.memories}


def _param() -> str:
    """The active backend's SQL placeholder ('?' on SQLite, '%s' on PostgreSQL).
    Falls back to '?' when the backend seam isn't importable (e.g. a standalone
    sqlite fixture in tests)."""
    try:
        from memory.backends import dialect
        return dialect().param()
    except Exception:
        return "?"


def _json_column_to_dict(value) -> dict:
    """Normalize a fetched JSON column to a dict via the backend seam.

    Routes through dialect.json_column_to_dict so a Postgres JSONB dict and a
    SQLite TEXT string both work — never a bare json.loads(), which TypeErrors on
    a pre-parsed JSONB dict. Falls back to a local safe parse only when the seam
    isn't importable (a standalone sqlite fixture), matching _param()'s pattern.
    Fails safe: anything unparseable / non-object yields {}.
    """
    try:
        from memory.backends import dialect
    except ImportError:
        # Seam not importable — a standalone sqlite fixture, definitionally TEXT.
        # Degrade with the SAME fail-safe contract the seam guarantees. Not a
        # backend branch: there is no backend here, only a raw sqlite column.
        return _local_json_to_dict(value)
    return dialect().json_column_to_dict(value)


def _local_json_to_dict(value) -> dict:
    """Fallback parse for when the backend seam isn't importable. Mirrors the
    seam's fail-safe contract: None/empty/malformed/non-object → {}."""
    if value is None or value == "" or value == b"":
        return {}
    if isinstance(value, dict):
        return value
    try:
        import json as _json
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        parsed = _json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _row_get(row, key):
    """Column value by name across a sqlite3.Row (dict-like) and a seam row, or
    None if the column is absent (e.g. the NULL AS ... alias on an older schema)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _set_dict_rows(conn) -> None:
    """Ensure column access by name works. On a raw sqlite3 connection we set the
    Row factory; on a backend-seam connection (PostgreSQL) rows already support
    name access, so this is a best-effort no-op there."""
    try:
        conn.row_factory = sqlite3.Row
    except (AttributeError, TypeError):
        pass  # non-sqlite connection from the backend seam — already name-addressable


def _has_column(conn, table: str, column: str) -> bool:
    """Backend-portable column existence check via the dialect's metadata query.

    Uses `dialect.columns_of()` — `pragma_table_info()` on SQLite,
    `information_schema.columns` on PostgreSQL — which lists column names WITHOUT
    erroring on a missing column. This matters on PostgreSQL: a failed probe query
    (e.g. `SELECT missing_col …`) aborts the whole transaction, poisoning every
    later query on the connection. A metadata query never does that. Falls back to
    `PRAGMA table_info` for a raw sqlite3 fixture with no backend seam (tests)."""
    try:
        from memory.backends import dialect
        sql, params = dialect().columns_of(table)
        return any(r[0] == column for r in conn.execute(sql, params).fetchall())
    except Exception:
        # No backend seam (standalone sqlite fixture): PRAGMA is safe on sqlite.
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            return any(
                (r["name"] if isinstance(r, sqlite3.Row) else r[1]) == column
                for r in rows
            )
        except Exception:
            return False


def select_core_memories(
    conn: sqlite3.Connection,
    *,
    importance_threshold: float = 0.6,
    limit: int = 5000,
    exclude_regex: Optional[str] = None,
) -> list[Mem]:
    """Load the canonical memory set, sorted deterministically.

    Resilient to older schemas: `pinned`/`confidence`/`valid_*` are accreted
    columns, so fall back to safe defaults if a column is absent (keeps the
    generator usable against a minimal fixture DB).

    `exclude_regex`, when set, drops any memory whose title OR content matches it
    (case-insensitive) — used to keep private/bench memories out of a shareable
    vault. Applied in Python (not SQL) so the pattern is a full regex.
    """
    import re as _re
    _excl = _re.compile(exclude_regex, _re.IGNORECASE) if exclude_regex else None
    has_pinned = _has_column(conn, "memory_items", "pinned")
    has_conf = _has_column(conn, "memory_items", "confidence")
    has_valid = _has_column(conn, "memory_items", "valid_from")
    has_meta = _has_column(conn, "memory_items", "metadata_json")

    pinned_sel = "pinned" if has_pinned else "0 AS pinned"
    conf_sel = "confidence" if has_conf else "NULL AS confidence"
    vfrom_sel = "valid_from" if has_valid else "NULL AS valid_from"
    vto_sel = "valid_to" if has_valid else "NULL AS valid_to"
    meta_sel = "metadata_json" if has_meta else "NULL AS metadata_json"

    p = _param()  # backend placeholder: '?' (SQLite) or '%s' (PostgreSQL)
    type_placeholders = ",".join([p] * len(CORE_TYPES))
    # The pinned predicate only references a real column when it exists. Compare
    # to a bound value (not literal 1) so it's valid whether `pinned` is INTEGER
    # (SQLite) or BOOLEAN (PostgreSQL).
    pinned_pred = f"pinned = {p} OR " if has_pinned else ""
    where_sql = (
        f"is_deleted = 0 AND ({pinned_pred}importance >= {p} "
        f"OR type IN ({type_placeholders}))"
    )

    sql = (
        f"SELECT id, type, title, content, importance, {conf_sel}, "
        f"       {vfrom_sel}, {vto_sel}, {pinned_sel}, created_at, updated_at, "
        f"       {meta_sel} "
        f"FROM memory_items WHERE {where_sql} "
        f"ORDER BY id LIMIT {p}"
    )
    params: list = []
    if has_pinned:
        params.append(1)              # pinned = 1 (truthy on both int and bool cols)
    params += [importance_threshold, *CORE_TYPES, limit]

    _set_dict_rows(conn)
    rows = conn.execute(sql, params).fetchall()
    _to_dict = _json_column_to_dict  # bind once (seam lookup is cached inside)
    out: list[Mem] = []
    for r in rows:
        if _excl is not None:
            hay = f"{r['title'] or ''}\n{r['content'] or ''}"
            if _excl.search(hay):
                continue
        out.append(
            Mem(
                id=r["id"],
                type=r["type"] or "note",
                title=r["title"] or "",
                content=r["content"] or "",
                importance=float(r["importance"] if r["importance"] is not None else 0.5),
                confidence=(float(r["confidence"]) if r["confidence"] is not None else None),
                valid_from=r["valid_from"],
                valid_to=r["valid_to"],
                pinned=int(r["pinned"] or 0),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                metadata=_to_dict(_row_get(r, "metadata_json")),
            )
        )
    out.sort(key=lambda m: m.rank_key())
    return out


def load_memory_edges(conn: sqlite3.Connection, ids: set[str]) -> list[Edge]:
    """Load memory_relationships edges whose BOTH endpoints are in the core set.

    Deterministically sorted. Edges to memories outside the core set are dropped
    (they'd render as dangling links); the lint pass reports those separately.
    """
    _set_dict_rows(conn)
    rows = conn.execute(
        "SELECT from_id, to_id, relationship_type FROM memory_relationships"
    ).fetchall()
    edges: list[Edge] = []
    for r in rows:
        fid, tid = r["from_id"], r["to_id"]
        if fid in ids and tid in ids and fid != tid:
            edges.append(Edge(from_id=fid, to_id=tid, rel=r["relationship_type"] or "related"))
    edges.sort(key=lambda e: (e.from_id, e.to_id, e.rel))
    return edges


def load_entity_comention_edges(conn: sqlite3.Connection, ids: set[str]) -> list[Edge]:
    """Synthesize weak binding edges between core memories that share an entity.

    m3 already extracts entities per memory (memory_item_entities). Two memories
    that mention the same specific entity ("M3_ENGINE_ROOT", "LongMemEval") almost
    always belong on/near the same page even without a hand-authored edge — this is
    what rescues the otherwise-large orphan set into real topics.

    Generic entities (mentioned by > ENTITY_MAX_DEGREE core memories) are skipped so
    a ubiquitous term doesn't collapse the whole graph into one blob. Edges are
    deterministic (sorted) and carry rel='co_mentions' at a sub-`related` weight.
    """
    if not ids:
        return []
    _set_dict_rows(conn)
    # Guard BEFORE running the query, not with try/except around it: on PostgreSQL
    # a query against a missing table aborts the whole transaction, poisoning the
    # connection for any later query. `_has_column` uses a safe metadata query.
    if not _has_column(conn, "memory_item_entities", "entity_id"):
        return []  # entity graph not present on this DB
    rows = conn.execute(
        "SELECT entity_id, memory_id FROM memory_item_entities"
    ).fetchall()

    # Non-discriminative entity types (dates, file paths, env vars, …) bridge
    # unrelated topics, so drop their entities from co-mention. Load the type map
    # if the entities table is present; if it isn't, degrade to no filtering
    # (behaviour unchanged on a DB without the entity catalog).
    excluded_ids: set[str] = set()
    if _has_column(conn, "entities", "entity_type"):
        self_names = _self_ref_names()
        has_name = _has_column(conn, "entities", "canonical_name")
        cols = "id, entity_type" + (", canonical_name" if has_name else "")
        for er in conn.execute(f"SELECT {cols} FROM entities").fetchall():
            # (a) non-discriminative TYPE, or (b) a self-referential project name.
            if er["entity_type"] in NON_DISCRIMINATIVE_ENTITY_TYPES:
                excluded_ids.add(er["id"])
            elif has_name and (er["canonical_name"] or "").lower() in self_names:
                excluded_ids.add(er["id"])

    # entity_id -> sorted list of core memory ids that mention it
    by_entity: dict[str, list[str]] = {}
    for r in rows:
        eid = r["entity_id"]
        if eid in excluded_ids:
            continue  # non-discriminative type — no cross-topic bridge
        mid = r["memory_id"]
        if mid in ids:
            by_entity.setdefault(eid, []).append(mid)

    pair_seen: set[tuple[str, str]] = set()
    edges: list[Edge] = []
    for _eid, members in by_entity.items():
        members = sorted(set(members))
        if len(members) < 2 or len(members) > ENTITY_MAX_DEGREE:
            continue
        # Connect the members into a chain (not a full clique) — a chain is enough
        # to place them in one connected component and keeps edge count linear.
        for a, b in zip(members, members[1:]):
            key = (a, b)
            if key in pair_seen:
                continue
            pair_seen.add(key)
            edges.append(Edge(from_id=a, to_id=b, rel="co_mentions"))
    edges.sort(key=lambda e: (e.from_id, e.to_id, e.rel))
    return edges


def load_promotions(files_conn: sqlite3.Connection, memory_ids: set[str]) -> list[Promo]:
    """Load promotion_markers whose target memory is in the core set.

    This is the cross-DB bridge: files-DB fact/leaf/summary -> memory row. Only
    keep markers pointing at a core memory (so Evidence links resolve). Sorted.
    """
    files_conn.row_factory = sqlite3.Row
    try:
        rows = files_conn.execute(
            "SELECT uuid, promoted_to, source_memory, source_memory_type "
            "FROM promotion_markers"
        ).fetchall()
    except sqlite3.OperationalError:
        # files DB may predate promotion_markers; degrade gracefully.
        return []

    # Enrich with filename/source_path via the source item where possible.
    out: list[Promo] = []
    for r in rows:
        if r["promoted_to"] not in memory_ids:
            continue
        filename, source_path = _resolve_source_file(files_conn, r["source_memory"], r["source_memory_type"])
        out.append(
            Promo(
                marker_uuid=r["uuid"],
                promoted_to=r["promoted_to"],
                source_memory=r["source_memory"],
                source_memory_type=r["source_memory_type"] or "",
                filename=filename,
                source_path=source_path,
            )
        )
    out.sort(key=lambda p: (p.promoted_to, p.marker_uuid))
    return out


def _resolve_source_file(
    conn: sqlite3.Connection, source_uuid: str, source_type: str
) -> tuple[Optional[str], Optional[str]]:
    """Best-effort filename + path for a promoted files-DB item."""
    try:
        if source_type == "fact":
            row = conn.execute(
                "SELECT fn.filename, fn.path_absolute FROM facts f "
                "JOIN file_nodes fn ON fn.uuid = f.file_node WHERE f.uuid = ?",
                (source_uuid,),
            ).fetchone()
        elif source_type == "leaf":
            row = conn.execute(
                "SELECT fn.filename, fn.path_absolute FROM leaves l "
                "JOIN file_nodes fn ON fn.uuid = l.file_node WHERE l.uuid = ?",
                (source_uuid,),
            ).fetchone()
        else:  # file_summary → source_uuid is the file_node
            row = conn.execute(
                "SELECT filename, path_absolute FROM file_nodes WHERE uuid = ?",
                (source_uuid,),
            ).fetchone()
    except sqlite3.OperationalError:
        return (None, None)
    if not row:
        return (None, None)
    return (row[0], row[1])
