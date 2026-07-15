"""``M3Store(BaseStore)`` — the LangGraph-native + LangMem long-term memory store.

PR-2. ``BaseStore``'s ENTIRE abstract surface is two methods — ``batch(ops)`` /
``abatch(ops)`` over ``GetOp | PutOp | SearchOp | ListNamespacesOp`` (verified
against langgraph 0.6.x). Every convenience method (``aget``/``asearch``/``aput``/
``adelete``/``get``/``search``/``put``/``delete``) has a default that funnels
through them — so implementing the two batch methods gives the whole store,
including the exact four methods LangMem drives (§2.1a: ``asearch``/``aput``/
``adelete``/``aget``). No LangMem shim required — ``store=M3Store()`` just works
with ``create_react_agent`` + LangMem managers.

Faithful to the tenets:
  * §7 Privacy — the namespace tuple ``(user_id, scope?, ...)`` is MANDATORY;
    ``user_id`` (element 0) is enforced (raise on empty), and every op sets
    ``user_id`` + ``scope="user"`` so a write can't land in the shared bucket
    and reads can't cross tenants. Same model as the mem0-compat surface.
  * §4 Efficiency — ``abatch`` COALESCES: many delete-puts → one
    ``memory_delete_bulk``; many writes → one ``memory_write_bulk_impl`` (direct,
    §11); searches run on the one loop-thread (no per-call loop).
  * §3 Robustness — results are typed ``Item``/``SearchItem``/None, in INPUT
    ORDER; LangChain-facing empties follow ITS contract (``get``→None,
    ``search``→[]). Missing langchain-core/langgraph fails loud at import.
  * §2.4 — arbitrary ``value`` keys ride ``metadata_json`` (split on write, merge
    on read) so a ``PutOp`` value round-trips losslessly; ``content`` is the text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# Hard dependency on langgraph — this module is imported LAZILY by the package
# __init__ so a missing dep fails loud with an install hint (§3), never here.
from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    SearchItem,
    SearchOp,
)

from . import mapping
from .m3client import M3Client

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_dt(raw: Any) -> datetime:
    """m3 timestamps are ISO-8601 strings; coerce to aware datetime (epoch on
    miss — the Item contract requires a datetime, never None)."""
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return _EPOCH
    return _EPOCH


class M3Store(BaseStore):
    """A LangGraph ``BaseStore`` backed by m3's hybrid long-term memory.

    Namespace convention (§2.1): ``namespace = (user_id, scope, *rest)``.
    ``user_id`` (element 0) is the tenancy key and is REQUIRED. ``scope`` is
    accepted as element 1 for documentation but always sent to m3 as the bounded
    category ``"user"`` (m3's ``scope`` is an enum, not free-form — the mem0
    surface learned this the hard way; isolation is on ``user_id``).
    """

    supports_ttl = False  # m3 has its own decay/TTL model; we don't honor Op.ttl.

    def __init__(self, *, agent_id: str = "langchain", call_timeout: float = 30.0):
        self._client = M3Client(agent_id=agent_id, call_timeout=call_timeout)

    # ── namespace → tenancy (§7) ──────────────────────────────────────────────
    @staticmethod
    def _user_id(namespace: tuple[str, ...]) -> str:
        # Namespace element 0 is the tenancy key; fall back to M3_DEFAULT_USER_ID
        # for single-user apps (resolve_user_id never invents a value — it returns
        # None when the env is unset, so the raise below still guards isolation).
        ns_uid = namespace[0] if namespace else None
        uid = mapping.resolve_user_id(ns_uid)
        if not uid:
            raise ValueError(
                "M3Store namespace must start with a non-empty user_id: "
                "namespace=(user_id, scope?, ...). m3 enforces per-user tenancy; "
                "there is no anonymous/global namespace. Set M3_DEFAULT_USER_ID "
                "for a single-user app."
            )
        return uid

    # ── the whole store: abatch + batch, both on the m3client loop-thread ─────
    async def abatch(self, ops: Iterable[Op]) -> list[Any]:
        """Execute a batch of ops, results in INPUT ORDER (BaseStore contract).

        Awaited on the caller's (LangGraph's) loop, but the actual m3 work is
        scheduled onto the single m3client loop-thread (§8: SQLite pool is
        thread-consistent, embedder/HTTP client is loop-affinity-bound). We bridge
        back with ``_run_async`` so the caller's loop is never blocked.
        """
        op_list = list(ops)
        return await self._client._run_async(lambda: self._abatch_on_loop(op_list))

    def batch(self, ops: Iterable[Op]) -> list[Any]:
        """Sync bridge over the same loop-thread work (§8)."""
        op_list = list(ops)
        return self._client._run(self._abatch_on_loop(op_list))

    async def _abatch_on_loop(self, ops: list[Op]) -> list[Any]:
        """The real batch body — runs ENTIRELY on the m3client loop-thread.

        A LangGraph ``key`` is a caller-chosen string, NOT m3's uuid — m3
        generates its own id. We map ``key`` to an m3 row by
        ``(user_id, scope, title=key)``, storing the key in ``title`` +
        ``metadata_json._ns_key``. So put is idempotent-by-key (an existing key
        is superseded, not duplicated) and get/delete resolve key→id first.
        """
        results: list[Any] = [None] * len(ops)

        # Coalesce bulk deletes (put value=None AND resolved existing rows).
        write_items: list[dict] = []
        delete_ids: list[str] = []

        for i, op in enumerate(ops):
            if isinstance(op, PutOp):
                uid = self._user_id(op.namespace)
                existing = self._resolve_id_by_key(uid, op.key)
                if op.value is None:
                    if existing:
                        delete_ids.append(existing)
                    results[i] = None
                else:
                    content, md = mapping.split_value(op.value)
                    if existing:
                        # idempotent-by-key: supersede the prior row (bi-temporal,
                        # not a silent overwrite — §0b contradiction handling).
                        from memory_core import memory_supersede_impl
                        await memory_supersede_impl(
                            old_id=existing, content=content, title=op.key,
                            user_id=uid, scope="user",
                            metadata=mapping.dumps_metadata({**md, "_ns_key": op.key}),
                        )
                    else:
                        write_items.append({
                            "type": "fact",
                            "content": content,
                            "title": op.key,       # LangGraph key -> title
                            "user_id": uid,
                            "scope": "user",
                            "metadata": {**md, "_ns_key": op.key},
                            "auto_classify": True,
                            "source": "langgraph",
                        })
                    results[i] = None  # PutOp returns None
            elif isinstance(op, GetOp):
                results[i] = await self._do_get(op)
            elif isinstance(op, SearchOp):
                results[i] = await self._do_search(op)
            elif isinstance(op, ListNamespacesOp):
                results[i] = await self._do_list_namespaces(op)
            else:  # unknown op type — fail loud (§3)
                raise TypeError(f"M3Store.abatch: unsupported op {type(op).__name__}")

        # Coalesced NEW writes → ONE bulk impl call (§4/§11).
        if write_items:
            from memory_core import memory_write_bulk_impl
            await memory_write_bulk_impl(write_items, check_contradictions=True)

        # Coalesced deletes → ONE bulk delete.
        if delete_ids:
            from memory_core import memory_delete_bulk_impl
            memory_delete_bulk_impl(delete_ids)  # sync impl

        return results

    def _resolve_id_by_key(self, user_id: str, key: str) -> Optional[str]:
        """Find the m3 row id for a LangGraph (user_id, key) — via title match
        within the user's scope. Returns None if the key was never written."""
        from memory.db import _db

        with _db() as db:
            row = db.execute(
                "SELECT id FROM memory_items "
                "WHERE user_id = ? AND scope = 'user' AND title = ? "
                "AND (is_deleted IS NULL OR is_deleted = 0) "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id, key),
            ).fetchone()
        return row["id"] if row else None

    # ── op implementations (async, on the loop-thread) ────────────────────────
    async def _do_get(self, op: GetOp) -> Optional[Item]:
        uid = self._user_id(op.namespace)
        # A LangGraph key is NOT an m3 uuid — resolve key→id within the tenant.
        row_id = self._resolve_id_by_key(uid, op.key)
        if row_id is None:
            return None
        from memory_core import memory_get_impl

        raw = memory_get_impl(row_id)  # sync impl, returns JSON string / sentinel
        item = mapping.parse_get(raw)
        if item is None:
            return None
        return self._to_item(op.namespace, op.key, item)

    async def _do_search(self, op: SearchOp) -> list[SearchItem]:
        uid = self._user_id(op.namespace_prefix)
        from memory_core import memory_search_scored_impl

        type_filter = ""
        if op.filter and isinstance(op.filter, dict):
            tf = op.filter.get("type")
            if isinstance(tf, str):
                type_filter = tf
        rows = await memory_search_scored_impl(
            query=op.query or "",
            user_id=uid,
            scope="user",
            k=op.limit,
            type_filter=type_filter,
            extra_columns=mapping.EXTRA_COLUMNS,
        ) or []
        # offset is applied client-side (m3 search has no offset arg).
        if op.offset:
            rows = rows[op.offset:]
        return [self._to_search_item(op.namespace_prefix, score, it)
                for score, it in rows]

    async def _do_list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        """Distinct (user_id, scope) tuples — IDs only, never content (§6)."""
        from memory.db import _db

        self._client_ensure_bin()
        out: list[tuple[str, ...]] = []
        with _db() as db:
            cur = db.execute(
                "SELECT DISTINCT user_id, scope FROM memory_items "
                "WHERE user_id IS NOT NULL AND user_id != '' "
                "AND (is_deleted IS NULL OR is_deleted = 0) "
                "LIMIT ?",
                (int(op.limit or 100),),
            )
            for r in cur.fetchall():
                ns = tuple(x for x in (r["user_id"], r["scope"]) if x)
                if op.max_depth:
                    ns = ns[: op.max_depth]
                out.append(ns)
        if op.offset:
            out = out[op.offset:]
        return out

    def _client_ensure_bin(self) -> None:
        from .m3client import _ensure_bin_on_path
        _ensure_bin_on_path()

    # ── row → LangGraph objects ───────────────────────────────────────────────
    def _to_item(self, namespace: tuple[str, ...], key: str, item: dict) -> Item:
        return Item(
            value=mapping.merge_value(item),
            key=key,
            namespace=tuple(namespace),
            created_at=_parse_dt(item.get("created_at")),
            updated_at=_parse_dt(item.get("updated_at") or item.get("created_at")),
        )

    def _to_search_item(
        self, namespace: tuple[str, ...], score: float, item: dict
    ) -> SearchItem:
        # The stored LangGraph key was carried in metadata_json as _ns_key; fall
        # back to the m3 id if a row wasn't written through this store.
        md = mapping._loads_metadata(item.get("metadata_json"))
        key = md.get("_ns_key") or item.get("id") or ""
        return SearchItem(
            namespace=tuple(namespace),
            key=key,
            value=mapping.merge_value(item),
            created_at=_parse_dt(item.get("valid_from") or item.get("created_at")),
            updated_at=_parse_dt(item.get("updated_at") or item.get("created_at")),
            score=score,
        )
