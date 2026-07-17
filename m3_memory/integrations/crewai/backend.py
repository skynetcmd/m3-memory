"""``M3StorageBackend`` — m3 as a CrewAI v1.x memory ``StorageBackend``.

Implements the ``crewai.memory.storage.backend.StorageBackend`` Protocol (v1.10+)
over m3's one canonical in-process dispatch — no HTTP, no subprocess, no mem0.
Reuses the framework-agnostic ``M3Client`` from the langchain adapter (it has zero
langchain coupling) as the shared dispatch core (§2 narrow seam; §4 pool reuse).

THE m3 EDGE — cross-agent memory (dual-embed on write):
CrewAI embeds with its OWN embedder and hands ``save`` records that already carry
``.embedding`` (a query vector on ``search``). If m3 stored ONLY that vector,
every other m3 agent (bge-m3, 1024-dim) would be blind to CrewAI's memories — the
identity guard correctly excludes an incompatible vector space. So on save m3
writes the memory ONCE and embeds it TWICE:
  * m3's native bge-m3 vector, via the normal async backfill (memory_write with
    embed=True defers, embed_backfill.py fills it) — every other m3 agent finds it;
  * CrewAI's supplied vector, stored under a per-dim ``embed_model`` identity via a
    direct memory_embeddings insert — CrewAI's own search finds it.
One memory, two doors. m3's multi-embedding ``memory_embeddings`` schema
(vector_kind, migration 022) was built for exactly this; a single-vector store
(LanceDB/Qdrant/mem0) cannot do it.

TENANCY (§7): CrewAI's contract carries no user_id. m3 mandates tenancy on every
query. Resolution: the tenant (``user_id``) is fixed at CONSTRUCTION and stamped
on every save/search; a missing tenant RAISES (§3 crash-on-contract). CrewAI's own
``scope_prefix`` path maps to a sub-scope WITHIN that tenant (in metadata_json),
never widening it.

METHOD COVERAGE (verified against CrewAI 1.15.3 call sites): the sync methods
CrewAI actually calls are implemented fully (several crash the crew if they
raise). ``count`` is never called and the async trio delegate to sync (CrewAI's
async Memory methods delegate to sync in 1.15.3) — kept cheap, not absent, so they
are free if wired up later.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..langchain.m3client import M3Client
from . import mapping

_logger = logging.getLogger("m3_memory.crewai")

# m3's category scope for CrewAI-written memories. user_id is the tenancy key;
# scope is a bounded category {agent,user,session,org} — "user" pairs with a
# per-crew user_id so writes land where reads look (§7).
_M3_SCOPE = "user"


class M3StorageBackend:
    """m3-backed CrewAI ``StorageBackend``. Wire via ``Memory(storage=...)``.

        from m3_memory.crewai import M3StorageBackend
        from crewai.memory import Memory
        mem = Memory(storage=M3StorageBackend(user_id="crew-alpha"))

    ``user_id`` is REQUIRED (m3 enforces per-tenant isolation — there is no
    anonymous/global mode). ``dual_embed=False`` opts out of the m3-native bge-m3
    pass (CrewAI-space only — for deployments that never use non-CrewAI agents).
    """

    def __init__(
        self,
        user_id: str,
        *,
        dual_embed: bool = True,
        call_timeout: float = 30.0,
    ) -> None:
        if not user_id or not str(user_id).strip():
            raise ValueError(
                "M3StorageBackend requires a non-empty user_id — m3 enforces "
                "per-tenant isolation (DESIGN_PHILOSOPHIES §7); there is no "
                "anonymous/global mode. Pass one backend per crew/tenant, e.g. "
                "M3StorageBackend(user_id='crew-alpha')."
            )
        self._user_id = str(user_id).strip()
        self._dual_embed = dual_embed
        self._client = M3Client(agent_id="crewai", call_timeout=call_timeout)

    # ── the CrewAI StorageBackend protocol (sync — the exercised path) ─────────

    def save(self, records: list) -> None:
        """Persist a batch of MemoryRecords (CrewAI calls this, never asave).

        Per record: one m3 ``memory_write`` (bge-m3 embed queued async — cross-
        agent searchable), then a direct insert of CrewAI's own vector under its
        per-dim identity, then an async ``chatlog_write`` for m3's Observer deep
        extraction (the free upgrade). Records may arrive with ``embedding=None``
        if CrewAI's embed failed — then only the m3-native vector is written.
        """
        for record in records or []:
            args = mapping.record_to_write_args(
                record, user_id=self._user_id, scope=_M3_SCOPE
            )
            raw = self._client._tool(
                "memory_write",
                type=args["type"],
                content=args["content"],
                user_id=args["user_id"],
                scope=args["scope"],
                importance=args["importance"],
                metadata=args["metadata"],
                source=args["source"],
                embed=self._dual_embed,  # queues m3's bge-m3 vector (async backfill)
                auto_classify=True,
            )
            new_id = _parse_written_id(raw)
            emb = getattr(record, "embedding", None)
            if new_id and emb:
                self._store_crewai_vector(new_id, list(emb))
            # Observer/Reflector deep extraction (best-effort, never blocks save).
            if new_id:
                self._enqueue_observer(record, new_id)

    def search(
        self,
        query_embedding: list,
        scope_prefix: "str | None" = None,
        categories: "list[str] | None" = None,
        metadata_filter: "dict[str, Any] | None" = None,
        limit: int = 10,
        min_score: float = 0.0,
    ) -> list:
        """Vector search against CrewAI-space vectors → ``list[(record, score)]``.

        CrewAI hands us a precomputed ``query_embedding`` (it embeds upstream), so
        this scores directly against the CrewAI-identity vectors via m3's
        ``vector_search`` seam — never re-embeds. ``scope_prefix``/``categories``
        are applied as post-filters (they ride metadata_json). ``metadata_filter``
        is accepted but unused (CrewAI never passes it in 1.15.3). Score is cosine,
        HIGHER = better (CrewAI's ``compute_composite_score`` blends it with
        recency+importance downstream, so this is an input, not the final rank).
        """
        if not query_embedding:
            return []
        from crewai.memory.types import MemoryRecord

        prefix = mapping.normalize_scope_prefix(scope_prefix)
        rows = self._client._run(
            self._search_async(list(query_embedding), limit, min_score)
        )
        out: list = []
        cat_set = set(categories or [])
        for score, item in rows:
            rec = mapping.item_to_record(item, record_cls=MemoryRecord)
            if not mapping.scope_matches(rec.scope, prefix):
                continue
            if cat_set and not (cat_set & set(rec.categories)):
                continue
            out.append((rec, float(score)))
        return out[:limit]

    def delete(
        self,
        scope_prefix: "str | None" = None,
        categories: "list[str] | None" = None,
        record_ids: "list[str] | None" = None,
        older_than: "datetime | None" = None,
        metadata_filter: "dict[str, Any] | None" = None,
    ) -> int:
        """Delete memories matching the criteria; returns the count deleted.

        ``record_ids`` is the direct path (CrewAI ``forget``). Scope/older_than
        deletes resolve matching ids first, then soft-delete via m3 (bi-temporal —
        the row is closed, not destroyed; §9). Empty criteria deletes nothing
        (never a global wipe — that is ``reset``).
        """
        ids = list(record_ids or [])
        if not ids and (scope_prefix or categories or older_than):
            ids = [
                item.get("id")
                for _s, item in self._client._run(self._list_all_async())
                if _match_for_delete(
                    item, scope_prefix, categories, older_than
                )
            ]
            ids = [i for i in ids if i]
        if not ids:
            return 0
        raw = self._client._tool("memory_delete_bulk", ids=ids)
        return _deleted_count(raw, fallback=len(ids))

    def update(self, record: Any) -> None:
        """Replace the record with the same id — via m3 supersede (a real
        contradiction-aware edge, bi-temporal; the m3 differentiator over a
        flat overwrite). No-op-safe if the id is unknown."""
        rid = str(getattr(record, "id", "") or "")
        if not rid:
            return
        content = str(getattr(record, "content", "") or "")
        md = mapping.record_to_write_args(
            record, user_id=self._user_id, scope=_M3_SCOPE
        )["metadata"]
        self._client._tool(
            "memory_supersede",
            old_id=rid,
            content=content,
            user_id=self._user_id,
            scope=_M3_SCOPE,
            metadata=md,
        )

    def get_record(self, record_id: str) -> Any:
        """Return one record by id, or None (CrewAI reads this before update)."""
        if not record_id:
            return None
        from crewai.memory.types import MemoryRecord

        item = self._client._run(self._get_item_async(str(record_id)))
        if item is None:
            return None
        return mapping.item_to_record(item, record_cls=MemoryRecord)

    def list_records(
        self, scope_prefix: "str | None" = None, limit: int = 200, offset: int = 0
    ) -> list:
        """Records in a scope, newest first (introspection: Memory.list_records)."""
        from crewai.memory.types import MemoryRecord

        prefix = mapping.normalize_scope_prefix(scope_prefix)
        rows = self._client._run(self._list_all_async())
        recs = [mapping.item_to_record(it, record_cls=MemoryRecord) for _s, it in rows]
        recs = [r for r in recs if mapping.scope_matches(r.scope, prefix)]
        recs.sort(key=lambda r: r.created_at, reverse=True)
        return recs[offset : offset + limit]

    def get_scope_info(self, scope: str) -> Any:
        """ScopeInfo for a scope path: count, categories, date range, children
        (Memory.info()/tree()/deep-recall query analysis all call this)."""
        from crewai.memory.types import MemoryRecord, ScopeInfo

        prefix = mapping.normalize_scope_prefix(scope)
        rows = self._client._run(self._list_all_async())
        recs = [
            mapping.item_to_record(it, record_cls=MemoryRecord)
            for _s, it in rows
            if mapping.scope_matches(
                mapping._loads_metadata(it.get("metadata_json")).get(
                    mapping.SCOPE_PATH_KEY, "/"
                ),
                prefix,
            )
        ]
        cats: set = set()
        children: set = set()
        oldest = newest = None
        for r in recs:
            cats.update(r.categories)
            oldest = r.created_at if oldest is None else min(oldest, r.created_at)
            newest = r.created_at if newest is None else max(newest, r.created_at)
            child = _immediate_child(prefix, r.scope)
            if child:
                children.add(child)
        return ScopeInfo(
            path=prefix or "/",
            record_count=len(recs),
            categories=sorted(cats),
            oldest_record=oldest,
            newest_record=newest,
            child_scopes=sorted(children),
        )

    def list_scopes(self, parent: str = "/") -> list:
        """Immediate child scope paths under ``parent`` (not the full subtree)."""
        prefix = mapping.normalize_scope_prefix(parent)
        rows = self._client._run(self._list_all_async())
        children: set = set()
        for _s, it in rows:
            sp = mapping._loads_metadata(it.get("metadata_json")).get(
                mapping.SCOPE_PATH_KEY, "/"
            )
            if mapping.scope_matches(sp, prefix):
                child = _immediate_child(prefix, sp)
                if child:
                    children.add(child)
        return sorted(children)

    def list_categories(self, scope_prefix: "str | None" = None) -> dict:
        """Category → record-count within a scope (CrewAI field inference)."""
        prefix = mapping.normalize_scope_prefix(scope_prefix)
        rows = self._client._run(self._list_all_async())
        counts: dict = {}
        for _s, it in rows:
            md = mapping._loads_metadata(it.get("metadata_json"))
            if not mapping.scope_matches(md.get(mapping.SCOPE_PATH_KEY, "/"), prefix):
                continue
            for c in md.get(mapping.CATEGORIES_KEY, []) or []:
                counts[c] = counts.get(c, 0) + 1
        return counts

    def count(self, scope_prefix: "str | None" = None) -> int:
        """Record count in scope. (Not called by CrewAI 1.15.3; implemented for
        completeness so the protocol surface is whole.)"""
        prefix = mapping.normalize_scope_prefix(scope_prefix)
        rows = self._client._run(self._list_all_async())
        return sum(
            1
            for _s, it in rows
            if mapping.scope_matches(
                mapping._loads_metadata(it.get("metadata_json")).get(
                    mapping.SCOPE_PATH_KEY, "/"
                ),
                prefix,
            )
        )

    def reset(self, scope_prefix: "str | None" = None) -> None:
        """Delete all memories in scope (None = this tenant's memories). Scoped to
        the backend's user_id — never crosses tenants (§7)."""
        prefix = mapping.normalize_scope_prefix(scope_prefix)
        ids = [
            it.get("id")
            for _s, it in self._client._run(self._list_all_async())
            if mapping.scope_matches(
                mapping._loads_metadata(it.get("metadata_json")).get(
                    mapping.SCOPE_PATH_KEY, "/"
                ),
                prefix,
            )
        ]
        ids = [i for i in ids if i]
        if ids:
            self._client._tool("memory_delete_bulk", ids=ids)

    def touch_records(self, record_ids: list) -> None:
        """Refresh ``last_accessed`` for the given records (CrewAI calls this via
        getattr after every recall to feed recency scoring). m3 already tracks
        last_accessed_at + access_count for its confidence dynamics, so this bumps
        the same signal — CrewAI's recency ranking improves over time on m3 in a
        way a bare backend can't. Best-effort; never raises to the caller."""
        ids = [str(i) for i in (record_ids or []) if i]
        if not ids:
            return
        try:
            self._client._run(self._touch_async(ids))
        except Exception:
            pass  # ranking-quality signal only; never break recall

    # ── async trio: CrewAI 1.15.3 delegates async→sync, so mirror that ────────
    async def asave(self, records: list) -> None:
        self.save(records)

    async def asearch(self, query_embedding: list, **kwargs: Any) -> list:
        return self.search(query_embedding, **kwargs)

    async def adelete(self, **kwargs: Any) -> int:
        return self.delete(**kwargs)

    # ── internals (run on M3Client's shared loop-thread) ──────────────────────

    def _store_crewai_vector(self, memory_id: str, vec: list) -> None:
        """Direct insert of CrewAI's precomputed vector under its per-dim identity.

        memory_write has no param for a caller-supplied vector (it embeds
        internally), so the CrewAI-space vector goes via a direct memory_embeddings
        insert — mirroring write.py's own insert shape — tagged crewai-<dim> so the
        search path selects exactly this space. A failure here leaves the m3-native
        vector intact (still searchable by other m3 agents) but breaks CrewAI's OWN
        recall of this row — so it is LOGGED loudly (§3: never silent), not
        swallowed, even though it doesn't abort the save."""
        try:
            self._client._run(self._insert_vector_async(memory_id, vec))
        except Exception as e:
            _logger.warning(
                "M3StorageBackend: failed to store CrewAI vector for %s (%s: %s) — "
                "memory saved and searchable by other m3 agents, but CrewAI's own "
                "recall will miss it until fixed.",
                memory_id, type(e).__name__, e,
            )

    async def _insert_vector_async(self, memory_id: str, vec: list) -> None:
        import uuid

        from embedding_utils import pack as _pack
        from memory.backends import dialect
        from memory.db import _db
        from memory.textprep import _content_hash

        dim = len(vec)
        model = mapping.crewai_embed_model(dim)
        _d = dialect()
        now = _d.now()
        # Fetch the content to hash (matches write.py's content_hash-per-vector).
        with _db() as db:
            row = db.execute(
                f"SELECT content FROM memory_items WHERE id = {_d.param()}",
                (memory_id,),
            ).fetchone()
            content = (row[0] if row else "") or ""
            db.execute(
                "INSERT INTO memory_embeddings "
                "(id, memory_id, embedding, embed_model, dim, created_at, "
                f"content_hash, vector_kind) VALUES ({_d.placeholder(7)}, {now})",
                (
                    str(uuid.uuid4()), memory_id, _pack([float(x) for x in vec]),
                    model, dim, _content_hash(content), "crewai",
                ),
            )
            db.commit()

    async def _search_async(
        self, query_vector: list, limit: int, min_score: float
    ) -> list:
        """Score a provided query vector against CrewAI-space vectors, hydrate rows."""
        from memory.backends import active_backend
        from memory.db import _db

        backend = active_backend()
        dim = len(query_vector)
        model = mapping.crewai_embed_model(dim)
        _p = backend.dialect().param()
        # Tenant scope filter composed into the seam (§7).
        tenancy_sql = f" AND mi.user_id = {_p}"
        tenancy_params = (self._user_id,)
        out: list = []
        with _db() as conn:
            hits = backend.vector_search(
                conn, [float(x) for x in query_vector], limit=max(limit * 4, limit),
                dim=dim, embed_models=(model,),
                tenancy_sql=tenancy_sql, tenancy_params=tenancy_params,
            )
            if not hits:
                return []
            by_id = {h.memory_id: h.score for h in hits}
            ids = list(by_id)
            ph = ", ".join([_p] * len(ids))
            cur = conn.execute(
                "SELECT id, content, importance, created_at, metadata_json, user_id "
                f"FROM memory_items WHERE id IN ({ph}) "
                "AND (is_deleted IS NULL OR is_deleted = 0)",
                tuple(ids),
            )
            for r in cur.fetchall():
                item = dict(r) if not isinstance(r, dict) else r
                score = by_id.get(item["id"], 0.0)
                if score < min_score:
                    continue
                out.append((score, item))
        out.sort(key=lambda p: p[0], reverse=True)
        return out

    async def _list_all_async(self) -> list:
        """This tenant's live memories as (score=0, item) pairs (deterministic,
        no embedding dependency — mirrors the langchain adapter's list path)."""
        from memory.db import _db

        _p = _import_param()
        rows: list = []
        with _db() as db:
            cur = db.execute(
                "SELECT id, content, importance, created_at, last_accessed_at, "
                "metadata_json, user_id FROM memory_items "
                f"WHERE user_id = {_p} AND scope = {_p} "
                "AND (is_deleted IS NULL OR is_deleted = 0) "
                "ORDER BY created_at DESC LIMIT 5000",
                (self._user_id, _M3_SCOPE),
            )
            for r in cur.fetchall():
                rows.append((0.0, dict(r) if not isinstance(r, dict) else r))
        return rows

    async def _get_item_async(self, record_id: str) -> "dict | None":
        from memory.db import _db

        _p = _import_param()
        with _db() as db:
            r = db.execute(
                "SELECT id, content, importance, created_at, last_accessed_at, "
                "metadata_json, user_id FROM memory_items "
                f"WHERE id = {_p} AND user_id = {_p} "
                "AND (is_deleted IS NULL OR is_deleted = 0)",
                (record_id, self._user_id),
            ).fetchone()
            return (dict(r) if r is not None and not isinstance(r, dict) else r) or None

    async def _touch_async(self, ids: list) -> None:
        from memory.backends import dialect
        from memory.db import _db

        _d = dialect()
        ph = ", ".join([_d.param()] * len(ids))
        with _db() as db:
            db.execute(
                f"UPDATE memory_items SET last_accessed_at = {_d.now()} "
                f"WHERE id IN ({ph}) AND user_id = {_d.param()}",
                (*ids, self._user_id),
            )
            db.commit()

    def _enqueue_observer(self, record: Any, memory_id: str) -> None:
        """Best-effort async chatlog_write for m3's Observer/Reflector deep
        extraction (entities/relationships) on the CrewAI memory — the free
        upgrade. Never blocks or fails the save."""
        try:
            self._client._tool(
                "chatlog_write",
                content=str(getattr(record, "content", "") or ""),
                user_id=self._user_id,
                host_agent="crewai",
                provider="crewai",
                model_id="crewai",
            )
        except Exception:
            pass


# ── module-level pure helpers (hermetically testable) ─────────────────────────

def _import_param() -> str:
    from memory.backends import dialect

    return dialect().param()


def _parse_written_id(raw: Any) -> "str | None":
    """Extract the new memory id from memory_write's ``Created: <uuid> ...`` return."""
    import re

    if not raw:
        return None
    s = str(raw).strip()
    if s.startswith("Error"):
        return None
    m = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        s,
    )
    return m.group(0) if m else None


def _deleted_count(raw: Any, *, fallback: int) -> int:
    """Best-effort count from a delete_bulk return; fallback to the id count."""
    if isinstance(raw, dict):
        for k in ("deleted", "count", "n"):
            if isinstance(raw.get(k), int):
                return raw[k]
    if isinstance(raw, int):
        return raw
    return fallback


def _immediate_child(prefix: str, scope_path: str) -> "str | None":
    """The immediate child path of ``prefix`` on the way to ``scope_path``, or None.

    ``_immediate_child("/crew", "/crew/research/facts") -> "/crew/research"``.
    ``_immediate_child("/crew", "/crew") -> None`` (it's the node itself).
    """
    base = prefix or ""
    if not scope_path.startswith(base):
        return None
    rest = scope_path[len(base):].lstrip("/")
    if not rest:
        return None
    first = rest.split("/", 1)[0]
    return f"{base}/{first}" if base else f"/{first}"


def _match_for_delete(
    item: dict,
    scope_prefix: "str | None",
    categories: "list[str] | None",
    older_than: "datetime | None",
) -> bool:
    md = mapping._loads_metadata(item.get("metadata_json"))
    if scope_prefix:
        if not mapping.scope_matches(
            md.get(mapping.SCOPE_PATH_KEY, "/"),
            mapping.normalize_scope_prefix(scope_prefix),
        ):
            return False
    if categories:
        if not (set(categories) & set(md.get(mapping.CATEGORIES_KEY, []) or [])):
            return False
    if older_than is not None:
        created = mapping._parse_dt(item.get("created_at"))
        if created >= older_than:
            return False
    return True
